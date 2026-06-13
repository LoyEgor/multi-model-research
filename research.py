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
import shutil
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
SERVED_MODELS = DATA_DIR / "served-models.jsonl"
# Quota-aware pacing: soft daily caps per leg (calls/day, UTC). A run shrinks its per-run budget
# to the remaining daily allowance so one run can't exhaust the day. Generous defaults; override
# with RESEARCH_<LEG>_DAILY_CAP. The subscription legs are "free per call" but rate/quota-limited.
DAILY_CAPS = {
    "gemini": env_int("RESEARCH_GEMINI_DAILY_CAP", 200),
    "codex": env_int("RESEARCH_CODEX_DAILY_CAP", 400),
    "claude": env_int("RESEARCH_CLAUDE_DAILY_CAP", 80),
}
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
# Claude's subscription pool is the most capped — keep it to one concurrent call by default.
CLAUDE_CONCURRENCY = threading.Semaphore(max(1, env_int("RESEARCH_CLAUDE_CONCURRENCY", 1)))
# Only the scarce-quota legs get a per-leg concurrency cap. Codex is intentionally absent — its
# pool tolerates the fan-out, and total concurrency is already bounded by MAX_PRIMARY_WORKERS.
LEG_SEMAPHORES = {"gemini": GEMINI_CONCURRENCY, "claude": CLAUDE_CONCURRENCY}
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


def refund_leg_budget(run_id: str, leg: str) -> None:
    """Give back a budget slot reserved for a call that never actually ran (e.g. cancelled after
    the slot was consumed) so the rest of the run isn't shortchanged."""
    with LEG_BUDGET_LOCK:
        budgets = RUN_LEG_BUDGET.get(run_id)
        if budgets is not None and leg in budgets:
            budgets[leg] += 1


def clear_leg_budget(run_id: str) -> None:
    with LEG_BUDGET_LOCK:
        RUN_LEG_BUDGET.pop(run_id, None)


def daily_call_counts(day: str | None = None) -> dict[str, int]:
    """Count successful leg calls logged today (UTC) in served-models.jsonl — the basis for
    quota-aware pacing. day defaults to today's UTC date (YYYY-MM-DD)."""
    day = day or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    counts: dict[str, int] = {}
    try:
        with SERVED_MODELS.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or day not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("ts", "")).startswith(day):
                    leg = row.get("leg")
                    if leg:
                        counts[leg] = counts.get(leg, 0) + 1
    except OSError:
        pass
    return counts


def paced_budget(leg: str, requested: int) -> tuple[int, int]:
    """Clamp a leg's per-run budget to the remaining daily allowance. Returns (budget, remaining)."""
    cap = DAILY_CAPS.get(leg)
    if not cap:
        return requested, -1
    used = daily_call_counts().get(leg, 0)
    remaining = max(0, cap - used)
    return min(requested, remaining), remaining


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


def kill_one_call(run_id: str, record_id: str) -> bool:
    """Kill a single in-flight model call (the UI's per-call ✕). Its item flows into rescue,
    exactly like a straggler — the user no longer has to wait on one slow leg."""
    with PROC_REGISTRY_LOCK:
        pid = (RUN_PROCS.get(run_id) or {}).get(record_id)
    if pid is None:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        return False
    with PROC_REGISTRY_LOCK:
        RUN_DROPPED.setdefault(run_id, set()).add(record_id)
    return True


def was_dropped_as_straggler(run_id: str, record_id: str) -> bool:
    with PROC_REGISTRY_LOCK:
        return record_id in RUN_DROPPED.get(run_id, set())


def clear_run_registry(run_id: str) -> None:
    with PROC_REGISTRY_LOCK:
        RUN_PROCS.pop(run_id, None)
        RUN_DROPPED.pop(run_id, None)


# Run cancellation: the UI's cancel button sets the flag and kills in-flight subprocesses;
# the pipeline notices between phases (and before spawning any new call) and finishes with a
# partial report instead of dying mid-write.
CANCEL_LOCK = threading.Lock()
CANCELLED_RUNS: set[str] = set()


class RunCancelled(Exception):
    pass


def run_cancelled(run_id: str) -> bool:
    with CANCEL_LOCK:
        return run_id in CANCELLED_RUNS


def request_cancel(run_id: str) -> bool:
    """Returns False when the run is not active in this process."""
    if run_id not in ACTIVE_RUNS:
        return False
    with CANCEL_LOCK:
        CANCELLED_RUNS.add(run_id)
    kill_stragglers(run_id)  # abort in-flight model calls; queued ones are blocked at spawn
    return True


def clear_cancel(run_id: str) -> None:
    with CANCEL_LOCK:
        CANCELLED_RUNS.discard(run_id)


# Vendors the user switched OFF for this run (to save that provider's quota). Role selection
# already avoids them; this is the belt-and-suspenders guard so no stray call ever reaches one.
USER_DISABLED: dict[str, set] = {}


def set_user_disabled(run_id: str, legs) -> None:
    with CANCEL_LOCK:
        USER_DISABLED[run_id] = set(legs or [])


def user_disabled(run_id: str, leg: str) -> bool:
    with CANCEL_LOCK:
        return leg in USER_DISABLED.get(run_id, set())


def clear_user_disabled(run_id: str) -> None:
    with CANCEL_LOCK:
        USER_DISABLED.pop(run_id, None)

# Effort scales breadth (tasks), depth (recheck rounds/items) and verification layers (cross-vendor
# adversarial review of the draft, Claude adjudication of disputes). The search legs (codex/gemini)
# NEVER downgrade — flagship only, tier guards in lib/legs/ask_*.sh stay in force. The Claude seat is
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
        "search_legs": ["codex", "gemini"],
        "claude_search_budget": 0,
        "query_variants_per_task": 1,
        "frontier_rounds": 0,
        "final_factcheck": False,
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
        "search_legs": ["codex", "gemini"],
        "claude_search_budget": 0,
        "query_variants_per_task": 1,
        "frontier_rounds": 0,
        "final_factcheck": False,
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
        "search_legs": ["codex", "gemini", "claude"],
        "claude_search_budget": 6,
        "query_variants_per_task": 2,
        "frontier_rounds": 1,
        "final_factcheck": True,
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
        "search_legs": ["codex", "gemini", "claude"],
        "claude_search_budget": 10,
        "query_variants_per_task": 3,
        "frontier_rounds": 2,
        "final_factcheck": True,
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


ALL_VENDORS = ("codex", "gemini", "claude")
VENDOR_ALIASES = {
    "gpt": "codex", "openai": "codex", "chatgpt": "codex", "codex": "codex",
    "google": "gemini", "gemini": "gemini",
    "anthropic": "claude", "claude": "claude", "opus": "claude", "sonnet": "claude",
}


def normalize_vendors(values: object) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,\s]+", values)
    out = []
    for v in values or []:
        canon = VENDOR_ALIASES.get(str(v).strip().lower())
        if canon and canon not in out:
            out.append(canon)
    return out


def make_config(effort: object = None, sites: object = None, disabled: object = None) -> dict:
    level = parse_effort(effort)
    config = dict(EFFORT_PROFILES[level])
    config["effort_level"] = level
    config["sites"] = normalize_sites(sites)

    # Vendor on/off (owner saves a provider's quota): drop disabled vendors from every role.
    # At least one vendor must remain — if the user disables all three, ignore the request.
    disabled_set = set(normalize_vendors(disabled))
    enabled = [v for v in ALL_VENDORS if v not in disabled_set]
    if not enabled:
        enabled = list(ALL_VENDORS)
        disabled_set = set()
    config["enabled_legs"] = enabled
    config["disabled_legs"] = sorted(disabled_set)
    # Filter the effort profile's leg lists; ensure search always has at least one enabled leg.
    config["search_legs"] = [l for l in config.get("search_legs", []) if l in enabled] or list(enabled)
    config["review_legs"] = [l for l in config.get("review_legs", []) if l in enabled]

    # Explicit env knobs still override the profile (documented in README).
    if os.environ.get("RESEARCH_MAX_TASKS") is not None:
        config["task_count"] = min(6, max(3, env_int("RESEARCH_MAX_TASKS", config["task_count"])))
    if os.environ.get("RESEARCH_MAX_RECHECK_ITEMS") is not None:
        config["max_recheck_items"] = max(0, env_int("RESEARCH_MAX_RECHECK_ITEMS", config["max_recheck_items"]))
    return config


def judge_chain(config: dict) -> list[str]:
    """Synthesis judge fallback order, restricted to enabled vendors (codex preferred)."""
    enabled = config.get("enabled_legs") or list(ALL_VENDORS)
    return [v for v in ("codex", "claude", "gemini") if v in enabled]


def judge_vendor(config: dict) -> str:
    """The single 'thin brain' vendor for decompose/revision — first enabled judge."""
    return (judge_chain(config) or ["codex"])[0]


def arbiter_vendor(config: dict) -> str:
    """Independent arbiter for dispute adjudication — prefer Claude, else any enabled vendor."""
    enabled = config.get("enabled_legs") or list(ALL_VENDORS)
    for v in ("claude", "codex", "gemini"):
        if v in enabled:
            return v
    return "codex"


def vendor_claude_model(vendor: str, config: dict) -> str | None:
    return config.get("claude_model") if vendor == "claude" else None

FINDING_FIELDS = [
    "title",
    "price",
    "currency",
    "url",
    "marketplace",
    "availability",
    "condition",
    "tier",
    "seller",
    "location",
    "shipping",
    "evidence",
    "confidence",
    "source_model",
    "checked_at",
]

# Static FX → USD. Models report price in the listing's NATIVE currency; ONE table converts so
# everything is comparable and rankable in USD (the owner wants USD; UAH may also stay shown).
# Approximate, mid-2026 levels — override any rate via env RESEARCH_FX_<CUR>=<units_per_usd>.
# This is deliberately a static table (no network dependency); refresh the constants periodically.
FX_PER_USD = {
    "USD": 1.0, "UAH": 41.5, "EUR": 0.92, "GBP": 0.79, "RUB": 92.0,
    "PLN": 4.0, "KZT": 470.0, "TRY": 34.0, "GEL": 2.7, "BYN": 3.3,
}
CURRENCY_ALIASES = {
    "$": "USD", "usd": "USD", "дол": "USD", "долл": "USD",
    "грн": "UAH", "uah": "UAH", "₴": "UAH", "гривень": "UAH", "гривен": "UAH",
    "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP",
    "руб": "RUB", "rub": "RUB", "₽": "RUB", "zł": "PLN", "pln": "PLN", "тг": "KZT", "kzt": "KZT",
}


def canon_currency(currency: object) -> str | None:
    text = str(currency or "").strip().lower()
    if not text:
        return None
    if text.upper() in FX_PER_USD:
        return text.upper()
    for alias, code in CURRENCY_ALIASES.items():
        if alias in text:
            return code
    return None


def fx_rate(code: str) -> float | None:
    env = os.environ.get(f"RESEARCH_FX_{code}")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return FX_PER_USD.get(code)


def to_usd(price: object, currency: object) -> float | None:
    if price is None:
        return None
    code = canon_currency(currency)
    if code is None:
        return None
    rate = fx_rate(code)
    if not rate:
        return None
    return round(float(price) / rate, 2)

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
    # Grey-market digital-goods marketplaces (the Plati run surfaced these). Canonicalizing by
    # the numeric item id lets the same listing dedup across slug/locale variants and lets the
    # dispute/adjudication path fire (HANDOFF: Plati had no key → no dedup, double-counted items).
    ("plati", re.compile(r"(?:^|\.)plati\.(?:market|ru|com)/.*?(\d{5,})", re.IGNORECASE)),
    ("digiseller", re.compile(r"(?:^|\.)(?:digiseller\.market|ggsel\.net|ggsel\.com)/(?:[a-z]{2,3}/)?.*?(\d{5,})", re.IGNORECASE)),
    ("funpay", re.compile(r"(?:^|\.)funpay\.(?:com|ru)/(?:[a-z]{2}/)?lots/offer\?id=(\d+)", re.IGNORECASE)),
]


def listing_key(url: object) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlsplit(str(url).strip())
    # Include the query: some marketplaces (FunPay) carry the listing id in ?id=...
    flat = parsed.netloc.lower() + parsed.path + (("?" + parsed.query) if parsed.query else "")
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
    # Canonicalize the native currency; default missing currency to UAH only as a last resort
    # (the system started on Ukrainian shopping). USD is computed once, here, from the FX table.
    finding["currency"] = canon_currency(finding.get("currency")) or (
        "UAH" if finding["price"] is not None else None
    )
    finding["price_usd"] = to_usd(finding["price"], finding["currency"])
    if finding.get("tier") is not None:
        finding["tier"] = str(finding["tier"]).strip().lower() or None

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
        [{"price": finding["price"], "price_usd": finding["price_usd"], "currency": finding["currency"], "source_model": source_model}]
        if finding["price"] is not None else []
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

    # "Cheaper" is decided in USD (comparable across currencies), then native price/currency
    # follow the chosen candidate.
    if incoming.get("price") is not None:
        inc_usd = incoming.get("price_usd")
        cur_usd = existing.get("price_usd")
        if existing.get("price") is None or (inc_usd is not None and (cur_usd is None or inc_usd < cur_usd)):
            existing["price"] = incoming["price"]
            existing["currency"] = incoming.get("currency")
            existing["price_usd"] = inc_usd

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
    # Cross-leg disagreement on the same canonical item, compared in USD: keep it, flag it, let
    # the recheck / adjudication stages resolve it by verified fact (never average, never vote).
    prices = sorted(c.get("price_usd") for c in candidates if c.get("price_usd"))
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
LDJSON_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)
OG_PRICE_RE = re.compile(r'<meta[^>]+(?:og:price:amount|product:price:amount)[^>]+content="([0-9][0-9.,\s]*)"', re.IGNORECASE)
OG_CUR_RE = re.compile(r'<meta[^>]+(?:og:price:currency|product:price:currency)[^>]+content="([A-Za-z₴$€£]{1,4})"', re.IGNORECASE)
TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.DOTALL | re.IGNORECASE)


def _offer_from_ldjson(obj: object) -> tuple[float | None, str | None, str | None]:
    """Walk a parsed JSON-LD object for the first Offer-like price/currency/availability."""
    found_price = found_cur = found_avail = None
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "price" in node and found_price is None:
                found_price = parse_price(node.get("price"))
                found_cur = found_cur or node.get("priceCurrency")
            avail = node.get("availability")
            if avail and found_avail is None:
                found_avail = str(avail).rsplit("/", 1)[-1].lower()
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found_price, (str(found_cur) if found_cur else None), found_avail


def live_listing_check(url: object, timeout: float | None = None) -> dict:
    """Adapter chain for live page facts: OLX embedded state → JSON-LD Offer → OpenGraph meta →
    generic price regex. Returns price, native currency, ad status, and the page <title>."""
    result: dict = {"ok": False, "live_price": None, "live_currency": None, "ad_status": None,
                    "page_title": None, "live_variants": {}, "reason": None}
    request = urllib.request.Request(str(url), headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout or URL_TIMEOUT_SEC * 2) as response:
            html_body = response.read(2_500_000).decode("utf-8", errors="replace")
    except Exception as exc:
        result["reason"] = exc.__class__.__name__
        return result
    result["ok"] = True

    title_match = TITLE_RE.search(html_body)
    if title_match:
        result["page_title"] = re.sub(r"\s+", " ", title_match.group(1)).strip()[:200]

    # Bundled multi-tier listings (Plati: Pro / Max 5x / Max 20x on one page) — map each tier to
    # its own price so the requested tier's price can override a listing-level/base-tier price.
    result["live_variants"] = extract_variants(html_body)

    # OLX embeds ad state + price in __PRERENDERED_STATE__.
    status_match = LIVE_AD_STATUS_RE.search(html_body)
    if status_match:
        result["ad_status"] = status_match.group(1)

    # JSON-LD Offer (most structured marketplaces, incl. real-estate/goods portals).
    for block in LDJSON_RE.findall(html_body):
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        price, cur, avail = _offer_from_ldjson(data)
        if price is not None:
            result["live_price"], result["live_currency"] = price, cur
            if avail and result["ad_status"] is None:
                result["ad_status"] = "active" if "instock" in avail or "available" in avail else avail
            return result

    # OpenGraph product meta.
    og_price = OG_PRICE_RE.search(html_body)
    if og_price:
        result["live_price"] = parse_price(og_price.group(1))
        og_cur = OG_CUR_RE.search(html_body)
        result["live_currency"] = og_cur.group(1) if og_cur else None
        return result

    # Generic fallback.
    price_match = LIVE_PRICE_RE.search(html_body)
    if price_match:
        result["live_price"] = parse_price(price_match.group(1))
    return result


def apply_live_check(item: dict, intent: dict | None = None) -> None:
    """Mutates a finding after its page was read: inactive ads get flagged for rejection,
    live price (compared in USD) overrides the model's claim, audit trail kept in
    price_candidates. Only runs for URLs with a known marketplace listing key.

    When the page bundles multiple tiers and the user (or the finding) names a required tier, the
    REQUESTED tier's price from the page wins over the listing-level/base price — fixing the
    'cheap Pro masquerading as Max 5x' failure."""
    if not listing_key(item.get("url")):
        return
    live = live_listing_check(item.get("url"))
    item["live_check"] = live
    if not live["ok"]:
        return
    if live["ad_status"] and live["ad_status"] not in {"active", "instock", "available"}:
        item["listing_inactive"] = True
        return

    live_price = live.get("live_price")
    live_cur = canon_currency(live.get("live_currency")) or item.get("currency")
    # Variant override: prefer the price of the tier the user/finding actually wants.
    wanted = canon_tier((intent or {}).get("required_tier")) or canon_tier(item.get("tier"))
    variant = (live.get("live_variants") or {}).get(wanted) if wanted else None
    if variant and variant.get("price") is not None:
        item["tier"] = wanted
        item["variant_corrected"] = True
        live_price = variant["price"]
        live_cur = canon_currency(variant.get("currency")) or live_cur
    if live_price is None:
        return
    live_usd = to_usd(live_price, live_cur)
    claimed_usd = item.get("price_usd")
    if live_usd is None:
        return
    # The live page is verified fact — it ALWAYS becomes the canonical price (even when it agrees
    # with the model's claim, so currency/USD are page-accurate). The tolerance only decides
    # whether this counts as a price CORRECTION worth flagging to the user.
    materially_changed = claimed_usd is None or max(live_usd, claimed_usd) > min(live_usd, claimed_usd) * LIVE_PRICE_TOLERANCE
    candidates = list(item.get("price_candidates") or [])
    candidate = {"price": live_price, "price_usd": live_usd, "currency": live_cur, "source_model": "live_page"}
    if candidate not in candidates:
        candidates.append(candidate)
    item["price_candidates"] = candidates
    if materially_changed and item.get("price") is not None:
        item["price_corrected_from"] = item.get("price")
    item["price"] = live_price
    item["currency"] = live_cur
    item["price_usd"] = live_usd
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


def rejection_reasons(finding: dict, url_check: dict | None = None, sites: list[str] | None = None, intent: dict | None = None) -> list[str]:
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

    intent_reason = intent_rejection(finding, intent)
    if intent_reason:
        reasons.append(intent_reason)

    if content_mismatch(finding, intent):
        reasons.append("content_mismatch")

    return reasons


# A rejection is final only when the item is disproven or excluded by policy. Everything else
# (broken/moved URL, timeout, missing price, 4xx) is a FAILURE TO VERIFY — the item may be exactly
# what the user wants, so it gets rescue rechecks and an "unconfirmed" slot in the report.
# off_intent/wrong_tier are final (it is the wrong thing); not_below_official is final (policy);
# content_mismatch is final (the live page sells something else).
NON_RESCUABLE_REASONS = {
    "parse_failed", "off_site", "out_of_stock", "adjudicated_reject", "listing_inactive",
    "off_intent", "wrong_tier", "not_below_official", "content_mismatch",
}
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
    intent: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    unique = dedupe_findings(findings)
    verified: list[dict] = []
    rejected: list[dict] = list(parse_rejections or [])

    def check_one(finding: dict) -> tuple[dict, dict]:
        item = dict(finding)
        url_check = verify_url(item.get("url"))
        if url_check.get("ok"):
            apply_live_check(item, intent)
        return item, url_check

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_VERIFY_WORKERS, max(1, len(unique)))) as executor:
        futures = [executor.submit(check_one, finding) for finding in unique]
        for future in concurrent.futures.as_completed(futures):
            item, url_check = future.result()
            item["url_check"] = url_check
            reasons = rejection_reasons(item, url_check, sites, intent)
            if reasons:
                item["reasons"] = reasons
                rejected.append(item)
            else:
                verified.append(item)

    return sort_by_usd(verified), sort_by_usd(rejected)


def sort_by_usd(items: list[dict]) -> list[dict]:
    # Rank by USD (comparable across currencies); items without a USD price sort last.
    return sorted(items, key=lambda x: (x.get("price_usd") is None, x.get("price_usd") or 10**18, str(x.get("title") or "")))


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
For each task ALSO give 2-3 "query_variants": alternate SURFACE phrasings of the SAME query that
beat search engines' phrase-adjacency (a query "macbook air m2" misses a listing titled "Apple
MacBook Air 2022 M2"). Vary: word order, synonyms, transliteration (latin↔cyrillic, e.g. "макбук"),
model/SKU codes (e.g. A2681), year/spec reorderings. They search the SAME thing, not a new angle.

ALSO extract an "intent" object that the verifier and judge will enforce:
- subject_keywords: words/phrases that a RELEVANT result's title MUST contain (the actual thing
  wanted, e.g. ["macbook air m2"] or ["claude", "max"]). Used to reject wrong products.
- exclude_keywords: words that mark a WRONG result to reject (other products, "for parts",
  "запчасти", competing brands the user did not ask for, etc.).
- required_tier: if the user demanded a minimum tier/variant (e.g. "Max 5x or higher"), name it
  in lowercase ("max_5x"); else null. Below-tier offers are rejected.
- official_price / official_currency: the official/reference price the user wants to BEAT (they
  said "cheaper than $100" → 100, "USD"); offers at-or-above this are rejected. null if none.
- cheaper_than_official: true if the user explicitly wants STRICTLY BELOW the official price.

Schema:
{{
  "tasks": [
    {{"id": "task-1", "query": "specific search query", "focus": "what to verify",
      "query_variants": ["alt phrasing 1", "alt phrasing 2"],
      "preferred_sites": ["olx.ua", "prom.ua"]}}
  ],
  "intent": {{
    "subject_keywords": ["..."], "exclude_keywords": ["..."],
    "required_tier": null, "official_price": null, "official_currency": null,
    "cheaper_than_official": false
  }}
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
        raw_variants = raw.get("query_variants") or []
        if not isinstance(raw_variants, list):
            raw_variants = [str(raw_variants)]
        variants, seen_v = [], {query.lower()}
        for v in raw_variants:
            v = str(v or "").strip()
            if v and v.lower() not in seen_v:
                seen_v.add(v.lower())
                variants.append(v)
        tasks.append(
            {
                "id": str(raw.get("id") or f"task-{idx}"),
                "query": query,
                "query_variants": variants[:4],
                "focus": str(raw.get("focus") or "Find current purchasable offers with verified URLs."),
                "preferred_sites": preferred,
            }
        )

    return tasks if len(tasks) >= 3 else fallback_tasks(user_prompt, config)


# Ordered tier ladder for subscription/account-style products; index = rank (higher = stronger).
# A finding's reported tier must rank >= the user's required tier, else it is the wrong variant.
TIER_LADDER = ["pro", "team", "max_5x", "max_20x"]
TIER_SYNONYMS = {
    "pro": "pro", "team": "team",
    "max5x": "max_5x", "max_5x": "max_5x", "max 5x": "max_5x", "5x": "max_5x", "6.25x": "max_5x",
    "max20x": "max_20x", "max_20x": "max_20x", "max 20x": "max_20x", "20x": "max_20x",
}


def canon_tier(value: object) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if not text:
        return None
    if text in TIER_SYNONYMS:
        return TIER_SYNONYMS[text]
    for key, canon in TIER_SYNONYMS.items():
        if key in text:
            return canon
    return text if text in TIER_LADDER else None


def tier_rank(tier: object) -> int | None:
    canon = canon_tier(tier)
    return TIER_LADDER.index(canon) if canon in TIER_LADDER else None


# Tier surface forms, longest-first so "max 5x" matches before "5x" (word-boundary anchored).
_TIER_LABEL_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(k) for k in sorted(TIER_SYNONYMS, key=len, reverse=True)) + r")(?![a-z0-9])",
    re.IGNORECASE,
)
# A price token near a tier label: optional currency, a number, optional currency.
_CUR = r"[$€£₴]|usd|eur|gbp|uah|грн|руб|rub|дол"
_PRICE_NEAR_RE = re.compile(
    rf"(?P<c1>{_CUR})?\s*(?P<n>[0-9][0-9.,   ]{{0,7}}[0-9]|[0-9])\s*(?P<c2>{_CUR})?",
    re.IGNORECASE,
)


def extract_variants(html_body: str) -> dict:
    """Best-effort map {canon_tier: {price, currency}} from a bundled multi-tier page. Heuristic:
    for each tier label, take the FIRST price within a short window after it. Conservative — only
    keeps a variant when a price is found close to the label; first occurrence per tier wins."""
    variants: dict[str, dict] = {}
    # Drop <head> (title/meta list tiers without prices and would mis-anchor the proximity scan).
    body = re.sub(r"(?is)<head\b.*?</head>", " ", html_body)
    for m in _TIER_LABEL_RE.finditer(body):
        tier = canon_tier(m.group(1))
        if not tier or tier in variants:
            continue
        window = body[m.end(): m.end() + 60]
        pm = _PRICE_NEAR_RE.search(window)
        if not pm:
            continue
        # Require an adjacent currency marker — otherwise a bare number like the "20" in a nearby
        # "Max 20x" label would be mistaken for a price. Real tier tables carry a currency.
        cur_raw = pm.group("c1") or pm.group("c2")
        if not cur_raw:
            continue
        price = parse_price(pm.group("n"))
        if price is None:
            continue
        variants[tier] = {"price": price, "currency": canon_currency(cur_raw)}
    return variants


def default_intent() -> dict:
    return {
        "subject_keywords": [], "exclude_keywords": [],
        "required_tier": None, "official_price_usd": None, "cheaper_than_official": False,
    }


def coerce_intent(payload: object) -> dict:
    intent = default_intent()
    raw = payload.get("intent") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return intent

    def strlist(v):
        if isinstance(v, str):
            v = [v]
        return [str(x).strip().lower() for x in (v or []) if str(x).strip()]

    intent["subject_keywords"] = strlist(raw.get("subject_keywords"))
    intent["exclude_keywords"] = strlist(raw.get("exclude_keywords"))
    intent["required_tier"] = canon_tier(raw.get("required_tier"))
    intent["cheaper_than_official"] = bool(raw.get("cheaper_than_official"))
    official = parse_price(raw.get("official_price"))
    intent["official_price_usd"] = to_usd(official, raw.get("official_currency") or "USD") if official else None
    return intent


def intent_rejection(finding: dict, intent: dict | None) -> str | None:
    """Reject findings that don't match what the user actually asked for: wrong product
    (exclude keyword / no subject keyword), wrong tier, or not below the official price."""
    if not intent:
        return None
    text = " ".join(str(finding.get(f) or "") for f in ("title", "evidence", "marketplace")).lower()
    if intent.get("exclude_keywords") and any(kw in text for kw in intent["exclude_keywords"]):
        return "off_intent"
    subject = intent.get("subject_keywords") or []
    if subject and not any(kw in text for kw in subject):
        return "off_intent"
    req_rank = tier_rank(intent.get("required_tier"))
    if req_rank is not None:
        ft = tier_rank(finding.get("tier"))
        # Reject only a KNOWN-lower tier. A finding with no reported tier is NOT rejected here
        # (failure-to-extract ≠ wrong tier; rescue/adjudication weigh it) — the synthesis prompt
        # is told the required tier so it can flag tier-unknown items rather than drop them.
        if ft is not None and ft < req_rank:
            return "wrong_tier"
    ceiling = intent.get("official_price_usd")
    if intent.get("cheaper_than_official") and ceiling and finding.get("price_usd") is not None:
        if finding["price_usd"] >= ceiling:
            return "not_below_official"
    return None


_WORD_RE = re.compile(r"[a-z0-9а-яёіїєґ]{3,}", re.IGNORECASE)


def _words(text: object) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(str(text or ""))}


def content_mismatch(finding: dict, intent: dict | None) -> bool:
    """The repurposed-listing trap (a 'macbook-air-m2' slug whose LIVE page sells a Dyson):
    HTTP 200 + active ad + a price all pass, but the page is about a DIFFERENT thing. We only
    have signal when the live page was actually fetched and exposed a title.

    Conservative — flag a mismatch ONLY when the listing was clearly indexed under the right
    subject (the finding's own title carries a subject keyword) yet the LIVE page title carries
    NONE of them, or carries an exclude keyword. That is exactly the repurposed-ad signature;
    legit listings whose title is merely phrased differently are not touched."""
    if not intent:
        return False
    live = finding.get("live_check") or {}
    if not live.get("ok"):
        return False
    live_title = str(live.get("page_title") or "")
    if not live_title.strip():
        return False
    live_words = _words(live_title)
    if intent.get("exclude_keywords") and any(kw in live_title.lower() for kw in intent["exclude_keywords"]):
        return True
    subject = [kw for kw in (intent.get("subject_keywords") or []) if kw]
    if not subject:
        return False
    finding_title = str(finding.get("title") or "").lower()
    indexed_on_subject = any(kw in finding_title for kw in subject)
    # subject keyword present as a whole word in the live title (handles multi-word subjects too)
    live_has_subject = any(all(part in live_words for part in _words(kw)) for kw in subject)
    return indexed_on_subject and not live_has_subject


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
    return f"""You are a web research worker for current offers of ANY kind — a product, a
service, a subscription, an account, real estate, a vehicle: anything purchasable OR rentable.
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
- Return direct listing/offer URLs (a product page, a rental listing, a provider's plan page),
  not category or search pages when avoidable.
- Unknown fields must be null. Do not invent prices, stock, location, shipping, or URLs.
- Report the price in the listing's OWN native currency + the currency code (do NOT pre-convert).
- Include evidence as a short phrase explaining what you verified on the page.
- Record the item's CONDITION (new / used-good / damaged / for parts) and SELLER trust signals
  (rating, reviews count, account age, business vs private) whenever the page shows them.
- If the listing bundles MULTIPLE tiers/variants/packages (e.g. Pro / Max 5x / Max 20x in one
  page), report the SPECIFIC tier your price is for in "tier", and make the price match THAT tier,
  not the cheapest bundled option. If the user asked for a minimum tier, return that tier's price.
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
      "tier": "the specific variant this price is for, or null",
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


def build_frontier_prompt(user_prompt: str, ceiling_usd: float, run_sites: list[str], intent: dict | None) -> str:
    """Targeted search for offers STRICTLY cheaper than the current best credible price — the
    frontier round pushes the price floor down or proves nothing cheaper-and-credible exists."""
    site_rule = (f"- HARD CONSTRAINT: only URLs on these domains: {', '.join(run_sites)}.\n" if run_sites else "")
    subj = ", ".join((intent or {}).get("subject_keywords") or []) or "the requested item"
    return f"""You are a price-FRONTIER research worker. The best credible offer found so far is
about ${ceiling_usd:.2f} USD. Find CURRENTLY AVAILABLE, credible offers for the SAME thing
({subj}) that are STRICTLY CHEAPER than that — or report that none exist.
Use live web results. Return ONLY valid JSON. No Markdown.

Original user request:
{user_prompt}

Rules:
{site_rule}- Every returned offer MUST be plausibly below ${ceiling_usd:.2f} USD (convert from native currency).
- It must be the SAME thing the user wants (right product/tier), working and honestly described —
  a cheaper price on a damaged / for-parts / wrong-tier / scam-flavored item does NOT count.
- Return direct listing/offer URLs with native price + currency code. Unknown fields null.
- If there is genuinely nothing credible below ${ceiling_usd:.2f}, return an empty findings array.

Schema:
{{"findings": [{{"title": "...", "price": 0, "currency": "USD", "url": "https://...",
  "marketplace": "...", "availability": "...", "condition": "...", "tier": null,
  "seller": "...", "location": null, "shipping": null, "evidence": "...", "confidence": 0.0}}]}}
"""


def credible_floor_usd(verified: list[dict]) -> float | None:
    """Lowest USD price among non-disputed verified findings — the current frontier."""
    prices = [v.get("price_usd") for v in verified if v.get("price_usd") is not None and not v.get("disputed")]
    return min(prices) if prices else None


# --- Seller / source trust (R2 Phase 5) ----------------------------------------------------------
# A structured 0..1 trust score from the signals the legs surfaced (seller string, condition,
# price-vs-market, disputes). Used to rank by fit × trust × price — a credible slightly-pricier
# offer should beat a suspiciously-cheap one. Heuristic and explainable (signals listed).
_AGE_RE = re.compile(r"(?:since|account since|c|з|с)\s*(20\d\d)", re.IGNORECASE)
_AGE_YEARS_RE = re.compile(r"(\d{1,2})\+?\s*(?:year|years|год|года|лет|рок|роки|років)", re.IGNORECASE)
_REVIEWS_RE = re.compile(r"(\d[\d\s]*)\s*(?:review|reviews|отзыв|отзыва|отзывов|відгук|відгуки|відгуків)", re.IGNORECASE)
_RATING_RE = re.compile(r"(?:rating|рейтинг|рейтинґ|★|⭐|[0-9]{1,3}\s*%)", re.IGNORECASE)
_BUSINESS_RE = re.compile(r"(?:business|бизнес|бізнес|shop|store|магазин|official|офіц|офиц)", re.IGNORECASE)
_SCAM_RE = re.compile(r"(?:предоплат|предоплата|prepay|prepayment|no reviews|без отзыв|0 отзыв|без відгук|терміново|срочно|too cheap|слишком дешев|подозрит|scam|развод)", re.IGNORECASE)
SCAM_CHEAP_RATIO = 0.6  # below 60% of the credible floor with no reason = scam/damage flag


def seller_trust(item: dict, floor_usd: float | None) -> dict:
    score, signals = 0.5, []
    seller = str(item.get("seller") or "")
    evidence = str(item.get("evidence") or "")
    blob = f"{seller} {evidence}".lower()

    age_year = None
    m = _AGE_RE.search(seller)
    if m:
        age_year = int(m.group(1))
    elif _AGE_YEARS_RE.search(seller):
        age_year = 2026 - int(_AGE_YEARS_RE.search(seller).group(1))
    if age_year is not None:
        if age_year <= 2023:
            score += 0.15; signals.append(f"established account (~{age_year})")
        else:
            score += 0.05; signals.append("recent account")

    if _REVIEWS_RE.search(seller) or _RATING_RE.search(blob):
        score += 0.15; signals.append("has rating/reviews")
    if _BUSINESS_RE.search(blob):
        score += 0.08; signals.append("business/shop seller")

    if floor_usd and item.get("price_usd") is not None and item["price_usd"] < floor_usd * SCAM_CHEAP_RATIO:
        score -= 0.25; signals.append("far below market (scam/damage risk)")
    cond = str(item.get("condition") or "").lower()
    if any(w in cond for w in ("damaged", "for parts", "for-parts", "поврежд", "запчаст", "розбірк")):
        score -= 0.25; signals.append("damaged / for parts")
    if _SCAM_RE.search(blob):
        score -= 0.15; signals.append("scam-flavored wording")
    if item.get("disputed"):
        score -= 0.1; signals.append("disputed price")
    if not seller.strip():
        score -= 0.05; signals.append("no seller info")

    score = round(min(1.0, max(0.0, score)), 3)
    return {"score": score, "signals": signals}


def apply_trust(verified: list[dict]) -> None:
    """Attach a trust score to every verified finding (uses the credible floor for the
    too-cheap signal). Mutates in place."""
    floor = credible_floor_usd(verified)
    for item in verified:
        item["trust"] = seller_trust(item, floor)


def trust_rank_key(item: dict):
    """Order for the synthesis context: credible-and-cheap first. High/mid/low trust tier, then
    USD price within the tier — so a suspiciously-cheap low-trust item doesn't lead the list."""
    score = (item.get("trust") or {}).get("score", 0.5)
    tier = 0 if score >= 0.66 else (1 if score >= 0.4 else 2)
    return (tier, item.get("price_usd") if item.get("price_usd") is not None else 10**18, str(item.get("title") or ""))


# --- Confidence calibration (R2 Phase 7) ---------------------------------------------------------
# A 0..1 confidence per recommendation, combining the independent quality signals so the report
# can state certainty honestly. Weights sum to 1: cross-leg agreement, live verification, seller
# trust, and the model's own confidence. Explainable (factors listed). Requires trust set first.
def calibrate_confidence(item: dict) -> dict:
    factors = []

    legs = item.get("source_models") or ([item["source_model"]] if item.get("source_model") else [])
    n = len([l for l in legs if l and l != "live_page"])
    if item.get("disputed"):
        agreement = 0.0; factors.append("cross-model price dispute")
    else:
        agreement = {0: 0.4, 1: 0.45, 2: 0.75}.get(n, 1.0)
        if n >= 2:
            factors.append(f"{n} models agree")

    live = item.get("live_check") or {}
    if item.get("listing_inactive"):
        live_score = 0.0; factors.append("listing inactive")
    elif live.get("ok") and live.get("live_price") is not None:
        live_score = 1.0; factors.append("live page confirmed")
        if item.get("price_corrected_from") is not None:
            live_score = 0.85; factors.append("live price corrected")
        if item.get("variant_corrected"):
            factors.append("tier confirmed from page")
    elif live.get("ok"):
        live_score = 0.6
    else:
        live_score = 0.4; factors.append("not live-verified")

    trust = float((item.get("trust") or {}).get("score", 0.5))
    model_conf = item.get("confidence")
    model_conf = float(model_conf) if isinstance(model_conf, (int, float)) else 0.5

    score = round(0.25 * agreement + 0.30 * live_score + 0.30 * trust + 0.15 * model_conf, 3)
    band = "high" if score >= 0.7 else ("medium" if score >= 0.45 else "low")
    return {"score": score, "band": band, "factors": factors}


def apply_confidence(verified: list[dict]) -> None:
    """Attach a calibrated confidence to every verified finding. Run AFTER apply_trust."""
    for item in verified:
        item["confidence_calibrated"] = calibrate_confidence(item)


def run_frontier_round(prompt: str, ceiling_usd: float, run_dir: Path, config: dict, intent: dict | None, round_no: int) -> list[dict]:
    """One frontier sweep: each search leg hunts strictly below the ceiling."""
    search_legs = config.get("search_legs") or ["codex", "gemini"]
    fp = build_frontier_prompt(prompt, ceiling_usd, config.get("sites") or [], intent)
    timeout = min(RAW_TIMEOUT_SEC, config["search_timeout_sec"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_PRIMARY_WORKERS, len(search_legs))) as executor:
        futures = [
            executor.submit(call_model, leg, fp, run_dir, "frontier", f"frontier-{round_no}",
                            timeout, config["search_effort"], "sonnet" if leg == "claude" else None)
            for leg in search_legs
        ]
        return collect_with_straggler_drop(futures, run_dir, config)


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


def host_of(url: object) -> str:
    host = urllib.parse.urlsplit(str(url or "")).netloc.split(":")[0].lower().strip(".")
    return re.sub(r"^www\.", "", host)


def host_distribution(items: list[dict]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for it in items:
        h = host_of(it.get("url"))
        if h:
            dist[h] = dist.get(h, 0) + 1
    return dist


def diversify(items: list[dict], cap_fraction: float = 0.5, min_per_host: int = 2) -> list[dict]:
    """Anti-monoculture: keep order but stop one host from dominating the TOP. A host may take at
    most max(min_per_host, cap_fraction*N) of the leading slots; its overflow is pushed down,
    so the first options the user sees span multiple sources."""
    n = len(items)
    if n <= min_per_host:
        return items
    cap = max(min_per_host, int(cap_fraction * n))
    lead, overflow, seen = [], [], {}
    for it in items:
        h = host_of(it.get("url"))
        seen[h] = seen.get(h, 0) + 1
        (lead if seen[h] <= cap else overflow).append(it)
    return lead + overflow


def build_synthesis_prompt(
    user_prompt: str,
    tasks: list[dict],
    verified: list[dict],
    rejected: list[dict],
    config: dict,
    degraded_legs: list[str] | None = None,
    intent: dict | None = None,
) -> str:
    unconfirmed = [item for item in rejected if is_rescuable(item)]
    dead = [item for item in rejected if not is_rescuable(item)]
    ordered = diversify(sorted(verified, key=trust_rank_key))
    requested_sites = config.get("sites") or [s for t in tasks for s in (t.get("preferred_sites") or [])]
    found_hosts = host_distribution(verified)
    zero_result_sites = sorted(set(requested_sites) - set(found_hosts))
    context = {
        "user_prompt": user_prompt,
        "intent": intent or None,
        "restricted_to_sites": config.get("sites") or None,
        "degraded_legs": degraded_legs or None,
        "host_distribution": found_hosts,
        "sites_with_zero_results": zero_result_sites or None,
        "tasks": tasks,
        "verified_findings": ordered[:20],
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
- Infer the user's real intent (see the "intent" object: subject, excluded items, required tier,
  official price to beat). A relevant result is a WORKING, honestly-described one of the RIGHT
  variant/tier from a credible seller — not a different product, a lower tier, damaged, for-parts,
  bait-priced, or scam-flavored.
- Prices are in price_usd (USD, comparable). Show USD; you may also show the native price/currency.
- If intent.cheaper_than_official with an official_price_usd, every option you recommend MUST be
  strictly below it; never present the official price as a find.
- Rank by fit × seller TRUST × price. Each finding carries trust.score (0..1) and trust.signals
  (established account, has reviews, business seller, far-below-market, damaged, scam wording,
  disputed). A higher-trust slightly-pricier listing beats a low-trust cheaper one; never lead
  with a low-trust bait-priced item even if it is the cheapest.
- CRITICAL: for EVERY option cheaper than your top pick, explain in one line why it was not chosen
  (cite the trust signal when that's the reason).
- DIVERSITY: do not let one marketplace dominate. If host_distribution is lopsided or
  sites_with_zero_results is non-empty, say so plainly ("most results came from X; Y/Z returned
  nothing — treat the single-source list with caution"). Prefer surfacing options across sources.
- No cap on how many options you list — order them best-fit first.

Also include:
- URLs for every option.
- Items flagged "disputed": true carry conflicting cross-model prices (see price_candidates) —
  state the uncertainty explicitly instead of picking one silently.
- unconfirmed_candidates failed automated verification but were NOT disproven — put the promising
  ones in a separate "Unverified — check manually" section with URLs and what to verify.
- If degraded_legs is set, one of the search models failed — state it as a Markdown blockquote AT
  THE VERY TOP (`> WARNING: ...`). Never present this warning as the best pick or first section.
- Each finding carries confidence_calibrated {score, band: high/medium/low, factors} combining
  cross-model agreement, live verification, seller trust and the model's own confidence. State the
  confidence of your top pick honestly (e.g. "high confidence — 3 models agree, live-verified,
  trusted seller" or "low — single source, not live-verified"); prefer a high-confidence option
  for the lead and flag when the best price only comes with low confidence.
- A clear conclusion: the best pick (with its confidence) and the strongest runner-up.
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
    script = ROOT / "lib" / "legs" / f"ask_{leg}.sh"
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
        emit_event(run_dir, "call_finished", **{k: v for k, v in meta.items() if k != "rc"})
        meta["stdout"] = ""
        meta["stderr"] = reason_text
        return meta

    if run_cancelled(run_id):
        return skipped_meta("skipped_by_cancel", "run was cancelled")
    if user_disabled(run_id, leg):
        # Vendor switched off for this run to save its quota — never call it (even past the breaker).
        return skipped_meta("skipped_by_user", "vendor disabled for this run")
    if not bypass_breaker and leg_disabled(run_id, leg):
        return skipped_meta("skipped_by_breaker", "leg disabled by circuit breaker for this run")
    # Per-run call budgets cap only the heavy search/recheck fan-out; the few judge-seat calls
    # (decompose/adjudicate/review/synthesize) are important and not budget-limited.
    if not bypass_breaker and task_type in ("search", "recheck", "frontier") and not consume_leg_budget(run_id, leg):
        return skipped_meta("skipped_by_budget", "leg call budget for this run is spent")
    emit_event(run_dir, "call_started", record_id=record_id, leg=leg, task_type=task_type, task_id=task_id)

    queued_at = time.monotonic()
    env = os.environ.copy()
    if leg == "codex":
        env["CODEX_EFFORT"] = effort
    if leg == "claude" and claude_model:
        env["CLAUDE_MODEL"] = claude_model
    # Isolate any file the leg's agent might write (agy's --print is agentic and its --sandbox is
    # only "terminal restrictions", NOT a write guard) into a throwaway per-call scratch dir,
    # never the project root. The audit log must still land in the real data/ dir, so pin it.
    scratch = run_dir / "scratch" / record_id
    scratch.mkdir(parents=True, exist_ok=True)
    env["LLM_LEGS_DATA_DIR"] = str(DATA_DIR)
    semaphore = LEG_SEMAPHORES.get(leg)
    if semaphore is not None:
        semaphore.acquire()
        if run_cancelled(run_id):
            # The cancel may have landed while this thread waited for a semaphore slot —
            # never spawn a new subprocess into a cancelled run. Release the slot, refund the
            # budget we reserved at the gate, and drop the scratch dir we created.
            semaphore.release()
            if not bypass_breaker and task_type in ("search", "recheck", "frontier"):
                refund_leg_budget(run_id, leg)
            shutil.rmtree(scratch, ignore_errors=True)
            return skipped_meta("skipped_by_cancel", "run was cancelled")

    # Time the ACTUAL call from here — after the semaphore slot is acquired — so a leg's latency
    # reflects its real work, not time spent queued behind the concurrency cap. queue_wait is
    # recorded separately (this fixed the "gemini looks slow / sequential" illusion).
    queue_wait = time.monotonic() - queued_at
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [str(script), prompt],
            cwd=str(scratch),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,  # legs must never inherit the server's stdin
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
        shutil.rmtree(scratch, ignore_errors=True)

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
        "queue_wait_sec": round(queue_wait, 3),
        "stdout_file": str(raw_base.with_suffix(".txt").relative_to(run_dir)),
        "stderr_file": str(raw_base.with_suffix(".stderr.txt").relative_to(run_dir)),
    }
    write_json(raw_base.with_suffix(".meta.json"), meta)
    emit_event(
        run_dir,
        "call_finished",
        record_id=record_id,
        leg=leg,
        task_type=task_type,
        task_id=task_id,
        success=meta["success"],
        rc=rc,
        latency_sec=meta["latency_sec"],
        queue_wait_sec=meta["queue_wait_sec"],
        timed_out=timed_out,
        dropped_as_straggler=meta["dropped_as_straggler"],
    )
    breaker_tripped = record_leg_result(run_id, leg, meta["success"])
    if rc == 5 and force_disable_leg(run_id, leg, "quota_exhausted"):
        breaker_tripped = True
    if breaker_tripped:
        emit_event(run_dir, "leg_disabled", leg=leg, reason="quota_exhausted" if rc == 5 else "circuit_breaker")
        update_run(run_dir, leg_health=leg_health_snapshot(run_id))
    meta["stdout"] = stdout
    meta["stderr"] = stderr
    return meta


AGY_CLAUDE_MODEL = os.environ.get("RESEARCH_AGY_CLAUDE_MODEL", "Claude Opus 4.6 (Thinking)")


def call_agy_claude(prompt: str, run_dir: Path, task_type: str, task_id: str, timeout: int = 600) -> dict:
    """Claude RESERVE via the Antigravity CLI (separate quota pool from the Anthropic
    subscription). Used only when the native Claude judge/arbiter pool is exhausted. agy serves
    a Claude tier directly; the served model is pinned/unverified (agy does not report it).
    Spawned with the same scratch-cwd isolation and stdin guard as the other legs."""
    record_id = f"{task_type}-{safe_name(task_id)}-claudeagy-{uuid.uuid4().hex[:8]}"
    raw_base = run_dir / "raw" / record_id
    scratch = run_dir / "scratch" / record_id
    scratch.mkdir(parents=True, exist_ok=True)
    if not shutil.which("agy"):
        return {"success": False, "stdout": "", "leg": "claude-agy", "record_id": record_id}
    emit_event(run_dir, "call_started", record_id=record_id, leg="claude-agy", task_type=task_type, task_id=task_id)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            ["agy", "--print", "--model", AGY_CLAUDE_MODEL, "--print-timeout", "10m", "--sandbox", prompt],
            cwd=str(scratch), env=os.environ.copy(), text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        stdout, _ = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        stdout, rc = "", 124
    except Exception:
        stdout, rc = "", 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    success = rc == 0 and bool(stdout.strip())
    meta = {
        "record_id": record_id, "leg": "claude-agy", "task_type": task_type, "task_id": task_id,
        "rc": rc, "success": success, "latency_sec": round(time.monotonic() - started, 3),
    }
    raw_base.with_suffix(".txt").write_text(stdout, encoding="utf-8")
    write_json(raw_base.with_suffix(".meta.json"), meta)
    emit_event(run_dir, "call_finished", record_id=record_id, leg="claude-agy",
               task_type=task_type, task_id=task_id, success=success, rc=rc, latency_sec=meta["latency_sec"])
    if success:
        log_served = {"ts": utc_now(), "leg": "claude-agy", "transport": "agy",
                      "requested": AGY_CLAUDE_MODEL, "served": f"antigravity:pinned:{AGY_CLAUDE_MODEL} (unverified)", "weak_tier": 0}
        append_jsonl(SERVED_MODELS, log_served)
    meta["stdout"] = stdout
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
                "search_legs": config.get("search_legs") or ["codex", "gemini"],
                "review_legs": config["review_legs"],
                "enabled_legs": config.get("enabled_legs") or list(ALL_VENDORS),
                "disabled_legs": config.get("disabled_legs") or [],
                "sites": config.get("sites") or [],
            },
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "error": None,
        },
    )
    emit_event(run_dir, "run_created", prompt=prompt, effort=config["effort"], sites=config.get("sites") or [])
    return run_dir


RUN_JSON_LOCK = threading.Lock()
EVENTS_LOCK = threading.Lock()


def emit_event(run_dir: Path, event: str, **fields: object) -> None:
    """Append one event to the run's events.jsonl — the live feed the UI streams via SSE.
    Append-only, one JSON object per line; never read back by the pipeline itself."""
    row = {"ts": utc_now(), "event": event}
    row.update(fields)
    try:
        with EVENTS_LOCK:
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass  # the event feed is best-effort; never fail the run over it


def update_run(run_dir: Path, **fields: object) -> None:
    # Read-modify-write under a lock: concurrent updates (heartbeat + breaker) must not
    # lose each other's fields.
    with RUN_JSON_LOCK:
        path = run_dir / "run.json"
        data = read_json(path, {}) or {}
        changed = {
            key: value
            for key, value in fields.items()
            if key in {"status", "phase", "progress", "leg_health"} and data.get(key) != value
        }
        data.update(fields)
        data["updated_at"] = utc_now()
        write_json(path, data)
    if "status" in changed or "phase" in changed:
        emit_event(run_dir, "status", status=data.get("status"), phase=data.get("phase"))
    if changed.get("progress"):
        emit_event(run_dir, "progress", **changed["progress"])
    if "leg_health" in changed:
        emit_event(run_dir, "leg_health", leg_health=changed["leg_health"])


def decompose_tasks(prompt: str, run_dir: Path, config: dict) -> tuple[list[dict], dict]:
    """Returns (tasks, intent). intent drives the relevance / tier / below-official gates."""
    brain = judge_vendor(config)
    record = call_model(
        brain,
        build_decompose_prompt(prompt, config),
        run_dir,
        "decompose",
        "tasks",
        timeout=600,
        effort=config["judge_effort"],
        claude_model=vendor_claude_model(brain, config),
    )
    if not record["success"]:
        return fallback_tasks(prompt, config), default_intent()
    try:
        payload = extract_json(record["stdout"])
    except ValueError:
        return fallback_tasks(prompt, config), default_intent()
    return coerce_tasks(payload, prompt, config), coerce_intent(payload)


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
            killed = kill_stragglers(run_dir.name)
            if killed:
                emit_event(run_dir, "stragglers_killed", record_ids=killed)
            killed_once = True
            deadline = time.monotonic() + 30  # killed procs unwind within seconds; don't re-kill
    return records


def task_query_set(task: dict, n: int) -> list[dict]:
    """Expand a task into up to n surface-form query variants (base query first). Each variant is
    a task-shaped dict with its own query + a distinct id so the activity view can tell them apart;
    results union back together via listing-ID dedupe. n=1 → just the base query (no expansion)."""
    queries, seen = [str(task.get("query") or "").strip()], set()
    seen.add(queries[0].lower())
    for v in (task.get("query_variants") or []):
        v = str(v or "").strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            queries.append(v)
    queries = [q for q in queries if q][:max(1, n)]
    return [
        {**task, "query": q, "id": task["id"] if i == 0 else f'{task["id"]}#v{i + 1}'}
        for i, q in enumerate(queries)
    ]


def run_primary_search(prompt: str, tasks: list[dict], run_dir: Path, config: dict) -> list[dict]:
    # Three families search in parallel when the effort profile enables Claude (effort 3-4).
    # Claude searches on SONNET (cheap; Opus is reserved for the judge/arbiter seat) and runs
    # under its own tight concurrency + per-run budget, so the capped pool isn't drained.
    # Each task is also expanded into query variants (beats search phrase-adjacency; effort-gated)
    # — results union via listing-ID dedupe. Leg budgets + straggler drop bound the extra fan-out.
    search_legs = config.get("search_legs") or ["codex", "gemini"]
    n_variants = config.get("query_variants_per_task", 1)
    expanded = [tv for task in tasks for tv in task_query_set(task, n_variants)]
    jobs = [(leg, tv) for tv in expanded for leg in search_legs]

    timeout = min(RAW_TIMEOUT_SEC, config["search_timeout_sec"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_PRIMARY_WORKERS, len(jobs))) as executor:
        futures = [
            executor.submit(
                call_model,
                leg,
                build_search_prompt(prompt, tv, leg, config),
                run_dir,
                "search",
                tv["id"],
                timeout,
                config["search_effort"],
                "sonnet" if leg == "claude" else None,
            )
            for leg, tv in jobs
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
        search_legs = config.get("search_legs") or list(SEARCH_LEGS)
        tried = attempts.setdefault(key, set())
        # Prefer a leg that has NOT tried this item yet, and that differs from its source.
        untried = [
            leg for leg in search_legs
            if leg not in tried and not leg_disabled(run_dir.name, leg)
        ]
        untried.sort(key=lambda leg: leg == item.get("source_model"))  # non-source first
        legs = untried if config["recheck_legs"] >= len(search_legs) else untried[: config["recheck_legs"]]
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
                "sonnet" if leg == "claude" else None,
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
        adj_prompt = build_adjudication_prompt(prompt, item, config)
        arbiter = arbiter_vendor(config)  # prefer Claude, else any enabled vendor
        record = call_model(
            arbiter, adj_prompt, run_dir, "adjudicate", f"dispute-{idx}",
            timeout=600, claude_model=vendor_claude_model(arbiter, config),
        )
        # Reserve: if the native pool is exhausted/down, fall back to Claude-via-agy (separate
        # quota pool) — only when Claude itself wasn't disabled by the user for this run.
        if not record["success"] and "claude" in (config.get("enabled_legs") or []):
            reserve = call_agy_claude(adj_prompt, run_dir, "adjudicate", f"dispute-{idx}-agy")
            if reserve["success"]:
                record = reserve
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
    return sort_by_usd(still_verified), sort_by_usd(rejected)


def factcheck_top_pick(prompt: str, verified: list[dict], run_dir: Path, config: dict, intent: dict | None) -> dict | None:
    """Final adversarial check of the SINGLE most important claim — the top pick — right before
    it is presented. A fresh code re-fetch (authoritative) decides active/price-drift/content, and
    an INDEPENDENT vendor re-reads the page to confirm. Returns a verdict dict (ok True/False/None;
    None = couldn't re-verify). Never raises."""
    if not verified:
        return None
    top = sorted(verified, key=trust_rank_key)[0]
    url = top.get("url")
    verdict = {"url": url, "title": top.get("title"), "ok": True, "reason": None,
               "claimed_usd": top.get("price_usd"), "live_usd": None, "vendor_confirmed": None}
    if not listing_key(url):
        verdict["ok"] = None
        verdict["reason"] = "no_listing_adapter"  # can't re-verify this host by code
        return verdict

    live = live_listing_check(url)
    if not live.get("ok"):
        verdict.update(ok=False, reason="page_unreachable")
        return verdict
    if live.get("ad_status") and live["ad_status"] not in {"active", "instock", "available"}:
        verdict.update(ok=False, reason="listing_inactive")
        return verdict
    if content_mismatch({**top, "live_check": live}, intent):
        verdict.update(ok=False, reason="content_mismatch")
        return verdict
    live_cur = canon_currency(live.get("live_currency")) or top.get("currency")
    live_usd = to_usd(live.get("live_price"), live_cur)
    verdict["live_usd"] = live_usd
    if live_usd and top.get("price_usd"):
        drift = abs(live_usd - top["price_usd"]) / top["price_usd"]
        verdict["drift"] = round(drift, 3)
        if drift > (LIVE_PRICE_TOLERANCE - 1):
            verdict.update(ok=False, reason=f"price_drift {top['price_usd']}->{live_usd} USD")
            return verdict

    # Independent-vendor confirmation (best-effort): a search leg that is healthy re-reads the page.
    legs = [lg for lg in (config.get("search_legs") or ["codex", "gemini"]) if not leg_disabled(run_dir.name, lg)]
    if legs:
        leg = legs[0]
        fp = (f"Open this exact URL and confirm the offer still matches the recommendation.\n"
              f"URL: {url}\nUser wants: {prompt}\n"
              f"Recommended price: ~{top.get('price_usd')} USD ({top.get('price')} {top.get('currency')}).\n"
              f"Answer ONLY JSON: {{\"confirmed\": true|false, \"reason\": \"one line\"}}. "
              f"confirmed=false if the page is gone/sold, a different product, a different tier, or a very different price.")
        rec = call_model(leg, fp, run_dir, "factcheck", "top-pick", timeout=300,
                         effort="medium", claude_model="sonnet" if leg == "claude" else None, bypass_breaker=True)
        if rec["success"]:
            try:
                ans = extract_json(rec.get("stdout") or "")
                if isinstance(ans, dict) and "confirmed" in ans:
                    verdict["vendor_confirmed"] = bool(ans["confirmed"])
                    verdict["vendor_reason"] = str(ans.get("reason") or "")[:200]
                    if ans["confirmed"] is False:
                        verdict.update(ok=False, reason="vendor_refuted: " + verdict["vendor_reason"])
            except ValueError:
                pass
    return verdict


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
            claude_model=vendor_claude_model(reviewer, config),
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
        reviser = judge_vendor(config)
        revision_record = call_model(
            reviser,
            build_revision_prompt(prompt, draft, issues, config),
            run_dir,
            "revise",
            f"round-{round_no}",
            timeout=700,
            effort=config["judge_effort"],
            claude_model=vendor_claude_model(reviser, config),
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


def synthesize_report(prompt: str, tasks: list[dict], verified: list[dict], rejected: list[dict], run_dir: Path, config: dict, intent: dict | None = None) -> str:
    degraded = disabled_legs(run_dir.name)
    if not verified:
        return fallback_report(prompt, verified, rejected, degraded)
    # The judge seat is the run's whole value: if the first judge is down (quota), another ENABLED
    # vendor takes the seat rather than dumping an unranked fallback list on the user.
    for judge in judge_chain(config):
        record = call_model(
            judge,
            build_synthesis_prompt(prompt, tasks, verified, rejected, config, degraded, intent),
            run_dir,
            "synthesize",
            f"final-{judge}",
            timeout=700,
            effort=config["judge_effort"],
            claude_model=vendor_claude_model(judge, config),
            bypass_breaker=True,  # always attempt each judge once, even past the breaker
        )
        if record["success"] and record.get("stdout", "").strip():
            return record["stdout"].strip() + "\n"
    # Last resort before the unranked fallback: Claude-via-agy (separate quota pool) — unless the
    # user disabled Claude for this run.
    if "claude" in (config.get("enabled_legs") or []):
        reserve = call_agy_claude(
            build_synthesis_prompt(prompt, tasks, verified, rejected, config, degraded, intent),
            run_dir, "synthesize", "final-claude-agy", timeout=700,
        )
        if reserve["success"] and reserve.get("stdout", "").strip():
            return reserve["stdout"].strip() + "\n"
    return fallback_report(prompt, verified, rejected, degraded)


def execute_research(run_dir: Path, prompt: str, config: dict) -> None:
    run_id = run_dir.name
    sites = config.get("sites") or []
    all_parsed_records: list[dict] = []
    verified: list[dict] = []
    rejected: list[dict] = []
    ACTIVE_RUNS.add(run_id)
    init_leg_health(run_id)
    set_user_disabled(run_id, config.get("disabled_legs") or [])
    requested_budgets = {"gemini": config["gemini_call_budget"]}
    if "claude" in (config.get("search_legs") or []):
        requested_budgets["claude"] = config.get("claude_search_budget", 0)
    # Quota-aware pacing: clamp each leg's per-run budget to its remaining daily allowance so a
    # single run can't exhaust the day's quota. Surfaced in run.json for the UI/scoreboard.
    leg_budgets, pacing = {}, {}
    for leg, requested in requested_budgets.items():
        budget, remaining = paced_budget(leg, requested)
        leg_budgets[leg] = budget
        pacing[leg] = {"requested": requested, "granted": budget, "daily_remaining": remaining,
                       "daily_cap": DAILY_CAPS.get(leg)}
    init_leg_budget(run_id, leg_budgets)
    update_run(run_dir, pacing=pacing)

    def check_cancel() -> None:
        if run_cancelled(run_id):
            raise RunCancelled()

    try:
        update_run(run_dir, status="running", phase="decomposing", progress=None)
        tasks, intent = decompose_tasks(prompt, run_dir, config)
        write_json(run_dir / "tasks.json", {"tasks": tasks, "intent": intent})
        update_run(run_dir, intent=intent)
        check_cancel()

        update_run(run_dir, phase="primary_search", progress=None)
        primary_records = run_primary_search(prompt, tasks, run_dir, config)
        findings, parse_rejections, parsed_records = parse_model_records(primary_records)
        all_parsed_records.extend(parsed_records)
        write_json(run_dir / "findings.json", {"stage": "primary", "findings": findings, "parse_rejections": parse_rejections})
        check_cancel()

        update_run(run_dir, phase="verifying", progress=None)
        verified, rejected = verify_findings(findings, parse_rejections, sites, intent)
        write_json(run_dir / "verification.json", {"stage": "primary", "verified": verified, "rejected": rejected})
        check_cancel()

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
            verified, rejected = verify_findings(findings, parse_rejections, sites, intent)
            check_cancel()
        if recheck_dropped:
            # No silent caps: the report and UI must show how many candidates the budget skipped.
            update_run(run_dir, recheck_dropped=recheck_dropped)
        check_cancel()

        # Frontier rounds: push strictly below the current best credible price until a round finds
        # nothing cheaper-and-credible (dry) or the round budget is spent. Effort-gated.
        for round_no in range(1, config.get("frontier_rounds", 0) + 1):
            ceiling = credible_floor_usd(verified)
            if ceiling is None:
                break
            update_run(run_dir, phase=f"frontier_{round_no}", progress=None)
            frontier_records = run_frontier_round(prompt, ceiling, run_dir, config, intent, round_no)
            if not frontier_records:
                break
            f_findings, f_parse_rej, f_parsed = parse_model_records(frontier_records)
            all_parsed_records.extend(f_parsed)
            findings = findings + f_findings
            parse_rejections = parse_rejections + f_parse_rej
            verified, rejected = verify_findings(findings, parse_rejections, sites, intent)
            new_floor = credible_floor_usd(verified)
            check_cancel()
            if new_floor is None or new_floor >= ceiling:  # dry round — nothing credible cheaper
                break

        if config["adjudicate_disputes"] and any(item.get("disputed") for item in verified):
            update_run(run_dir, phase="adjudicating", progress=None)
            verified, rejected = adjudicate_disputes(prompt, verified, rejected, run_dir, config)
            check_cancel()

        apply_trust(verified)  # structured seller/source trust for ranking + the UI
        apply_confidence(verified)  # calibrated 0..1 confidence per recommendation (after trust)
        write_json(run_dir / "findings.json", {"stage": "final", "findings": dedupe_findings(findings), "parse_rejections": parse_rejections})
        write_json(run_dir / "verification.json", {"stage": "final", "verified": verified, "rejected": rejected})
        write_model_stats(run_id, all_parsed_records, rejected)

        update_run(run_dir, phase="synthesizing", progress=None)
        report = synthesize_report(prompt, tasks, verified, rejected, run_dir, config, intent)
        check_cancel()

        if config["review_legs"]:
            update_run(run_dir, phase="reviewing", progress=None)
            report = adversarial_review(prompt, report, verified, rejected, run_dir, config)

        # Final adversarial fact-check of the top pick — re-confirm the single most important
        # claim before presenting it; a failure becomes a warning callout at the top of the report.
        if config.get("final_factcheck") and verified:
            update_run(run_dir, phase="factchecking", progress=None)
            fc = factcheck_top_pick(prompt, verified, run_dir, config, intent)
            update_run(run_dir, final_check=fc)
            if fc and fc.get("ok") is False:
                report = (f"> ⚠ FINAL CHECK: the top recommendation could not be re-confirmed "
                          f"({fc['reason']}). Re-verify it yourself before buying.\n\n" + report)

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
    except RunCancelled:
        # Finish in the best shape available: whatever survived verification becomes a partial
        # fallback report instead of vanishing.
        report = fallback_report(prompt, verified, rejected, disabled_legs(run_id))
        report = "> NOTE: this run was CANCELLED by the user — results below are partial.\n\n" + report
        (run_dir / "final.md").write_text(report, encoding="utf-8")
        update_run(
            run_dir,
            status="cancelled",
            phase="cancelled",
            progress=None,
            verified_count=len(verified),
            rejected_count=len(rejected),
            final_path=str((run_dir / "final.md").relative_to(ROOT)),
        )
    except Exception as exc:
        update_run(run_dir, status="failed", phase="failed", error=str(exc), traceback=traceback.format_exc())
        (run_dir / "final.md").write_text(f"# Research failed\n\n{exc}\n", encoding="utf-8")
    finally:
        ACTIVE_RUNS.discard(run_id)
        clear_cancel(run_id)
        clear_user_disabled(run_id)
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
        if not meta.get("prompt") or not meta.get("created_at"):
            continue  # crashed-before-init garbage; keep on disk for forensics, hide from the UI
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


def build_scoreboard() -> dict:
    """Per-leg health indicator (NOT routing input — premature for 3 legs): aggregate
    model-stats.jsonl (quality) + served-models.jsonl (weak/quota events) + today's pacing."""
    agg: dict[str, dict] = {}

    def slot(leg: str) -> dict:
        return agg.setdefault(leg, {
            "calls": 0, "success": 0, "parse_failed": 0, "no_sources": 0,
            "rejected_total": 0, "latency_sum": 0.0, "latency_n": 0,
        })

    try:
        with MODEL_STATS.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                leg = row.get("leg")
                if not leg:
                    continue
                s = slot(leg)
                s["calls"] += 1
                s["success"] += 1 if row.get("success") else 0
                s["parse_failed"] += 1 if row.get("parse_failed") else 0
                s["no_sources"] += 1 if row.get("no_sources") else 0
                s["rejected_total"] += int(row.get("rejected_count") or 0)
                lat = row.get("latency_sec")
                if isinstance(lat, (int, float)):
                    s["latency_sum"] += lat
                    s["latency_n"] += 1
    except OSError:
        pass

    today = daily_call_counts()
    weak_today: dict[str, int] = {}
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        with SERVED_MODELS.open("r", encoding="utf-8") as f:
            for line in f:
                if day not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(row.get("ts", "")).startswith(day) and (row.get("weak_tier") or str(row.get("served", "")).upper() in {"QUOTA_EXHAUSTED", "FAILED"}):
                    leg = row.get("leg")
                    if leg:
                        weak_today[leg] = weak_today.get(leg, 0) + 1
    except OSError:
        pass

    legs = []
    for leg in sorted(set(agg) | set(today) | set(DAILY_CAPS)):
        s = agg.get(leg, {"calls": 0, "success": 0, "parse_failed": 0, "no_sources": 0, "rejected_total": 0, "latency_sum": 0.0, "latency_n": 0})
        calls = s["calls"]
        cap = DAILY_CAPS.get(leg)
        used = today.get(leg, 0)
        legs.append({
            "leg": leg,
            "calls": calls,
            "success_rate": round(s["success"] / calls, 3) if calls else None,
            "parse_fail_rate": round(s["parse_failed"] / calls, 3) if calls else None,
            "no_sources_rate": round(s["no_sources"] / calls, 3) if calls else None,
            "avg_latency_sec": round(s["latency_sum"] / s["latency_n"], 1) if s["latency_n"] else None,
            "rejected_total": s["rejected_total"],
            "today_calls": used,
            "daily_cap": cap,
            "daily_remaining": (max(0, cap - used) if cap else None),
            "weak_or_quota_today": weak_today.get(leg, 0),
        })
    return {"legs": legs, "generated_at": utc_now()}


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


UI_DIR = ROOT / "ui"
UI_FALLBACK_HTML = "<!doctype html><meta charset=utf-8><h1>ui/index.html is missing</h1>"


def load_index_html() -> str:
    """The UI is a plain file on disk, read per request — edits show up on refresh (no build,
    no server restart), which lets frontend work proceed in parallel with backend work."""
    try:
        return (UI_DIR / "index.html").read_text(encoding="utf-8")
    except OSError:
        return UI_FALLBACK_HTML


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
            self.send_text(200, load_index_html(), "text/html; charset=utf-8")
            return

        if path.startswith("/ui/"):
            asset = (UI_DIR / path[len("/ui/"):]).resolve()
            if asset.is_file() and UI_DIR.resolve() in asset.parents:
                content_type = "application/json" if asset.suffix == ".json" else "text/plain"
                if asset.suffix in {".html", ".css", ".js"}:
                    content_type = {"html": "text/html", "css": "text/css", "js": "text/javascript"}[asset.suffix[1:]]
                self.send_text(200, asset.read_text(encoding="utf-8"), f"{content_type}; charset=utf-8")
            else:
                self.send_json(404, {"error": "asset_not_found"})
            return

        if path == "/api/runs":
            self.send_json(200, {"runs": list_runs()})
            return

        if path == "/api/scoreboard":
            self.send_json(200, build_scoreboard())
            return

        match = re.fullmatch(r"/api/runs/([^/]+)/events", path)
        if match:
            self.stream_run_events(Path(urllib.parse.unquote(match.group(1))).name)
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

    def stream_run_events(self, run_id: str) -> None:
        """SSE: replay events.jsonl from the start, then tail it until the run reaches a
        terminal state. One `data:` frame per event line; a final `event: done` frame closes."""
        run_dir = RUNS_DIR / run_id
        if not run_dir.exists():
            self.send_json(404, {"error": "run_not_found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        events_path = run_dir / "events.jsonl"
        pos = 0
        # Hard ceiling so the streaming thread can't live forever even if a run never reaches a
        # terminal state and the client stays connected (a run also can't outlive STALE_AFTER_SEC).
        deadline = time.monotonic() + STALE_AFTER_SEC + 120
        try:
            while True:
                chunk = b""
                if events_path.exists():
                    with events_path.open("rb") as f:
                        f.seek(pos)
                        chunk = f.read()
                        pos = f.tell()
                if chunk:
                    for line in chunk.decode("utf-8", errors="replace").splitlines():
                        if line.strip():
                            self.wfile.write(b"data: " + line.encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                meta = read_json(run_dir / "run.json", {}) or {}
                if (meta.get("status") not in {"queued", "running"} and not chunk) or time.monotonic() > deadline:
                    self.wfile.write(b"event: done\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)

        match = re.fullmatch(r"/api/runs/([^/]+)/cancel", parsed.path)
        if match:
            run_id = Path(urllib.parse.unquote(match.group(1))).name
            if request_cancel(run_id):
                emit_event(RUNS_DIR / run_id, "cancel_requested")
                self.send_json(202, {"status": "cancelling"})
            else:
                self.send_json(409, {"error": "run_not_active"})
            return

        match = re.fullmatch(r"/api/runs/([^/]+)/calls/([^/]+)/cancel", parsed.path)
        if match:
            run_id = Path(urllib.parse.unquote(match.group(1))).name
            record_id = Path(urllib.parse.unquote(match.group(2))).name
            if kill_one_call(run_id, record_id):
                emit_event(RUNS_DIR / run_id, "call_cancel_requested", record_id=record_id)
                self.send_json(202, {"status": "cancelling_call"})
            else:
                self.send_json(409, {"error": "call_not_active"})
            return

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
            config = make_config(payload.get("effort"), payload.get("sites"), payload.get("disabled"))
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
    parser.add_argument(
        "--disable",
        action="append",
        default=None,
        help="Turn a vendor OFF for this run to save its quota: gpt/codex, gemini, or claude "
             "(repeatable or comma-separated). The remaining vendor(s) do everything.",
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

    config = make_config(args.effort, ",".join(args.site) if args.site else None,
                         ",".join(args.disable) if args.disable else None)
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
