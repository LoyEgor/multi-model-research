#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import html
import http.server
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
DATA_DIR = ROOT / "data"
MODEL_STATS = DATA_DIR / "model-stats.jsonl"
RAW_TIMEOUT_SEC = max(60, env_int("RESEARCH_MODEL_TIMEOUT_SEC", 900))
URL_TIMEOUT_SEC = max(2, env_int("RESEARCH_URL_TIMEOUT_SEC", 12))
MAX_PRIMARY_WORKERS = max(2, env_int("RESEARCH_MAX_PRIMARY_WORKERS", 6))
MAX_VERIFY_WORKERS = max(2, env_int("RESEARCH_MAX_VERIFY_WORKERS", 8))
# A run with no heartbeat for longer than one full model call + slack is dead, not "running".
STALE_AFTER_SEC = RAW_TIMEOUT_SEC + 600
ACTIVE_RUNS: set[str] = set()

# Per-run circuit breaker: after N consecutive call failures a leg is disabled for the REST OF
# THAT RUN — remaining calls to it return instantly instead of burning full timeouts, the other
# legs keep the search going, and the degradation is recorded in run.json + the final report.
BREAKER_THRESHOLD = max(2, env_int("RESEARCH_LEG_BREAKER", 3))
LEG_HEALTH_LOCK = threading.Lock()
RUN_LEG_HEALTH: dict[str, dict[str, dict]] = {}


def init_leg_health(run_id: str) -> None:
    with LEG_HEALTH_LOCK:
        RUN_LEG_HEALTH[run_id] = {}


def clear_leg_health(run_id: str) -> None:
    with LEG_HEALTH_LOCK:
        RUN_LEG_HEALTH.pop(run_id, None)


def leg_health_snapshot(run_id: str) -> dict:
    with LEG_HEALTH_LOCK:
        return {leg: dict(entry) for leg, entry in RUN_LEG_HEALTH.get(run_id, {}).items()}


def leg_disabled(run_id: str, leg: str) -> bool:
    with LEG_HEALTH_LOCK:
        entry = RUN_LEG_HEALTH.get(run_id, {}).get(leg)
        return bool(entry and entry.get("disabled"))


def disabled_legs(run_id: str) -> list[str]:
    with LEG_HEALTH_LOCK:
        return sorted(leg for leg, entry in RUN_LEG_HEALTH.get(run_id, {}).items() if entry.get("disabled"))


def record_leg_result(run_id: str, leg: str, success: bool) -> bool:
    """Returns True when this result just tripped the breaker for the leg."""
    with LEG_HEALTH_LOCK:
        run_entry = RUN_LEG_HEALTH.get(run_id)
        if run_entry is None:
            return False
        entry = run_entry.setdefault(leg, {"consecutive_failures": 0, "disabled": False})
        if success:
            entry["consecutive_failures"] = 0
            return False
        entry["consecutive_failures"] += 1
        if not entry["disabled"] and entry["consecutive_failures"] >= BREAKER_THRESHOLD:
            entry["disabled"] = True
            return True
        return False


def force_disable_leg(run_id: str, leg: str, reason: str) -> bool:
    """Instantly disable a leg (quota exhaustion etc.) — no need to burn 3 strikes."""
    with LEG_HEALTH_LOCK:
        run_entry = RUN_LEG_HEALTH.get(run_id)
        if run_entry is None:
            return False
        entry = run_entry.setdefault(leg, {"consecutive_failures": 0, "disabled": False})
        if entry["disabled"]:
            return False
        entry["disabled"] = True
        entry["reason"] = reason
        return True


# Gemini's subscription quota is the scarcest resource in the system: parallel hammering
# exhausts it mid-run. Cap concurrent gemini calls process-wide and give each run a call
# budget from its effort profile — spend on primary search first (phases run in order).
GEMINI_CONCURRENCY = threading.Semaphore(max(1, env_int("RESEARCH_GEMINI_CONCURRENCY", 2)))
LEG_BUDGET_LOCK = threading.Lock()
RUN_LEG_BUDGET: dict[str, dict[str, int]] = {}


def init_leg_budget(run_id: str, budgets: dict[str, int]) -> None:
    with LEG_BUDGET_LOCK:
        RUN_LEG_BUDGET[run_id] = dict(budgets)


def consume_leg_budget(run_id: str, leg: str) -> bool:
    """True if the call may proceed; False when the leg's per-run budget is spent."""
    with LEG_BUDGET_LOCK:
        budgets = RUN_LEG_BUDGET.get(run_id)
        if budgets is None or leg not in budgets:
            return True
        if budgets[leg] <= 0:
            return False
        budgets[leg] -= 1
        return True


def clear_leg_budget(run_id: str) -> None:
    with LEG_BUDGET_LOCK:
        RUN_LEG_BUDGET.pop(run_id, None)


# Live subprocess registry per run: lets a fan-out phase kill its stragglers (and only its own
# calls — phases never overlap within a run) once the quorum is in.
PROC_REGISTRY_LOCK = threading.Lock()
RUN_PROCS: dict[str, dict[str, int]] = {}
RUN_DROPPED: dict[str, set[str]] = {}


def register_proc(run_id: str, record_id: str, pid: int) -> None:
    with PROC_REGISTRY_LOCK:
        RUN_PROCS.setdefault(run_id, {})[record_id] = pid


def unregister_proc(run_id: str, record_id: str) -> None:
    with PROC_REGISTRY_LOCK:
        RUN_PROCS.get(run_id, {}).pop(record_id, None)


def kill_stragglers(run_id: str) -> list[str]:
    with PROC_REGISTRY_LOCK:
        procs = dict(RUN_PROCS.get(run_id) or {})
    killed = []
    for record_id, pid in procs.items():
        try:
            os.killpg(pid, signal.SIGTERM)
            killed.append(record_id)
        except Exception:
            pass
    if killed:
        with PROC_REGISTRY_LOCK:
            RUN_DROPPED.setdefault(run_id, set()).update(killed)
    return killed


def was_dropped_as_straggler(run_id: str, record_id: str) -> bool:
    with PROC_REGISTRY_LOCK:
        return record_id in RUN_DROPPED.get(run_id, set())


def clear_run_registry(run_id: str) -> None:
    with PROC_REGISTRY_LOCK:
        RUN_PROCS.pop(run_id, None)
        RUN_DROPPED.pop(run_id, None)

# Effort scales breadth (tasks), depth (recheck rounds/items) and verification layers (cross-vendor
# adversarial review of the draft, Claude adjudication of disputes). The search legs (codex/gemini)
# NEVER downgrade — flagship only, tier guards in lib/ask_*.sh stay in force. The Claude seat is
# tiered by COST: sonnet at low effort, opus at high effort. Opus is the HARD ceiling (owner
# decision 2026-06-11): never Fable/Mythos-class models — the wrapper blocks them outright.
# review_legs: vendors that adversarially review the codex draft, one round each, in order.
EFFORT_PROFILES = {
    1: {
        "effort": "quick",
        "task_count": 3,
        "recheck_rounds": 1,
        "max_recheck_items": 4,
        "recheck_legs": 1,
        "review_legs": [],
        "adjudicate_disputes": True,
        "claude_model": "sonnet",
        "search_effort": "medium",
        "judge_effort": "medium",
        "search_timeout_sec": 420,
        "recheck_timeout_sec": 300,
        "straggler_grace_sec": 90,
        "gemini_call_budget": 6,
    },
    2: {
        "effort": "standard",
        "task_count": 4,
        "recheck_rounds": 1,
        "max_recheck_items": 6,
        "recheck_legs": 1,
        "review_legs": [],
        "adjudicate_disputes": True,
        "claude_model": "sonnet",
        "search_effort": "medium",
        "judge_effort": "xhigh",
        "search_timeout_sec": 480,
        "recheck_timeout_sec": 360,
        "straggler_grace_sec": 120,
        "gemini_call_budget": 9,
    },
    3: {
        "effort": "deep",
        "task_count": 5,
        "recheck_rounds": 2,
        "max_recheck_items": 8,
        "recheck_legs": 1,
        "review_legs": ["gemini"],
        "adjudicate_disputes": True,
        "claude_model": "opus",
        "search_effort": "medium",
        "judge_effort": "xhigh",
        "search_timeout_sec": 600,
        "recheck_timeout_sec": 480,
        "straggler_grace_sec": 240,
        "gemini_call_budget": 12,
    },
    4: {
        "effort": "max",
        "task_count": 6,
        "recheck_rounds": 2,
        "max_recheck_items": 12,
        "recheck_legs": 2,
        "review_legs": ["gemini", "claude"],
        "adjudicate_disputes": True,
        "claude_model": "opus",
        "search_effort": "xhigh",
        "judge_effort": "xhigh",
        "search_timeout_sec": 900,
        "recheck_timeout_sec": 600,
        "straggler_grace_sec": 360,
        "gemini_call_budget": 16,
    },
}
# Once this fraction of a phase's parallel calls has returned, the remaining stragglers get a
# bounded grace window (max of the profile grace and half the median completed latency) and are
# then killed — their items flow into the rescue pool instead of stalling the whole phase.
STRAGGLER_QUORUM = 0.75
EFFORT_NAME_TO_LEVEL = {profile["effort"]: level for level, profile in EFFORT_PROFILES.items()}
DEFAULT_EFFORT_LEVEL = 2
PRICE_DISPUTE_RATIO = 1.10
MAX_ADJUDICATED_ITEMS = 6


def parse_effort(value: object) -> int:
    if value is None or value == "":
        return DEFAULT_EFFORT_LEVEL
    text = str(value).strip().lower()
    if text in EFFORT_NAME_TO_LEVEL:
        return EFFORT_NAME_TO_LEVEL[text]
    try:
        level = int(text)
    except ValueError:
        return DEFAULT_EFFORT_LEVEL
    return min(max(level, min(EFFORT_PROFILES)), max(EFFORT_PROFILES))


def normalize_site(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if "//" not in text:
        text = "//" + text
    host = urllib.parse.urlsplit(text).netloc.split(":")[0].strip(".")
    host = re.sub(r"^www\.", "", host)
    return host if "." in host else None


def normalize_sites(values: object) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,\s]+", values)
    sites = []
    for value in values or []:
        site = normalize_site(value)
        if site and site not in sites:
            sites.append(site)
    return sites


def make_config(effort: object = None, sites: object = None) -> dict:
    level = parse_effort(effort)
    config = dict(EFFORT_PROFILES[level])
    config["effort_level"] = level
    config["sites"] = normalize_sites(sites)
    # Explicit env knobs still override the profile (documented in README).
    if os.environ.get("RESEARCH_MAX_TASKS") is not None:
        config["task_count"] = min(6, max(3, env_int("RESEARCH_MAX_TASKS", config["task_count"])))
    if os.environ.get("RESEARCH_MAX_RECHECK_ITEMS") is not None:
        config["max_recheck_items"] = max(0, env_int("RESEARCH_MAX_RECHECK_ITEMS", config["max_recheck_items"]))
    return config

FINDING_FIELDS = [
    "title",
    "price",
    "currency",
    "url",
    "marketplace",
    "availability",
    "condition",
    "seller",
    "location",
    "shipping",
    "evidence",
    "confidence",
    "source_model",
    "checked_at",
]

OUT_OF_STOCK_RE = re.compile(
    r"(out\s*of\s*stock|sold|unavailable|not\s+available|"
    r"немає\s+в\s+наявності|нема\s+в\s+наявності|нет\s+в\s+наличии|продано|законч)",
    re.IGNORECASE,
)
TRACKING_QUERY_RE = re.compile(r"^(utm_|fbclid$|gclid$|yclid$|mc_)", re.IGNORECASE)
STATS_LOCK = threading.Lock()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(text: str, limit: int = 46) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (slug[:limit].strip("-") or "research")


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp per writer: run.json is written concurrently (heartbeat in the main thread,
    # breaker updates from worker threads) — a shared tmp name loses the race and crashes.
    tmp = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex[:8]}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path: Path, default: object | None = None) -> object:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with STATS_LOCK:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def extract_json(text: str) -> object:
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty model output")

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL):
        candidate = match.group(1).strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(text[idx:])
            return value
        except json.JSONDecodeError:
            continue

    raise ValueError("no valid JSON object found")


def parse_price(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return float(value)
    text = str(value)
    text = text.replace("\u00a0", " ")
    match = re.search(r"([0-9][0-9\s.,]*)", text)
    if not match:
        return None
    number = match.group(1).strip().replace(" ", "")
    if "," in number and "." in number:
        number = number.replace(",", "")
    elif "," in number:
        number = number.replace(",", ".")
    try:
        parsed = float(number)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


# Marketplace listing IDs survive language prefixes (/d/uk/... vs /d/...), slug edits, and
# mirrors — canonicalize by ID so the same listing never appears twice (HANDOFF finding #2).
LISTING_ID_PATTERNS = [
    ("olx", re.compile(r"(?:^|\.)olx\.[a-z.]{2,6}/.*-ID([A-Za-z0-9]+)\.html", re.IGNORECASE)),
    ("prom", re.compile(r"(?:^|\.)prom\.ua/(?:[a-z]{2}/)?p(\d+)-", re.IGNORECASE)),
    ("rozetka", re.compile(r"(?:^|\.)rozetka\.com\.ua/.*/p(\d+)/", re.IGNORECASE)),
]


def listing_key(url: object) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlsplit(str(url).strip())
    flat = (parsed.netloc.lower() + parsed.path)
    for marketplace, pattern in LISTING_ID_PATTERNS:
        match = pattern.search(flat)
        if match:
            return f"{marketplace}:{match.group(1).lower()}"
    return None


def url_in_sites(url: object, sites: list[str]) -> bool:
    if not sites:
        return True
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = parsed.netloc.split(":")[0].lower().strip(".")
    host = re.sub(r"^www\.", "", host)
    return any(host == site or host.endswith("." + site) for site in sites)


def normalize_url_for_key(url: object) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlsplit(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if not TRACKING_QUERY_RE.search(k)]
    normalized_query = urllib.parse.urlencode(sorted(query))
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, normalized_query, ""))


def normalize_finding(item: dict, source_model: str, task_id: str, record_id: str) -> dict:
    finding = {field: None for field in FINDING_FIELDS}
    for field in FINDING_FIELDS:
        if field in item:
            finding[field] = item.get(field)

    finding["price"] = parse_price(finding.get("price"))
    if finding["price"] is not None and not finding.get("currency"):
        finding["currency"] = "UAH"

    if finding.get("url") is not None:
        finding["url"] = str(finding["url"]).strip() or None
    if finding.get("title") is not None:
        finding["title"] = str(finding["title"]).strip() or None
    if isinstance(finding.get("evidence"), list):
        finding["evidence"] = "; ".join(str(x) for x in finding["evidence"] if x)
    elif finding.get("evidence") is not None:
        finding["evidence"] = str(finding["evidence"]).strip() or None

    try:
        confidence = float(finding["confidence"]) if finding.get("confidence") is not None else None
        finding["confidence"] = min(max(confidence, 0.0), 1.0) if confidence is not None else None
    except (TypeError, ValueError):
        finding["confidence"] = None

    finding["source_model"] = source_model
    finding["source_models"] = [source_model]
    finding["checked_at"] = utc_now()
    finding["task_id"] = task_id
    finding["record_id"] = record_id
    finding["disputed"] = False
    finding["price_candidates"] = (
        [{"price": finding["price"], "source_model": source_model}] if finding["price"] is not None else []
    )
    return finding


def coerce_findings(payload: object, source_model: str, task_id: str, record_id: str) -> list[dict]:
    if isinstance(payload, dict):
        candidates = payload.get("findings") or payload.get("items") or payload.get("results") or []
    elif isinstance(payload, list):
        candidates = payload
    else:
        candidates = []

    findings = []
    for item in candidates:
        if isinstance(item, dict):
            findings.append(normalize_finding(item, source_model, task_id, record_id))
    return findings


def dedupe_key(finding: dict) -> str:
    id_key = listing_key(finding.get("url"))
    if id_key:
        return "listing:" + id_key
    url_key = normalize_url_for_key(finding.get("url"))
    if url_key:
        return "url:" + url_key
    title = re.sub(r"\s+", " ", str(finding.get("title") or "").strip().lower())
    return f"item:{title}|{finding.get('price')}|{finding.get('currency')}"


def merge_finding(existing: dict, incoming: dict) -> dict:
    models = set(existing.get("source_models") or [existing.get("source_model")])
    models.update(incoming.get("source_models") or [incoming.get("source_model")])
    existing["source_models"] = sorted(m for m in models if m)

    for field in FINDING_FIELDS:
        if existing.get(field) in (None, "") and incoming.get(field) not in (None, ""):
            existing[field] = incoming[field]

    if incoming.get("price") is not None:
        if existing.get("price") is None or incoming["price"] < existing["price"]:
            existing["price"] = incoming["price"]
            if incoming.get("currency"):
                existing["currency"] = incoming["currency"]

    evidences = []
    for evidence in (existing.get("evidence"), incoming.get("evidence")):
        if evidence and evidence not in evidences:
            evidences.append(evidence)
    existing["evidence"] = " | ".join(evidences) if evidences else existing.get("evidence")

    if incoming.get("confidence") is not None:
        existing["confidence"] = max(existing.get("confidence") or 0.0, incoming["confidence"])

    candidates = list(existing.get("price_candidates") or [])
    for candidate in incoming.get("price_candidates") or []:
        if candidate not in candidates:
            candidates.append(candidate)
    existing["price_candidates"] = candidates
    prices = sorted(c["price"] for c in candidates if c.get("price"))
    # Cross-leg disagreement on the same canonical item: keep it, flag it, let the recheck /
    # adjudication stages resolve it by verified fact (never average, never vote).
    existing["disputed"] = bool(prices) and prices[-1] > prices[0] * PRICE_DISPUTE_RATIO
    return existing


def dedupe_findings(findings: list[dict]) -> list[dict]:
    by_key: dict[str, dict] = {}
    for finding in findings:
        key = dedupe_key(finding)
        if key not in by_key:
            by_key[key] = dict(finding)
        else:
            by_key[key] = merge_finding(by_key[key], finding)
    return list(by_key.values())


# --- Stage 2 (minimal): live page-content verification for marketplace listings -------------
# HTTP 200 is NOT enough: sellers repurpose listings (a "macbook-air-m2" slug serving a Dyson),
# prices move within hours, and search-index caches feed models stale prices (both legs agreed
# on 20500 while the live page said 30000 — benchmark 2026-06-12). For URLs with a known
# listing-ID pattern we fetch the page, read the live price and ad status, and RESOLVE BY
# VERIFIED FACT: the page beats any model claim.
LIVE_PRICE_RE = re.compile(r'"price"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?')
LIVE_AD_STATUS_RE = re.compile(r'\\?"status\\?"\s*:\s*\\?"([a-z_]+)\\?"')
LIVE_PRICE_TOLERANCE = 1.10
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def live_listing_check(url: object, timeout: float | None = None) -> dict:
    result: dict = {"ok": False, "live_price": None, "ad_status": None, "reason": None}
    request = urllib.request.Request(str(url), headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout or URL_TIMEOUT_SEC * 2) as response:
            html_body = response.read(2_500_000).decode("utf-8", errors="replace")
    except Exception as exc:
        result["reason"] = exc.__class__.__name__
        return result

    status_match = LIVE_AD_STATUS_RE.search(html_body)
    if status_match:
        result["ad_status"] = status_match.group(1)
    price_match = LIVE_PRICE_RE.search(html_body)
    if price_match:
        result["live_price"] = parse_price(price_match.group(1))
    result["ok"] = True
    return result


def apply_live_check(item: dict) -> None:
    """Mutates a finding after its page was read: inactive ads get flagged for rejection,
    live price overrides the model's claim (kept in price_candidates for the audit trail)."""
    if not listing_key(item.get("url")):
        return
    live = live_listing_check(item.get("url"))
    item["live_check"] = live
    if not live["ok"]:
        return
    if live["ad_status"] and live["ad_status"] != "active":
        item["listing_inactive"] = True
        return
    live_price = live.get("live_price")
    claimed = item.get("price")
    if live_price is None:
        return
    if claimed is None or max(live_price, claimed) > min(live_price, claimed) * LIVE_PRICE_TOLERANCE:
        candidates = list(item.get("price_candidates") or [])
        candidate = {"price": live_price, "source_model": "live_page"}
        if candidate not in candidates:
            candidates.append(candidate)
        item["price_candidates"] = candidates
        if claimed is not None:
            item["price_corrected_from"] = claimed
        item["price"] = live_price
        item["disputed"] = False  # resolved by verified fact, not by vote


def verify_url(url: object, timeout: float = URL_TIMEOUT_SEC) -> dict:
    if not url:
        return {"ok": False, "reason": "missing_url", "status": None}
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"ok": False, "reason": "invalid_url", "status": None}

    headers = {"User-Agent": "multi-model-research/1.0"}
    methods = ("HEAD", "GET")
    last_reason = "url_unverified"
    last_status = None

    for method in methods:
        request = urllib.request.Request(str(url), headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", response.getcode())
                if 200 <= int(status) < 400:
                    return {"ok": True, "reason": "ok", "status": int(status), "method": method}
                last_reason = f"http_{status}"
                last_status = int(status)
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            last_reason = f"http_{exc.code}"
            if exc.code in {405, 403, 429} and method == "HEAD":
                continue
        except urllib.error.URLError as exc:
            last_reason = exc.reason.__class__.__name__ if not isinstance(exc.reason, str) else exc.reason
        except TimeoutError:
            last_reason = "timeout"
        except Exception as exc:
            last_reason = exc.__class__.__name__

    return {"ok": False, "reason": last_reason, "status": last_status}


def rejection_reasons(finding: dict, url_check: dict | None = None, sites: list[str] | None = None) -> list[str]:
    if finding.get("parse_failed"):
        return ["parse_failed"]

    reasons = []
    if not finding.get("url"):
        reasons.append("missing_url")
    elif sites and not url_in_sites(finding.get("url"), sites):
        reasons.append("off_site")
    elif url_check and not url_check.get("ok"):
        reasons.append(url_check.get("reason") or "url_unverified")

    if finding.get("listing_inactive"):
        reasons.append("listing_inactive")

    if finding.get("price") is None:
        reasons.append("missing_price")

    availability = str(finding.get("availability") or "")
    if availability and OUT_OF_STOCK_RE.search(availability):
        reasons.append("out_of_stock")

    return reasons


# A rejection is final only when the item is disproven or excluded by policy. Everything else
# (broken/moved URL, timeout, missing price, 4xx) is a FAILURE TO VERIFY — the item may be exactly
# what the user wants, so it gets rescue rechecks and an "unconfirmed" slot in the report.
NON_RESCUABLE_REASONS = {"parse_failed", "off_site", "out_of_stock", "adjudicated_reject", "listing_inactive"}
SEARCH_LEGS = ("codex", "gemini")


def is_rescuable(item: dict) -> bool:
    reasons = item.get("reasons") or []
    return bool(reasons) and not any(reason in NON_RESCUABLE_REASONS for reason in reasons)


def sort_by_price(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda x: (x.get("price") is None, x.get("price") or 10**18, str(x.get("title") or "")))


def verify_findings(
    findings: list[dict],
    parse_rejections: list[dict] | None = None,
    sites: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    unique = dedupe_findings(findings)
    verified: list[dict] = []
    rejected: list[dict] = list(parse_rejections or [])

    def check_one(finding: dict) -> tuple[dict, dict]:
        item = dict(finding)
        url_check = verify_url(item.get("url"))
        if url_check.get("ok"):
            apply_live_check(item)
        return item, url_check

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_VERIFY_WORKERS, max(1, len(unique)))) as executor:
        futures = [executor.submit(check_one, finding) for finding in unique]
        for future in concurrent.futures.as_completed(futures):
            item, url_check = future.result()
            item["url_check"] = url_check
            reasons = rejection_reasons(item, url_check, sites)
            if reasons:
                item["reasons"] = reasons
                rejected.append(item)
            else:
                verified.append(item)

    return sort_by_price(verified), sort_by_price(rejected)


def build_decompose_prompt(user_prompt: str, config: dict) -> str:
    sites = config.get("sites") or []
    site_rule = (
        f"HARD CONSTRAINT: the user restricted this research to {', '.join(sites)}. "
        f"Every task must search ONLY those domains; off-domain URLs will be rejected by code."
        if sites
        else (
            "First decide WHERE offers of this kind actually live — general marketplaces and "
            "classifieds (e.g. OLX, Prom.ua for goods in Ukraine), category-specialized portals "
            "(e.g. real-estate portals for housing, auto portals for vehicles), official "
            "stores/providers — and put those venues into preferred_sites per task."
        )
    )
    return f"""You are the thin planning brain for a multi-model offer research system.
The subject can be ANYTHING purchasable or rentable: a product, a service, a subscription plan,
an account, real estate, a vehicle. First reason about what the request actually involves
(which tiers/variants/sellers exist, where such offers are listed), then split the work so
different angles and venues are covered by independent tasks.
Return ONLY valid JSON. No Markdown.

User request:
{user_prompt}

Create {config["task_count"]} independent web-search tasks for finding current purchasable offers.
{site_rule}
Each task must be useful if run independently by another model.
Write plain queries WITHOUT search operators like site: — put domains in preferred_sites instead.

Schema:
{{
  "tasks": [
    {{
      "id": "task-1",
      "query": "specific search query",
      "focus": "what to verify",
      "preferred_sites": ["olx.ua", "prom.ua"]
    }}
  ]
}}
"""


def fallback_tasks(user_prompt: str, config: dict) -> list[dict]:
    sites = config.get("sites") or []
    if sites:
        tasks = [
            {
                "id": f"task-{idx}",
                "query": user_prompt,
                "focus": f"Search {site} listings only, verify availability and listed price.",
                "preferred_sites": [site],
            }
            for idx, site in enumerate(sites[:3], start=1)
        ]
        while len(tasks) < 3:
            tasks.append(
                {
                    "id": f"task-{len(tasks) + 1}",
                    "query": user_prompt,
                    "focus": "Find the cheapest currently available offers with prices and working URLs.",
                    "preferred_sites": sites,
                }
            )
        return tasks
    return [
        {
            "id": "task-1",
            "query": user_prompt,
            "focus": "Find the cheapest currently available direct offers with prices and working URLs.",
            "preferred_sites": [],
        },
        {
            "id": "task-2",
            "query": user_prompt,
            "focus": "Search general marketplaces and classifieds relevant to this subject; verify availability, location, and price.",
            "preferred_sites": [],
        },
        {
            "id": "task-3",
            "query": user_prompt,
            "focus": "Search category-specialized portals and official stores/providers for this subject; verify availability and price.",
            "preferred_sites": [],
        },
    ]


def coerce_tasks(payload: object, user_prompt: str, config: dict) -> list[dict]:
    if isinstance(payload, dict):
        raw_tasks = payload.get("tasks") or []
    elif isinstance(payload, list):
        raw_tasks = payload
    else:
        raw_tasks = []

    run_sites = config.get("sites") or []
    tasks: list[dict] = []
    for idx, raw in enumerate(raw_tasks[: config["task_count"]], start=1):
        if not isinstance(raw, dict):
            continue
        query = str(raw.get("query") or "").strip()
        if not query:
            continue
        preferred = raw.get("preferred_sites") or []
        if not isinstance(preferred, list):
            preferred = [str(preferred)]
        preferred = [site for site in (normalize_site(s) for s in preferred) if site]
        if run_sites:
            preferred = [site for site in preferred if site in run_sites] or list(run_sites)
        tasks.append(
            {
                "id": str(raw.get("id") or f"task-{idx}"),
                "query": query,
                "focus": str(raw.get("focus") or "Find current purchasable offers with verified URLs."),
                "preferred_sites": preferred,
            }
        )

    return tasks if len(tasks) >= 3 else fallback_tasks(user_prompt, config)


SITE_OPERATOR_RE = re.compile(r"\bsite:\S+", re.IGNORECASE)


def shape_query_for_leg(query: str, leg: str, sites: list[str]) -> str:
    # Per-leg query templates (HANDOFF finding #4): Codex web_search returns empty findings on
    # site:-operator queries — strip them and rely on the plain-language domain instruction.
    # Gemini's Google grounding understands site: natively — add it when domains are enforced.
    if leg == "gemini":
        if sites and not SITE_OPERATOR_RE.search(query):
            operator = " OR ".join(f"site:{site}" for site in sites[:3])
            return f"{query} ({operator})" if len(sites) > 1 else f"{query} site:{sites[0]}"
        return query
    return re.sub(r"\s{2,}", " ", SITE_OPERATOR_RE.sub("", query)).strip() or query


def build_search_prompt(user_prompt: str, task: dict, leg: str, config: dict) -> str:
    run_sites = config.get("sites") or []
    task_sites = [s for s in (task.get("preferred_sites") or []) if s]
    sites = run_sites or task_sites
    query = shape_query_for_leg(str(task.get("query") or ""), leg, run_sites)
    site_rule = (
        f"- HARD CONSTRAINT: only URLs on these domains are accepted: {', '.join(sites)}. "
        f"Any other domain will be rejected by automated verification.\n"
        if run_sites
        else ""
    )
    return f"""You are a web research worker for current purchasable offers.
Work independently. Do not assume another model will fill gaps.
Use live web results. Return ONLY valid JSON. No Markdown.

Original user request:
{user_prompt}

Task id: {task.get("id")}
Search query: {query}
Focus: {task.get("focus")}
Preferred sites: {", ".join(sites) or "none"}

Rules:
{site_rule}
- Return direct purchasable product/listing URLs, not category pages when avoidable.
- Unknown fields must be null. Do not invent prices, stock, location, shipping, or URLs.
- Use numeric prices. If the product is in Ukraine and currency is not explicit, use UAH.
- Include evidence as a short phrase explaining what you verified on the page.
- Record the item's CONDITION (new / used-good / damaged / for parts) and SELLER trust signals
  (rating, reviews count, account age, business vs private) whenever the page shows them.
- A price far below market with no explanation is a scam signal — still report the item, but say
  so in evidence and lower confidence. The user wants working, honestly-described items.
- Include only current offers that look relevant to the original request.

Schema:
{{
  "findings": [
    {{
      "title": "listing title",
      "price": 12345,
      "currency": "UAH",
      "url": "https://...",
      "marketplace": "OLX",
      "availability": "in stock / available / out of stock / sold / unknown",
      "condition": "new / used-good / damaged / for parts / unknown",
      "seller": "trust signals: rating, reviews, account age, or null",
      "location": "city or region or null",
      "shipping": "shipping details or null",
      "evidence": "short evidence phrase",
      "confidence": 0.0
    }}
  ],
  "notes": []
}}
"""


def build_recheck_prompt(user_prompt: str, rejected_item: dict, config: dict) -> str:
    compact = json.dumps(
        {
            "title": rejected_item.get("title"),
            "price": rejected_item.get("price"),
            "currency": rejected_item.get("currency"),
            "url": rejected_item.get("url"),
            "marketplace": rejected_item.get("marketplace"),
            "availability": rejected_item.get("availability"),
            "reasons": rejected_item.get("reasons"),
            "disputed": rejected_item.get("disputed"),
            "price_candidates": rejected_item.get("price_candidates"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    sites = config.get("sites") or []
    site_rule = (
        f"\n- HARD CONSTRAINT: only URLs on these domains are accepted: {', '.join(sites)}."
        if sites
        else ""
    )
    return f"""You are the second-pass verifier for an offer research system.
Return ONLY valid JSON. No Markdown.

Original user request:
{user_prompt}

Rejected or disputed item:
{compact}

Your job:
- This item failed automated verification, but it may be EXACTLY what the user is looking for.
  Do not discard it lightly.
- First try to RECOVER this exact item: listings move, change language prefixes, or get re-posted —
  find the current working URL for the same offer, its live price, and availability.
- If the exact item is truly gone, find the closest equivalent current offer.
- Return an empty findings array ONLY if you are confident no current purchasable offer exists for it.{site_rule}

Use the same schema:
{{
  "findings": [
    {{
      "title": "listing title",
      "price": 12345,
      "currency": "UAH",
      "url": "https://...",
      "marketplace": "OLX",
      "availability": "available",
      "condition": "new / used-good / damaged / for parts / unknown",
      "seller": "trust signals: rating, reviews, account age, or null",
      "location": "city or region or null",
      "shipping": "shipping details or null",
      "evidence": "short evidence phrase",
      "confidence": 0.0
    }}
  ]
}}
"""


def build_synthesis_prompt(
    user_prompt: str,
    tasks: list[dict],
    verified: list[dict],
    rejected: list[dict],
    config: dict,
    degraded_legs: list[str] | None = None,
) -> str:
    unconfirmed = [item for item in rejected if is_rescuable(item)]
    dead = [item for item in rejected if not is_rescuable(item)]
    context = {
        "user_prompt": user_prompt,
        "restricted_to_sites": config.get("sites") or None,
        "degraded_legs": degraded_legs or None,
        "tasks": tasks,
        "verified_findings": verified[:20],
        "unconfirmed_candidates": unconfirmed[:10],
        "rejected_sample": dead[:8],
        "rejected_count": len(rejected),
    }
    return f"""You are the final judge for a multi-model offer research run.
Finish the search the way a careful human would: the FIRST option you present must be the one the
user most likely actually wants — not merely the lowest number.
Use only the structured facts below. Do not invent new offers or prices.
Write the final answer in the same language as the user request.

Facts:
{json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)}

Ranking rules:
- Infer the user's real intent: someone searching for an item wants a WORKING, honestly-described
  one from a credible seller — not damaged, for-parts, bait-priced, or scam-flavored offers.
- Rank by fit to that intent first, then by price. A slightly pricier listing with a solid
  condition and a trusted seller beats a cheaper suspicious one.
- CRITICAL: for EVERY option cheaper than your top pick, explain in one line why it was not chosen
  (damaged / for parts / scam signals / no seller history / dead or indirect link / disputed
  price / category page instead of a listing). The user must see that cheaper items were
  considered and understand why they lost.
- There is no cap on how many options you list — order them best-fit first.

Also include:
- URLs for every option.
- Items flagged "disputed": true carry conflicting cross-model prices (see price_candidates) —
  state the uncertainty explicitly instead of picking one silently.
- unconfirmed_candidates failed automated verification (broken/unreachable URL, missing price)
  but were NOT disproven — they may be exactly what the user wants. Put the promising ones in a
  separate "Unverified — check manually" section with their URLs and what to verify. Never
  silently drop them.
- Short rejection/risk notes for stale, out-of-stock, off-site, or disproven items.
- If degraded_legs is set, one of the search models failed during this run — say so plainly and
  warn that coverage may be incomplete.
- A clear conclusion: the best pick and the strongest runner-up.
"""


def build_review_prompt(user_prompt: str, draft: str, verified: list[dict], rejected: list[dict], config: dict) -> str:
    context = {
        "user_prompt": user_prompt,
        "restricted_to_sites": config.get("sites") or None,
        "verified_findings": verified[:12],
        "rejected_sample": rejected[:8],
    }
    return f"""You are an adversarial reviewer from a DIFFERENT model family than the draft's author.
Your job is to try to REFUTE the draft below, not to polish it.
Return ONLY valid JSON. No Markdown.

Draft report:
---
{draft}
---

Verified facts the draft must rest on:
{json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)}

Check for:
- Claims, prices, or URLs in the draft that are NOT supported by the verified findings.
- A verified cheaper or better option the draft ignored or buried.
- Disputed prices presented as certain.
- A conclusion that does not follow from the facts.

Schema:
{{
  "verdict": "approve" | "revise",
  "issues": [
    {{"claim": "what the draft says", "problem": "why it is wrong or unsupported", "fix": "what to do"}}
  ]
}}
"""


def build_revision_prompt(user_prompt: str, draft: str, issues: list[dict], config: dict) -> str:
    return f"""You are the final judge revising your report after an adversarial cross-vendor review.
Fix ONLY the listed issues using the facts already in the draft; do not invent new offers.
Write the final answer in the same language as the user request. Return the full revised Markdown report.

Original user request:
{user_prompt}

Current draft:
---
{draft}
---

Reviewer issues to address:
{json.dumps(issues, ensure_ascii=False, indent=2)}
"""


def build_adjudication_prompt(user_prompt: str, item: dict, config: dict) -> str:
    compact = {
        "title": item.get("title"),
        "url": item.get("url"),
        "marketplace": item.get("marketplace"),
        "availability": item.get("availability"),
        "condition": item.get("condition"),
        "seller": item.get("seller"),
        "price_candidates": item.get("price_candidates"),
        "evidence": item.get("evidence"),
    }
    return f"""You are the independent arbiter in a multi-model research system.
Two model families reported DIFFERENT prices for the same item. Decide which price the evidence
supports, or reject the item if neither is trustworthy. Resolve by verified fact, not by averaging.
Return ONLY valid JSON. No Markdown.

Original user request:
{user_prompt}

Disputed item:
{json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True)}

Schema:
{{
  "action": "accept" | "reject",
  "price": 12345,
  "currency": "UAH",
  "reason": "one-sentence justification grounded in the evidence"
}}
"""


def safe_name(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", text).strip("-")[:90] or "item"


def call_model(
    leg: str,
    prompt: str,
    run_dir: Path,
    task_type: str,
    task_id: str,
    timeout: int = RAW_TIMEOUT_SEC,
    effort: str = "medium",
    claude_model: str | None = None,
    bypass_breaker: bool = False,
) -> dict:
    script = ROOT / "lib" / f"ask_{leg}.sh"
    record_id = f"{task_type}-{safe_name(task_id)}-{leg}-{uuid.uuid4().hex[:8]}"
    raw_base = run_dir / "raw" / record_id
    run_id = run_dir.name

    def skipped_meta(reason_key: str, reason_text: str) -> dict:
        meta = {
            "record_id": record_id,
            "leg": leg,
            "task_type": task_type,
            "task_id": task_id,
            "rc": -1,
            "success": False,
            "timed_out": False,
            reason_key: True,
            "latency_sec": 0.0,
        }
        write_json(raw_base.with_suffix(".meta.json"), meta)
        meta["stdout"] = ""
        meta["stderr"] = reason_text
        return meta

    if not bypass_breaker and leg_disabled(run_id, leg):
        return skipped_meta("skipped_by_breaker", "leg disabled by circuit breaker for this run")
    if not bypass_breaker and not consume_leg_budget(run_id, leg):
        return skipped_meta("skipped_by_budget", "leg call budget for this run is spent")

    started = time.monotonic()
    env = os.environ.copy()
    if leg == "codex":
        env["CODEX_EFFORT"] = effort
    if leg == "claude" and claude_model:
        env["CLAUDE_MODEL"] = claude_model
    semaphore = GEMINI_CONCURRENCY if leg == "gemini" else None
    if semaphore is not None:
        semaphore.acquire()

    try:
        proc = subprocess.Popen(
            [str(script), prompt],
            cwd=str(ROOT),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        register_proc(run_id, record_id, proc.pid)
        stdout, stderr = proc.communicate(timeout=timeout)
        rc = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
            stdout, stderr = proc.communicate()
        stdout = stdout or exc.stdout or ""
        stderr = stderr or exc.stderr or ""
        rc = 124
        timed_out = True
    finally:
        unregister_proc(run_id, record_id)
        if semaphore is not None:
            semaphore.release()

    elapsed = time.monotonic() - started
    raw_base.with_suffix(".txt").write_text(stdout, encoding="utf-8")
    raw_base.with_suffix(".stderr.txt").write_text(stderr, encoding="utf-8")
    meta = {
        "record_id": record_id,
        "leg": leg,
        "task_type": task_type,
        "task_id": task_id,
        "rc": rc,
        "success": rc == 0 and not timed_out,
        "timed_out": timed_out,
        "dropped_as_straggler": was_dropped_as_straggler(run_id, record_id),
        "latency_sec": round(elapsed, 3),
        "stdout_file": str(raw_base.with_suffix(".txt").relative_to(run_dir)),
        "stderr_file": str(raw_base.with_suffix(".stderr.txt").relative_to(run_dir)),
    }
    write_json(raw_base.with_suffix(".meta.json"), meta)
    breaker_tripped = record_leg_result(run_id, leg, meta["success"])
    if rc == 5 and force_disable_leg(run_id, leg, "quota_exhausted"):
        breaker_tripped = True
    if breaker_tripped:
        update_run(run_dir, leg_health=leg_health_snapshot(run_id))
    meta["stdout"] = stdout
    meta["stderr"] = stderr
    return meta


def init_run(prompt: str, config: dict) -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]
    run_id = f"{timestamp}-{slugify(prompt)}-{digest}-{uuid.uuid4().hex[:6]}"
    run_dir = RUNS_DIR / run_id
    (run_dir / "raw").mkdir(parents=True, exist_ok=False)
    write_json(
        run_dir / "run.json",
        {
            "run_id": run_id,
            "prompt": prompt,
            "status": "queued",
            "phase": "queued",
            "config": {
                "effort": config["effort"],
                "effort_level": config["effort_level"],
                "task_count": config["task_count"],
                "recheck_rounds": config["recheck_rounds"],
                "review_legs": config["review_legs"],
                "sites": config.get("sites") or [],
            },
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "error": None,
        },
    )
    return run_dir


RUN_JSON_LOCK = threading.Lock()


def update_run(run_dir: Path, **fields: object) -> None:
    # Read-modify-write under a lock: concurrent updates (heartbeat + breaker) must not
    # lose each other's fields.
    with RUN_JSON_LOCK:
        path = run_dir / "run.json"
        data = read_json(path, {}) or {}
        data.update(fields)
        data["updated_at"] = utc_now()
        write_json(path, data)


def decompose_tasks(prompt: str, run_dir: Path, config: dict) -> list[dict]:
    record = call_model(
        "codex",
        build_decompose_prompt(prompt, config),
        run_dir,
        "decompose",
        "tasks",
        timeout=600,
        effort=config["judge_effort"],
    )
    if not record["success"]:
        return fallback_tasks(prompt, config)
    try:
        payload = extract_json(record["stdout"])
    except ValueError:
        return fallback_tasks(prompt, config)
    return coerce_tasks(payload, prompt, config)


def parse_model_records(records: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    findings: list[dict] = []
    parse_rejections: list[dict] = []
    parsed_records: list[dict] = []

    for record in records:
        parsed = dict(record)
        parsed["parse_failed"] = False
        parsed["finding_count"] = 0
        parsed["no_sources"] = True
        parsed["parse_error"] = None

        if not record.get("success"):
            parsed["parse_failed"] = True
            parsed["parse_error"] = f"model_call_failed_rc_{record.get('rc')}"
            parse_rejections.append(
                {
                    "parse_failed": True,
                    "source_model": record.get("leg"),
                    "task_id": record.get("task_id"),
                    "record_id": record.get("record_id"),
                    "reasons": ["parse_failed"],
                    "raw_file": record.get("stdout_file"),
                }
            )
            parsed_records.append(parsed)
            continue

        try:
            payload = extract_json(record.get("stdout") or "")
            record_findings = coerce_findings(payload, record["leg"], record["task_id"], record["record_id"])
        except ValueError as exc:
            parsed["parse_failed"] = True
            parsed["parse_error"] = str(exc)
            record_findings = []
            parse_rejections.append(
                {
                    "parse_failed": True,
                    "source_model": record.get("leg"),
                    "task_id": record.get("task_id"),
                    "record_id": record.get("record_id"),
                    "reasons": ["parse_failed"],
                    "raw_file": record.get("stdout_file"),
                }
            )

        parsed["finding_count"] = len(record_findings)
        parsed["no_sources"] = not record_findings or all(not item.get("url") for item in record_findings)
        findings.extend(record_findings)
        parsed_records.append(parsed)

    return findings, parse_rejections, parsed_records


def write_model_stats(run_id: str, records: list[dict], rejected: list[dict]) -> None:
    rejected_by_record: dict[str, int] = {}
    for item in rejected:
        record_id = item.get("record_id")
        if record_id:
            rejected_by_record[record_id] = rejected_by_record.get(record_id, 0) + 1

    for record in records:
        append_jsonl(
            MODEL_STATS,
            {
                "ts": utc_now(),
                "run_id": run_id,
                "leg": record.get("leg"),
                "task_type": record.get("task_type"),
                "task_id": record.get("task_id"),
                "success": bool(record.get("success")),
                "parse_failed": bool(record.get("parse_failed")),
                "no_sources": bool(record.get("no_sources")),
                "rejected_count": rejected_by_record.get(record.get("record_id"), 0),
                "latency_sec": record.get("latency_sec"),
            },
        )


def collect_with_straggler_drop(futures: list, run_dir: Path, config: dict) -> list[dict]:
    """Collect fan-out results; once STRAGGLER_QUORUM of calls are in, give the rest a bounded
    grace window, then kill them. Killed calls return as failures and flow into rescue."""
    records: list[dict] = []
    latencies: list[float] = []
    deadline: float | None = None
    killed_once = False
    pending = set(futures)
    while pending:
        wait_timeout = max(1.0, deadline - time.monotonic()) if deadline is not None else None
        done, pending = concurrent.futures.wait(
            pending, timeout=wait_timeout, return_when=concurrent.futures.FIRST_COMPLETED
        )
        for future in done:
            record = future.result()
            records.append(record)
            latencies.append(record.get("latency_sec") or 0.0)
            update_run(run_dir, progress={"done": len(records), "total": len(futures)})
        if not pending:
            break
        quorum = max(1, math.ceil(len(futures) * STRAGGLER_QUORUM))
        if deadline is None and len(records) >= quorum:
            median = sorted(latencies)[len(latencies) // 2] if latencies else 0.0
            deadline = time.monotonic() + max(config["straggler_grace_sec"], median * 0.5)
        if deadline is not None and time.monotonic() >= deadline and not killed_once:
            kill_stragglers(run_dir.name)
            killed_once = True
            deadline = time.monotonic() + 30  # killed procs unwind within seconds; don't re-kill
    return records


def run_primary_search(prompt: str, tasks: list[dict], run_dir: Path, config: dict) -> list[dict]:
    jobs = []
    for task in tasks:
        for leg in ("codex", "gemini"):
            jobs.append((leg, task))

    timeout = min(RAW_TIMEOUT_SEC, config["search_timeout_sec"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_PRIMARY_WORKERS, len(jobs))) as executor:
        futures = [
            executor.submit(
                call_model,
                leg,
                build_search_prompt(prompt, task, leg, config),
                run_dir,
                "search",
                task["id"],
                timeout,
                config["search_effort"],
            )
            for leg, task in jobs
        ]
        return collect_with_straggler_drop(futures, run_dir, config)


def run_rechecks(
    prompt: str,
    items: list[dict],
    run_dir: Path,
    config: dict,
    round_no: int,
    attempts: dict[str, set[str]],
) -> tuple[list[dict], int]:
    """Rescue pass for rejected/disputed items. `attempts` maps canonical item key -> legs that
    already tried it, so each round can hand the item to a model that has NOT tried yet (the
    other vendor first, then the originating one). Returns (records, dropped_by_cap)."""
    jobs = []
    seen_keys: set[str] = set()
    items_used = 0
    dropped = 0
    for item in items:
        if not (item.get("disputed") or is_rescuable(item)):
            continue
        key = dedupe_key(item)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        tried = attempts.setdefault(key, set())
        untried = [
            leg for leg in SEARCH_LEGS
            if leg not in tried and not leg_disabled(run_dir.name, leg)
        ]
        if config["recheck_legs"] >= len(SEARCH_LEGS):
            legs = untried
        else:
            other = "gemini" if item.get("source_model") == "codex" else "codex"
            legs = [other] if other in untried else untried[:1]
        if not legs:
            continue
        if items_used >= config["max_recheck_items"]:
            dropped += 1
            continue
        items_used += 1
        for leg in legs:
            tried.add(leg)
            jobs.append((leg, item, f"recheck-{round_no}-{items_used}-{leg}"))
    if not jobs:
        return [], dropped

    timeout = min(RAW_TIMEOUT_SEC, config["recheck_timeout_sec"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(jobs))) as executor:
        futures = [
            executor.submit(
                call_model,
                leg,
                build_recheck_prompt(prompt, item, config),
                run_dir,
                "recheck",
                task_id,
                timeout,
                config["search_effort"],
            )
            for leg, item, task_id in jobs
        ]
        records = collect_with_straggler_drop(futures, run_dir, config)
    return records, dropped


def adjudicate_disputes(prompt: str, verified: list[dict], rejected: list[dict], run_dir: Path, config: dict) -> tuple[list[dict], list[dict]]:
    """Claude (thin arbiter, third vendor) settles items where codex and gemini still disagree."""
    disputed = [item for item in verified if item.get("disputed")][:MAX_ADJUDICATED_ITEMS]
    if not disputed:
        return verified, rejected

    def adjudicate_one(idx: int, item: dict) -> tuple[dict, dict]:
        record = call_model(
            "claude",
            build_adjudication_prompt(prompt, item, config),
            run_dir,
            "adjudicate",
            f"dispute-{idx}",
            timeout=600,
            claude_model=config["claude_model"],
        )
        return item, record

    results: list[tuple[dict, dict]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(disputed))) as executor:
        futures = [executor.submit(adjudicate_one, idx, item) for idx, item in enumerate(disputed, start=1)]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            update_run(run_dir, progress={"done": len(results), "total": len(disputed)})

    to_reject: list[str] = []
    for item, record in results:
        if not record["success"]:
            continue  # arbiter unavailable -> the item stays flagged disputed in the report
        try:
            verdict = extract_json(record["stdout"])
        except ValueError:
            continue
        if not isinstance(verdict, dict):
            continue
        if verdict.get("action") == "reject":
            item["adjudication"] = verdict.get("reason")
            to_reject.append(item.get("record_id"))
        elif verdict.get("action") == "accept":
            price = parse_price(verdict.get("price"))
            if price is not None:
                item["price"] = price
                if verdict.get("currency"):
                    item["currency"] = str(verdict["currency"])
            item["disputed"] = False
            item["adjudication"] = verdict.get("reason")

    still_verified = []
    for item in verified:
        if item.get("record_id") in to_reject and item.get("adjudication"):
            item["reasons"] = ["adjudicated_reject"]
            rejected.append(item)
        else:
            still_verified.append(item)
    return sort_by_price(still_verified), sort_by_price(rejected)


def adversarial_review(prompt: str, draft: str, verified: list[dict], rejected: list[dict], run_dir: Path, config: dict) -> str:
    """Cross-vendor refutation loop: each reviewer leg gets one round; codex revises on issues."""
    for round_no, reviewer in enumerate(config["review_legs"], start=1):
        review_record = call_model(
            reviewer,
            build_review_prompt(prompt, draft, verified, rejected, config),
            run_dir,
            "review",
            f"round-{round_no}-{reviewer}",
            timeout=700,
            effort=config["judge_effort"],
            claude_model=config["claude_model"],
        )
        if not review_record["success"]:
            continue  # reviewer leg unavailable -> skip the round, never block the run
        try:
            review = extract_json(review_record["stdout"])
        except ValueError:
            continue
        if not isinstance(review, dict) or review.get("verdict") == "approve":
            continue
        issues = [issue for issue in (review.get("issues") or []) if isinstance(issue, dict)]
        if not issues:
            continue
        revision_record = call_model(
            "codex",
            build_revision_prompt(prompt, draft, issues, config),
            run_dir,
            "revise",
            f"round-{round_no}",
            timeout=700,
            effort=config["judge_effort"],
        )
        if revision_record["success"] and revision_record.get("stdout", "").strip():
            draft = revision_record["stdout"].strip()
    return draft


def fallback_report(prompt: str, verified: list[dict], rejected: list[dict], degraded_legs: list[str] | None = None) -> str:
    lines = [
        "# Research result",
        "",
        f"Prompt: {prompt}",
        "",
    ]
    if degraded_legs:
        lines.extend([f"> WARNING: model leg(s) unavailable during this run: {', '.join(degraded_legs)}. Coverage may be incomplete.", ""])
    lines.extend([
        "## Verified options",
        "",
    ])
    if not verified:
        lines.append("No verified purchasable offers survived URL, price, and stock checks.")
    for idx, item in enumerate(verified[:12], start=1):
        price = item.get("price")
        currency = item.get("currency") or ""
        lines.append(f"{idx}. [{item.get('title') or 'Untitled'}]({item.get('url')}) - {price:g} {currency}".strip())
        details = ", ".join(str(x) for x in [item.get("marketplace"), item.get("location"), item.get("availability")] if x)
        if details:
            lines.append(f"   {details}")
    unconfirmed = [item for item in rejected if is_rescuable(item)]
    if unconfirmed:
        lines.extend(["", "## Unverified — check manually", ""])
        for item in unconfirmed[:8]:
            price = f"{item.get('price'):g} {item.get('currency') or ''}".strip() if item.get("price") else "no price"
            lines.append(f"- [{item.get('title') or 'Untitled'}]({item.get('url')}) - {price} ({', '.join(item.get('reasons', []))})")
    lines.extend(["", "## Rejected or risky items", "", f"Rejected count: {len(rejected)}"])
    reason_counts: dict[str, int] = {}
    for item in rejected:
        for reason in item.get("reasons", []):
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    for reason, count in sorted(reason_counts.items()):
        lines.append(f"- {reason}: {count}")
    return "\n".join(lines) + "\n"


def synthesize_report(prompt: str, tasks: list[dict], verified: list[dict], rejected: list[dict], run_dir: Path, config: dict) -> str:
    degraded = disabled_legs(run_dir.name)
    if not verified:
        return fallback_report(prompt, verified, rejected, degraded)
    # The judge seat is the run's whole value: if codex is down (quota), another vendor takes
    # the seat rather than dumping an unranked fallback list on the user.
    for judge in ("codex", "claude", "gemini"):
        record = call_model(
            judge,
            build_synthesis_prompt(prompt, tasks, verified, rejected, config, degraded),
            run_dir,
            "synthesize",
            f"final-{judge}",
            timeout=700,
            effort=config["judge_effort"],
            claude_model=config["claude_model"],
            bypass_breaker=True,  # always attempt each judge once, even past the breaker
        )
        if record["success"] and record.get("stdout", "").strip():
            return record["stdout"].strip() + "\n"
    return fallback_report(prompt, verified, rejected, degraded)


def execute_research(run_dir: Path, prompt: str, config: dict) -> None:
    run_id = run_dir.name
    sites = config.get("sites") or []
    all_parsed_records: list[dict] = []
    ACTIVE_RUNS.add(run_id)
    init_leg_health(run_id)
    init_leg_budget(run_id, {"gemini": config["gemini_call_budget"]})
    try:
        update_run(run_dir, status="running", phase="decomposing", progress=None)
        tasks = decompose_tasks(prompt, run_dir, config)
        write_json(run_dir / "tasks.json", {"tasks": tasks})

        update_run(run_dir, phase="primary_search", progress=None)
        primary_records = run_primary_search(prompt, tasks, run_dir, config)
        findings, parse_rejections, parsed_records = parse_model_records(primary_records)
        all_parsed_records.extend(parsed_records)
        write_json(run_dir / "findings.json", {"stage": "primary", "findings": findings, "parse_rejections": parse_rejections})

        update_run(run_dir, phase="verifying", progress=None)
        verified, rejected = verify_findings(findings, parse_rejections, sites)
        write_json(run_dir / "verification.json", {"stage": "primary", "verified": verified, "rejected": rejected})

        rescue_attempts: dict[str, set[str]] = {}
        recheck_dropped = 0
        for round_no in range(1, config["recheck_rounds"] + 1):
            update_run(run_dir, phase=f"rechecking_{round_no}", progress=None)
            disputed = [item for item in verified if item.get("disputed")]
            recheck_records, dropped = run_rechecks(prompt, rejected + disputed, run_dir, config, round_no, rescue_attempts)
            recheck_dropped += dropped
            if not recheck_records:
                break
            recheck_findings, recheck_parse_rejections, recheck_parsed = parse_model_records(recheck_records)
            all_parsed_records.extend(recheck_parsed)
            findings = findings + recheck_findings
            parse_rejections = parse_rejections + recheck_parse_rejections
            verified, rejected = verify_findings(findings, parse_rejections, sites)
        if recheck_dropped:
            # No silent caps: the report and UI must show how many candidates the budget skipped.
            update_run(run_dir, recheck_dropped=recheck_dropped)

        if config["adjudicate_disputes"] and any(item.get("disputed") for item in verified):
            update_run(run_dir, phase="adjudicating", progress=None)
            verified, rejected = adjudicate_disputes(prompt, verified, rejected, run_dir, config)

        write_json(run_dir / "findings.json", {"stage": "final", "findings": dedupe_findings(findings), "parse_rejections": parse_rejections})
        write_json(run_dir / "verification.json", {"stage": "final", "verified": verified, "rejected": rejected})
        write_model_stats(run_id, all_parsed_records, rejected)

        update_run(run_dir, phase="synthesizing", progress=None)
        report = synthesize_report(prompt, tasks, verified, rejected, run_dir, config)

        if config["review_legs"]:
            update_run(run_dir, phase="reviewing", progress=None)
            report = adversarial_review(prompt, report, verified, rejected, run_dir, config)

        (run_dir / "final.md").write_text(report, encoding="utf-8")

        update_run(
            run_dir,
            status="completed",
            phase="completed",
            progress=None,
            verified_count=len(verified),
            rejected_count=len(rejected),
            disputed_count=sum(1 for item in verified if item.get("disputed")),
            degraded_legs=disabled_legs(run_id) or None,
            final_path=str((run_dir / "final.md").relative_to(ROOT)),
        )
    except Exception as exc:
        update_run(run_dir, status="failed", phase="failed", error=str(exc), traceback=traceback.format_exc())
        (run_dir / "final.md").write_text(f"# Research failed\n\n{exc}\n", encoding="utf-8")
    finally:
        ACTIVE_RUNS.discard(run_id)
        clear_leg_health(run_id)
        clear_leg_budget(run_id)
        clear_run_registry(run_id)


def run_research(prompt: str, config: dict | None = None) -> Path:
    config = config or make_config()
    run_dir = init_run(prompt, config)
    execute_research(run_dir, prompt, config)
    return run_dir


def parse_iso_ts(value: object) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def refresh_stale_status(run_dir: Path, meta: dict) -> dict:
    """A 'running' run with no heartbeat past STALE_AFTER_SEC (and not owned by this process)
    died with its server — mark it failed instead of showing 'running' forever."""
    if meta.get("status") not in {"queued", "running"} or run_dir.name in ACTIVE_RUNS:
        return meta
    updated = parse_iso_ts(meta.get("updated_at") or meta.get("created_at"))
    if updated is None:
        return meta
    age = (dt.datetime.now(dt.timezone.utc) - updated).total_seconds()
    if age <= STALE_AFTER_SEC:
        return meta
    update_run(run_dir, status="failed", phase="stale", error=f"stale run: no heartbeat for {int(age)}s")
    return read_json(run_dir / "run.json", meta) or meta


def list_runs() -> list[dict]:
    if not RUNS_DIR.exists():
        return []
    rows = []
    for path in sorted(RUNS_DIR.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        meta = refresh_stale_status(path, read_json(path / "run.json", {}) or {})
        rows.append(
            {
                "run_id": path.name,
                "status": meta.get("status"),
                "phase": meta.get("phase"),
                "prompt": meta.get("prompt"),
                "config": meta.get("config"),
                "created_at": meta.get("created_at"),
                "verified_count": meta.get("verified_count"),
                "rejected_count": meta.get("rejected_count"),
            }
        )
    return rows


def collect_run_payload(run_id: str) -> dict:
    safe_run_id = Path(run_id).name
    run_dir = RUNS_DIR / safe_run_id
    if not run_dir.exists():
        raise FileNotFoundError(run_id)
    verification = read_json(run_dir / "verification.json", {}) or {}
    tasks = read_json(run_dir / "tasks.json", {}) or {}
    return {
        "run": refresh_stale_status(run_dir, read_json(run_dir / "run.json", {}) or {}),
        "tasks": tasks.get("tasks", []) if isinstance(tasks, dict) else [],
        "verified": verification.get("verified", []) if isinstance(verification, dict) else [],
        "rejected": verification.get("rejected", []) if isinstance(verification, dict) else [],
        "final_url": f"/api/runs/{safe_run_id}/final.md" if (run_dir / "final.md").exists() else None,
    }


def start_background_run(prompt: str, config: dict) -> str:
    run_dir = init_run(prompt, config)
    ACTIVE_RUNS.add(run_dir.name)
    update_run(run_dir, status="running", phase="starting")
    thread = threading.Thread(target=execute_research, args=(run_dir, prompt, config), daemon=True)
    thread.start()
    return run_dir.name


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Multi-model price research</title>
  <style>
    :root { color-scheme: light; --ink: #1f2933; --muted: #667085; --line: #d9e2ec; --bg: #f7f9fb; --panel: #ffffff; --accent: #0f766e; --bad: #b42318; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); }
    header { padding: 20px 24px 12px; border-bottom: 1px solid var(--line); background: var(--panel); }
    h1 { margin: 0; font-size: 22px; font-weight: 700; letter-spacing: 0; }
    main { max-width: 1120px; margin: 0 auto; padding: 20px; display: grid; gap: 18px; }
    form { display: grid; gap: 10px; }
    .controls { display: grid; grid-template-columns: 1fr 1.4fr auto; gap: 12px; align-items: end; }
    .control { display: grid; gap: 4px; }
    .control-label { font-size: 13px; color: var(--muted); }
    .control input[type="text"] { padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; font: inherit; color: var(--ink); background: var(--panel); }
    .control input[type="range"] { width: 100%; accent-color: var(--accent); }
    .badge { display: inline-block; margin: 2px 0 6px; padding: 2px 8px; border-radius: 999px; background: #fef0c7; color: #93370d; font-size: 12px; font-weight: 650; }
    .history { display: grid; gap: 6px; }
    .history-row { text-align: left; border: 1px solid var(--line); background: #fbfdff; color: var(--ink); border-radius: 8px; padding: 8px 12px; font: inherit; font-size: 13px; cursor: pointer; min-height: 0; font-weight: 400; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .history-row.active { border-color: var(--accent); background: #ecfdf5; }
    textarea { width: 100%; min-height: 92px; resize: vertical; padding: 12px; border: 1px solid var(--line); border-radius: 8px; font: inherit; color: var(--ink); background: var(--panel); }
    button { min-height: 44px; padding: 0 18px; border: 0; border-radius: 8px; background: var(--accent); color: white; font: inherit; font-weight: 650; cursor: pointer; }
    button:disabled { opacity: 0.55; cursor: default; }
    section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
    .status { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; color: var(--muted); }
    .pill { border: 1px solid var(--line); border-radius: 999px; padding: 4px 10px; color: var(--ink); background: #fbfdff; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
    article { border: 1px solid var(--line); border-radius: 8px; padding: 12px; min-width: 0; }
    article h3 { margin: 0 0 8px; font-size: 15px; line-height: 1.35; letter-spacing: 0; }
    article a { color: var(--accent); overflow-wrap: anywhere; }
    .price { font-size: 18px; font-weight: 750; margin: 6px 0; }
    .muted { color: var(--muted); font-size: 13px; line-height: 1.45; }
    .bad { color: var(--bad); }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #111827; color: #eef2ff; padding: 14px; border-radius: 8px; max-height: 520px; overflow: auto; }
    @media (max-width: 680px) { .controls { grid-template-columns: 1fr; } button { width: 100%; } main { padding: 12px; } }
  </style>
</head>
<body>
  <header><h1>Multi-model research</h1></header>
  <main>
    <section>
      <form id="runForm">
        <textarea id="prompt" placeholder="Find the cheapest MacBook Air M2 in Ukraine with working links"></textarea>
        <div class="controls">
          <label class="control">
            <span class="control-label">Restrict to site (optional)</span>
            <input id="site" type="text" placeholder="olx.ua, prom.ua">
          </label>
          <label class="control effort">
            <span class="control-label">Effort: <strong id="effortName"></strong></span>
            <input id="effort" type="range" min="1" max="4" step="1" value="2">
            <span class="muted" id="effortHint"></span>
          </label>
          <button id="submit" type="submit">Start</button>
        </div>
      </form>
    </section>
    <section>
      <div class="status">
        <span class="pill" id="runId">No run</span>
        <span class="pill" id="status">Idle</span>
        <span class="pill" id="progress" hidden></span>
        <span class="pill bad" id="legHealth" hidden></span>
        <span class="pill" id="runConfig" hidden></span>
        <span class="pill" id="counts">0 verified / 0 rejected</span>
      </div>
    </section>
    <section>
      <h2>Run history</h2>
      <div id="history" class="history"></div>
    </section>
    <section>
      <h2>Verified</h2>
      <div id="verified" class="grid"></div>
    </section>
    <section>
      <h2>Rejected / unverified</h2>
      <div id="rejected" class="grid"></div>
    </section>
    <section>
      <h2>Final report</h2>
      <pre id="final">No report yet.</pre>
    </section>
  </main>
  <script>
    const EFFORTS = {
      1: { name: 'Quick', hint: '3 tasks x 2 models, 1 rescue round, Sonnet adjudicates disputes' },
      2: { name: 'Standard', hint: '4 tasks x 2 models, 1 rescue round, Sonnet adjudication' },
      3: { name: 'Deep', hint: '5 tasks x 2 models, 2 rescue rounds (second model retries failures), Gemini adversarial review, Opus adjudication' },
      4: { name: 'Max', hint: '6 tasks x 2 models at max reasoning, 2 rescue rounds with BOTH models per item, Gemini+Opus reviews, Opus adjudication' }
    };
    const form = document.getElementById('runForm');
    const promptEl = document.getElementById('prompt');
    const siteEl = document.getElementById('site');
    const effortEl = document.getElementById('effort');
    const effortNameEl = document.getElementById('effortName');
    const effortHintEl = document.getElementById('effortHint');
    const submit = document.getElementById('submit');
    const runIdEl = document.getElementById('runId');
    const statusEl = document.getElementById('status');
    const progressEl = document.getElementById('progress');
    const legHealthEl = document.getElementById('legHealth');
    const runConfigEl = document.getElementById('runConfig');
    const countsEl = document.getElementById('counts');
    const historyEl = document.getElementById('history');
    const verifiedEl = document.getElementById('verified');
    const rejectedEl = document.getElementById('rejected');
    const finalEl = document.getElementById('final');
    let currentRun = localStorage.getItem('currentRun') || null;
    let pollTimer = null;

    function syncEffortLabel() {
      const effort = EFFORTS[effortEl.value] || EFFORTS[2];
      effortNameEl.textContent = effortEl.value + ' - ' + effort.name;
      effortHintEl.textContent = effort.hint;
    }
    effortEl.addEventListener('input', syncEffortLabel);
    syncEffortLabel();

    function itemCard(item, rejected) {
      const article = document.createElement('article');
      const title = document.createElement('h3');
      title.textContent = item.title || 'Untitled';
      article.appendChild(title);
      const price = document.createElement('div');
      price.className = 'price';
      price.textContent = item.price ? `${item.price} ${item.currency || ''}` : 'No price';
      article.appendChild(price);
      if (item.disputed) {
        const disputed = document.createElement('span');
        disputed.className = 'badge';
        disputed.textContent = 'disputed: ' + (item.price_candidates || []).map(c => c.price).join(' vs ');
        article.appendChild(disputed);
      }
      if (item.url) {
        const link = document.createElement('a');
        link.href = item.url;
        link.target = '_blank';
        link.rel = 'noreferrer';
        link.textContent = item.url;
        article.appendChild(link);
      }
      const meta = document.createElement('div');
      meta.className = 'muted';
      meta.textContent = [item.marketplace, item.location, item.availability].filter(Boolean).join(' | ');
      article.appendChild(meta);
      if (item.adjudication) {
        const note = document.createElement('div');
        note.className = 'muted';
        note.textContent = 'arbiter: ' + item.adjudication;
        article.appendChild(note);
      }
      if (rejected) {
        const reasons = document.createElement('div');
        reasons.className = 'muted bad';
        reasons.textContent = (item.reasons || []).join(', ');
        article.appendChild(reasons);
      }
      return article;
    }

    function renderList(node, items, rejected) {
      node.replaceChildren();
      if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'muted';
        empty.textContent = 'Nothing yet.';
        node.appendChild(empty);
        return;
      }
      items.slice(0, 12).forEach(item => node.appendChild(itemCard(item, rejected)));
    }

    function describeConfig(config) {
      if (!config) return '';
      const parts = [];
      if (config.effort) parts.push('effort: ' + config.effort);
      if (config.sites && config.sites.length) parts.push('sites: ' + config.sites.join(', '));
      return parts.join(' | ');
    }

    async function loadHistory() {
      const res = await fetch('/api/runs');
      if (!res.ok) return;
      const data = await res.json();
      historyEl.replaceChildren();
      (data.runs || []).slice(0, 15).forEach(run => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'history-row' + (run.run_id === currentRun ? ' active' : '');
        const text = (run.prompt || '').replace(/\s+/g, ' ').slice(0, 80);
        const stats = `${run.status || '?'} | ${run.verified_count ?? 0} ok`;
        row.textContent = `${(run.created_at || '').slice(0, 16)}  ${text}  [${stats}]`;
        row.addEventListener('click', () => { watchRun(run.run_id); });
        historyEl.appendChild(row);
      });
      if (!historyEl.children.length) {
        const empty = document.createElement('div');
        empty.className = 'muted';
        empty.textContent = 'No runs yet.';
        historyEl.appendChild(empty);
      }
    }

    function watchRun(runId) {
      currentRun = runId;
      localStorage.setItem('currentRun', currentRun);
      finalEl.textContent = 'Loading...';
      refresh();
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(refresh, 2000);
      loadHistory();
    }

    async function refresh() {
      if (!currentRun) return;
      const res = await fetch(`/api/runs/${encodeURIComponent(currentRun)}`);
      if (!res.ok) return;
      const data = await res.json();
      const run = data.run || {};
      runIdEl.textContent = run.run_id || currentRun;
      statusEl.textContent = `${run.status || 'unknown'} / ${run.phase || 'unknown'}`;
      const progress = run.progress;
      progressEl.hidden = !(progress && progress.total);
      if (progress && progress.total) progressEl.textContent = `calls: ${progress.done}/${progress.total}`;
      const downLegs = run.degraded_legs
        || Object.entries(run.leg_health || {}).filter(([, h]) => h.disabled).map(([leg]) => leg);
      legHealthEl.hidden = !downLegs.length;
      if (downLegs.length) legHealthEl.textContent = `model down: ${downLegs.join(', ')}`;
      let configText = describeConfig(run.config);
      if (run.recheck_dropped) configText += ` | rescue budget skipped: ${run.recheck_dropped}`;
      runConfigEl.hidden = !configText;
      runConfigEl.textContent = configText;
      countsEl.textContent = `${data.verified.length} verified / ${data.rejected.length} rejected`;
      renderList(verifiedEl, data.verified || [], false);
      renderList(rejectedEl, data.rejected || [], true);
      if (data.final_url) {
        const finalRes = await fetch(data.final_url);
        finalEl.textContent = await finalRes.text();
      }
      if (run.status === 'completed' || run.status === 'failed') {
        submit.disabled = false;
        if (pollTimer) clearInterval(pollTimer);
        loadHistory();
      }
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const prompt = promptEl.value.trim();
      if (!prompt) return;
      submit.disabled = true;
      finalEl.textContent = 'Running...';
      const res = await fetch('/api/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, effort: Number(effortEl.value), sites: siteEl.value })
      });
      const data = await res.json();
      watchRun(data.run_id);
    });

    loadHistory();
    if (currentRun) {
      refresh();
      pollTimer = setInterval(refresh, 2000);
    }
  </script>
</body>
</html>
"""


class ResearchHandler(http.server.BaseHTTPRequestHandler):
    server_version = "MultiModelResearch/1.0"

    def send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, status: int, body: str, content_type: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_text(200, INDEX_HTML, "text/html; charset=utf-8")
            return

        if path == "/api/runs":
            self.send_json(200, {"runs": list_runs()})
            return

        match = re.fullmatch(r"/api/runs/([^/]+)", path)
        if match:
            try:
                self.send_json(200, collect_run_payload(urllib.parse.unquote(match.group(1))))
            except FileNotFoundError:
                self.send_json(404, {"error": "run_not_found"})
            return

        match = re.fullmatch(r"/api/runs/([^/]+)/final\.md", path)
        if match:
            run_id = Path(urllib.parse.unquote(match.group(1))).name
            final_path = RUNS_DIR / run_id / "final.md"
            if final_path.exists():
                self.send_text(200, final_path.read_text(encoding="utf-8"), "text/markdown; charset=utf-8")
            else:
                self.send_json(404, {"error": "final_not_found"})
            return

        self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/runs":
            self.send_json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            prompt = str(payload.get("prompt") or "").strip()
            if not prompt:
                self.send_json(400, {"error": "prompt_required"})
                return
            config = make_config(payload.get("effort"), payload.get("sites"))
            run_id = start_background_run(prompt, config)
            self.send_json(202, {"run_id": run_id})
        except json.JSONDecodeError:
            self.send_json(400, {"error": "invalid_json"})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))


def serve(host: str, port: int) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    server = http.server.ThreadingHTTPServer((host, port), ResearchHandler)
    print(f"Serving local UI at http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.", file=sys.stderr)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local multi-model offer research.")
    parser.add_argument("prompt", nargs="*", help="Research prompt.")
    parser.add_argument(
        "--effort",
        default=None,
        help="Research effort: 1-4 or quick/standard/deep/max (default: standard).",
    )
    parser.add_argument(
        "--site",
        action="append",
        default=None,
        help="Restrict research to this domain (repeatable or comma-separated).",
    )
    parser.add_argument("--serve", action="store_true", help="Start the local Web UI.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for --serve.")
    parser.add_argument("--port", type=int, default=8765, help="Port for --serve.")
    parser.add_argument("--list-runs", action="store_true", help="List previous runs.")
    args = parser.parse_args(argv)

    if args.list_runs:
        for row in list_runs():
            prompt = (row.get("prompt") or "").replace("\n", " ")
            print(
                f"{row.get('run_id')}  {row.get('status')}/{row.get('phase')}  "
                f"{row.get('verified_count') or 0} verified  {html.escape(prompt[:90])}"
            )
        return 0

    if args.serve:
        serve(args.host, args.port)
        return 0

    prompt = " ".join(args.prompt).strip()
    if not prompt:
        parser.error("prompt is required unless --serve or --list-runs is used")

    config = make_config(args.effort, ",".join(args.site) if args.site else None)
    run_dir = run_research(prompt, config)
    meta = read_json(run_dir / "run.json", {}) or {}
    print(f"run_id: {run_dir.name}")
    print(f"status: {meta.get('status')} / {meta.get('phase')}")
    print(f"artifacts: {run_dir}")
    final_path = run_dir / "final.md"
    if final_path.exists():
        print("")
        print(final_path.read_text(encoding="utf-8"))
    return 0 if meta.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
