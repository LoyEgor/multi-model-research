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


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
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
# Hold back this many of Claude's remaining daily calls from SEARCH, so one run's discovery can't
# eat the allowance the thin judge/arbiter seats (adjudication, review, synthesis) still need later
# in the day. Claude is the scarce leg; the free codex/gemini legs carry the bulk of the search.
CLAUDE_SEARCH_RESERVE = max(0, env_int("RESEARCH_CLAUDE_SEARCH_RESERVE", 8))
RAW_TIMEOUT_SEC = max(60, env_int("RESEARCH_MODEL_TIMEOUT_SEC", 900))
URL_TIMEOUT_SEC = max(2, env_int("RESEARCH_URL_TIMEOUT_SEC", 12))
MAX_PRIMARY_WORKERS = max(2, env_int("RESEARCH_MAX_PRIMARY_WORKERS", 16))
MAX_VERIFY_WORKERS = max(2, env_int("RESEARCH_MAX_VERIFY_WORKERS", 8))
# Small pool that warms the run URL cache concurrently with the still-running search calls, so the
# batch verify hits a warm cache instead of starting all network I/O only after the slowest call.
MAX_PREFETCH_WORKERS = max(1, env_int("RESEARCH_MAX_PREFETCH_WORKERS", 4))
# A run with no heartbeat for longer than one full model call + slack is dead, not "running".
STALE_AFTER_SEC = RAW_TIMEOUT_SEC + 600
# Wall-clock budget guard. Each effort profile sets time_budget_sec (TOTAL run cap). Optional rounds
# (extra rescue/coverage/frontier rounds, adversarial review, fact-check) are skipped once only
# SYNTHESIS_RESERVE_SEC remains, so the always-run synthesis stays inside the budget. Under 90 min.
SYNTHESIS_RESERVE_SEC = max(120, env_int("RESEARCH_SYNTHESIS_RESERVE_SEC", 900))
# The adversarial review and final fact-check run AFTER synthesis, so the synthesis reserve no longer
# needs protecting for them — gating those two on the full SYNTHESIS_RESERVE_SEC would skip the deep/
# max signature stages with plenty of budget left. They get their own much smaller post-synthesis
# reserve instead (just enough to finish writing final.md).
POST_SYNTHESIS_RESERVE_SEC = max(30, env_int("RESEARCH_POST_SYNTHESIS_RESERVE_SEC", 120))
# Interactive clarify gate: how long to wait for the user's answer to a clarifying question before
# proceeding on the assumed default reading. Only ever reached on interactive (UI / --ask) runs; the
# non-interactive CLI/API default never asks and never waits. The waited time is excluded from the
# run's time budget (execute_research pushes `started` forward), so parking on a question never eats
# into the search itself.
CLARIFY_TIMEOUT_SEC = max(1, env_int("RESEARCH_CLARIFY_TIMEOUT_SEC", 45))
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


def apply_call_to_breaker(run_id: str, leg: str, success: bool, dropped_as_straggler: bool) -> bool:
    """Feed a completed call's outcome to the per-run circuit breaker, EXCEPT straggler-killed calls:
    those are intentional scheduling drops of a healthy-but-slow leg (the fast-leg-aware quorum reaps
    codex on purpose), not leg failures, so they must count as neither pass nor fail — otherwise the
    scheduler's own kills would disable the very slow leg we still want for the deeper phases.
    Returns True when this result just tripped the breaker."""
    if dropped_as_straggler:
        return False
    return record_leg_result(run_id, leg, success)


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


# Latency reality (measured, runs/*/raw/*.meta.json): codex is the long pole (median 220-330s,
# tail >450s), while gemini (~15s) and claude (~35s) are 6-20x faster. The wall-clock critical
# path must therefore be the FAST legs, not codex — so the fast legs get generous concurrency
# (fire several in parallel per phase) and codex is bounded by fan-out (codex_task_cap) + straggler
# grace instead. Concurrency caps bound only PARALLELISM; total calls stay bounded by the per-run
# leg budgets, so raising these does not spend more quota — it just stops the fast legs serializing.
GEMINI_CONCURRENCY = threading.Semaphore(max(1, env_int("RESEARCH_GEMINI_CONCURRENCY", 3)))
# Claude's subscription pool is more capped than gemini but a handful of concurrent search calls is
# well within a Max plan; one-at-a-time (the old default) added up to ~100s of pure queue wait.
CLAUDE_CONCURRENCY = threading.Semaphore(max(1, env_int("RESEARCH_CLAUDE_CONCURRENCY", 3)))
# Only the scarce-quota fast legs get a per-leg concurrency cap. Codex is intentionally absent — its
# footprint is bounded by codex_task_cap (few slow calls per phase) and the fast-leg-aware straggler
# quorum, so it never has enough concurrent jobs to need a semaphore.
LEG_SEMAPHORES = {"gemini": GEMINI_CONCURRENCY, "claude": CLAUDE_CONCURRENCY}
# The slow-frontier legs: excluded from the straggler quorum so a phase declares "enough" the moment
# the FAST legs have largely returned, then gives these a bounded grace to land (see
# collect_with_straggler_drop). Membership is by measured latency class, not vendor identity.
SLOW_LEGS = frozenset({"codex"})
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


def iter_jsonl_rows(path: object):
    """Tolerant reader for our append-only JSONL logs (served-models / model-stats): yields one
    parsed object per line, silently skipping blank and unparseable lines (a writer may be mid-append
    or a line got truncated) and yielding nothing for a missing/unreadable file. Single reader shared
    by every log scan so their corruption tolerance can never drift apart."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def daily_call_counts(day: str | None = None) -> dict[str, int]:
    """Count successful leg calls logged today (UTC) in served-models.jsonl — the basis for
    quota-aware pacing. day defaults to today's UTC date (YYYY-MM-DD)."""
    day = day or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    counts: dict[str, int] = {}
    for row in iter_jsonl_rows(SERVED_MODELS):
        if str(row.get("ts", "")).startswith(day):
            leg = row.get("leg")
            if leg:
                counts[leg] = counts.get(leg, 0) + 1
    return counts


def paced_budget(leg: str, requested: int, reserve: int = 0, counts: dict | None = None) -> tuple[int, int]:
    """Clamp a leg's per-run budget to the remaining daily allowance. `reserve` withholds that many
    of the day's remaining calls from THIS budget (used to keep some Claude allowance for the
    non-search judge seats). Pass `counts` (from one daily_call_counts() call) to price several legs
    without re-reading the log per leg. Returns (budget, remaining), remaining = true daily remaining
    BEFORE the reserve."""
    cap = DAILY_CAPS.get(leg)
    if not cap:
        return requested, -1
    used = (counts if counts is not None else daily_call_counts()).get(leg, 0)
    remaining = max(0, cap - used)
    grantable = max(0, remaining - reserve)
    return min(requested, grantable), remaining


# Live subprocess registry per run: lets a fan-out phase kill its stragglers (and only its own
# calls — phases never overlap within a run) once the quorum is in.
PROC_REGISTRY_LOCK = threading.Lock()
RUN_PROCS: dict[str, dict[str, int]] = {}
RUN_DROPPED: dict[str, set[str]] = {}
# Protected calls (plan_audit / gap_audit) overlap a straggler-dropping fan-out phase; a phase's
# quorum kill must NOT reap them. Cancellation still does (kill_stragglers include_protected=True).
RUN_PROTECTED: dict[str, set[str]] = {}


def register_proc(run_id: str, record_id: str, pid: int, protected: bool = False) -> None:
    with PROC_REGISTRY_LOCK:
        RUN_PROCS.setdefault(run_id, {})[record_id] = pid
        if protected:
            RUN_PROTECTED.setdefault(run_id, set()).add(record_id)


def unregister_proc(run_id: str, record_id: str) -> None:
    with PROC_REGISTRY_LOCK:
        RUN_PROCS.get(run_id, {}).pop(record_id, None)
        RUN_PROTECTED.get(run_id, set()).discard(record_id)


def kill_stragglers(run_id: str, include_protected: bool = False) -> list[str]:
    with PROC_REGISTRY_LOCK:
        procs = dict(RUN_PROCS.get(run_id) or {})
        protected = set(RUN_PROTECTED.get(run_id) or ())
    killed = []
    for record_id, pid in procs.items():
        if not include_protected and record_id in protected:
            continue  # a concurrent audit overlaps this phase; only cancellation may reap it
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
        RUN_PROTECTED.pop(run_id, None)


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
    # Cancellation reaps EVERY in-flight call, including protected audits; queued ones block at spawn.
    kill_stragglers(run_id, include_protected=True)
    return True


def clear_cancel(run_id: str) -> None:
    with CANCEL_LOCK:
        CANCELLED_RUNS.discard(run_id)


# Interactive clarify gate: the answer to a clarifying question arrives out-of-band via the HTTP
# clarify endpoint while execute_research parks in a bounded poll loop. Belt-and-suspenders like the
# cancel flag — the answer is stored BOTH in this in-memory registry (same-process fast path) AND in
# runs/<id>/clarify.json on disk, and the poll loop reads whichever is present.
CLARIFY_LOCK = threading.Lock()
CLARIFY_ANSWERS: dict[str, dict] = {}


def _normalize_clarify_payload(payload: object) -> dict:
    """Coerce a raw clarify request body into {'answer': str, 'skip': bool}. An empty/blank answer
    (or an explicit skip flag) means 'proceed now with the default reading'."""
    payload = payload if isinstance(payload, dict) else {}
    answer = str(payload.get("answer") or "").strip()
    skip = bool(payload.get("skip")) or not answer
    return {"answer": answer, "skip": skip}


def submit_clarification(run_dir: Path, run_id: str, payload: object) -> dict:
    """Record a clarify response from the HTTP endpoint into the in-memory registry AND clarify.json.
    Returns the normalized entry. Mirrors the cancel flag's dual store so an answer is never lost to a
    thread-visibility gap between the request handler and the waiting pipeline thread."""
    entry = _normalize_clarify_payload(payload)
    with CLARIFY_LOCK:
        CLARIFY_ANSWERS[run_id] = entry
    try:
        write_json(run_dir / "clarify.json", entry)
    except OSError:
        pass  # the registry alone suffices in-process; disk is the cross-process fallback
    return entry


def read_clarification(run_dir: Path, run_id: str) -> dict | None:
    """Return a stored clarify response (registry first, then clarify.json), or None if none yet."""
    with CLARIFY_LOCK:
        entry = CLARIFY_ANSWERS.get(run_id)
    if entry is not None:
        return entry
    data = read_json(run_dir / "clarify.json", None)
    return data if isinstance(data, dict) else None


def clear_clarification(run_id: str) -> None:
    with CLARIFY_LOCK:
        CLARIFY_ANSWERS.pop(run_id, None)


def wait_for_clarification(run_dir: Path, run_id: str, timeout_sec: float,
                           check_cancel=None, poll_interval: float = 0.5) -> dict | None:
    """Block up to timeout_sec for a clarify response, polling the registry/clarify.json every
    poll_interval seconds. Returns the response dict as soon as one lands, or None on timeout. Reads
    once before checking the deadline so a pre-existing answer returns immediately even at timeout 0.
    check_cancel(), when given, runs each tick so a cancel during the wait still aborts promptly."""
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while True:
        entry = read_clarification(run_dir, run_id)
        if entry is not None:
            return entry
        if check_cancel is not None:
            check_cancel()
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval)


def should_ask_clarify(config: dict, intent: dict) -> tuple[bool, str, list]:
    """Gate predicate for the interactive clarify question. Returns (ask, question, alternatives).
    Asks ONLY when the run is interactive AND the plan flagged the request ambiguous AND a concrete
    question was produced — so non-interactive (CLI default / API-without-flag) runs never block."""
    intent = intent or {}
    question = str(intent.get("clarify_question") or "").strip()
    alternatives = [str(a).strip() for a in (intent.get("alternatives") or []) if str(a).strip()]
    ask = bool(config.get("interactive") and intent.get("ambiguous") and question)
    return ask, question, alternatives


def build_clarified_prompt(original_prompt: str, answer: str) -> str:
    """Fold an interactive clarification into the prompt so EVERY downstream stage (search, gap
    audit, coverage/frontier, rescue, synthesis, fact-check) sees the disambiguated request — not
    just the re-decompose. execute_research rebinds its local `prompt` to this. The original prompt
    is preserved verbatim in run.json (init_run) for the UI header, so nothing is lost."""
    return f"{original_prompt}\n\n---\nUser clarification (authoritative): {answer}"


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


# Per-vendor tier/effort the owner can dial (UI per-run, not persisted; CLI/API/env for headless).
# ONE setting per vendor, applied to EVERY role that vendor plays this run — search AND the judge
# seats share one codex effort and one Claude tier. WHO plays which role is decided by the effort
# scheme, not here. Defaults are the strongest tier; the owner only dials down to experiment.
VENDOR_TIER_DEFAULTS = {"codex": "xhigh", "gemini": "high", "claude": "opus"}
CODEX_EFFORTS = ("medium", "high", "xhigh")
CLAUDE_TIERS = ("sonnet", "opus")  # Opus is the hard ceiling — fable/mythos are blocked in the leg.
GEMINI_TIERS = {"high": "Gemini 3.1 Pro (High)", "low": "Gemini 3.1 Pro (Low)"}
_VENDOR_TIER_ENV = {"codex": "RESEARCH_CODEX_TIER", "gemini": "RESEARCH_GEMINI_TIER",
                    "claude": "RESEARCH_CLAUDE_TIER"}


def normalize_vendor_tiers(raw: object) -> dict:
    """Clamp a per-vendor tier/effort override to the allowed set, falling back to the max default.
    Precedence: passed-in (UI/API/CLI) > env > default. Unknown values are ignored, not errored."""
    tiers = dict(VENDOR_TIER_DEFAULTS)
    allowed = {"codex": CODEX_EFFORTS, "gemini": tuple(GEMINI_TIERS), "claude": CLAUDE_TIERS}
    raw = raw if isinstance(raw, dict) else {}
    for vendor, options in allowed.items():
        env_val = str(os.environ.get(_VENDOR_TIER_ENV[vendor]) or "").strip().lower()
        if env_val in options:
            tiers[vendor] = env_val
        val = str(raw.get(vendor) or "").strip().lower()
        if val in options:
            tiers[vendor] = val
    return tiers


# Vendors' Gemini model label for a run (agy has no per-call model flag we pass positionally, so
# call_model reads it here by run_id — mirrors the USER_DISABLED run-scoped store).
RUN_GEMINI_MODEL: dict[str, str] = {}


def set_run_gemini_model(run_id: str, label: str | None) -> None:
    with CANCEL_LOCK:
        if label:
            RUN_GEMINI_MODEL[run_id] = label
        else:
            RUN_GEMINI_MODEL.pop(run_id, None)


def run_gemini_model(run_id: str) -> str | None:
    with CANCEL_LOCK:
        return RUN_GEMINI_MODEL.get(run_id)


def clear_run_gemini_model(run_id: str) -> None:
    with CANCEL_LOCK:
        RUN_GEMINI_MODEL.pop(run_id, None)

# ALL frontier families (codex/gemini/claude) search at EVERY level — they run in parallel, so a
# third strong leg adds breadth without adding wall-clock. Effort scales by BREADTH (tasks) and the
# number of PASSES (recheck / coverage / frontier rounds, variants) and by verification layers
# (cross-vendor review, Claude adjudication), NOT by model tier: search never downgrades — flagship
# only, guards in lib/legs/ask_*.sh stay in force. claude_search_model = the tier Claude searches at
# (opus); the separate judge/arbiter seat tier is claude_model. Any role's model can be overridden
# via config["roles"] (see resolve_roles). Opus is the HARD ceiling (owner decision 2026-06-11):
# never Fable/Mythos-class — the wrapper blocks them outright. Per-run daily pacing still trims the
# Claude search budget when the day's allowance runs low (paced_budget), degrading to codex+gemini.
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
        "search_timeout_sec": 420,
        "recheck_timeout_sec": 300,
        "straggler_grace_sec": 45,
        "codex_task_cap": 1,
        "model_verify_cap": 2,
        "time_budget_sec": 1500,
        "gemini_call_budget": 6,
        "search_legs": ["codex", "gemini", "claude"],
        "claude_search_budget": 3,
        "query_variants_per_task": 1,
        "frontier_rounds": 0,
        "final_factcheck": False,
        "structured_decompose": True,
        "differentiate_legs": False,
        "angle_variants_per_task": 0,
        "coverage_rounds": 0,
        "plan_audit": False,
        "gap_audit": False,
        "adjudicate_samples": 1,
    },
    2: {
        "effort": "standard",
        "task_count": 4,
        "recheck_rounds": 1,
        "max_recheck_items": 6,
        "recheck_legs": 1,
        "review_legs": [],
        "adjudicate_disputes": True,
        "search_timeout_sec": 480,
        "recheck_timeout_sec": 360,
        "straggler_grace_sec": 90,
        "codex_task_cap": 2,
        "model_verify_cap": 3,
        "time_budget_sec": 2400,
        "gemini_call_budget": 9,
        "search_legs": ["codex", "gemini", "claude"],
        "claude_search_budget": 4,
        "query_variants_per_task": 1,
        "frontier_rounds": 0,
        "final_factcheck": False,
        "structured_decompose": True,
        "differentiate_legs": False,
        "angle_variants_per_task": 0,
        "coverage_rounds": 0,
        "plan_audit": True,
        "gap_audit": True,
        "adjudicate_samples": 1,
    },
    3: {
        "effort": "deep",
        "task_count": 5,
        "recheck_rounds": 1,
        "max_recheck_items": 6,
        "recheck_legs": 1,
        "review_legs": ["gemini"],
        "adjudicate_disputes": True,
        "search_timeout_sec": 600,
        "recheck_timeout_sec": 480,
        "straggler_grace_sec": 200,
        "codex_task_cap": 3,
        "model_verify_cap": 4,
        "time_budget_sec": 4200,
        "gemini_call_budget": 12,
        "search_legs": ["codex", "gemini", "claude"],
        "claude_search_budget": 8,
        "query_variants_per_task": 1,
        "frontier_rounds": 1,
        "final_factcheck": True,
        "structured_decompose": True,
        "differentiate_legs": True,
        "angle_variants_per_task": 1,
        "coverage_rounds": 1,
        "plan_audit": True,
        "gap_audit": True,
        "adjudicate_samples": 3,
    },
    4: {
        "effort": "max",
        "task_count": 6,
        "recheck_rounds": 1,
        "max_recheck_items": 8,
        "recheck_legs": 2,
        "review_legs": ["gemini", "claude"],
        "adjudicate_disputes": True,
        "search_timeout_sec": 900,
        "recheck_timeout_sec": 600,
        "straggler_grace_sec": 300,
        "codex_task_cap": 6,
        "model_verify_cap": 6,
        "time_budget_sec": 5100,
        "gemini_call_budget": 16,
        "search_legs": ["codex", "gemini", "claude"],
        "claude_search_budget": 12,
        "query_variants_per_task": 2,
        "frontier_rounds": 2,
        "final_factcheck": True,
        "structured_decompose": True,
        "differentiate_legs": True,
        "angle_variants_per_task": 2,
        "coverage_rounds": 1,
        "plan_audit": True,
        "gap_audit": True,
        "adjudicate_samples": 3,
    },
}
# Once this fraction of a phase's FAST-leg calls has returned (the slow frontier legs are excluded
# from the count — see collect_with_straggler_drop's fast_quorum_total), the remaining stragglers
# get a bounded grace window (max of the profile grace and half the median completed latency) and
# are then killed — their items flow into the rescue pool instead of stalling the whole phase.
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


def make_config(effort: object = None, sites: object = None, disabled: object = None,
                vendor_tiers: object = None, excluded_sites: object = None,
                interactive: object = False) -> dict:
    level = parse_effort(effort)
    config = dict(EFFORT_PROFILES[level])
    config["effort_level"] = level
    # Interactive runs (UI, or CLI --ask) may pause once for a clarifying question; non-interactive
    # runs (CLI default, API without the flag) never do. Default False so scripts never block.
    config["interactive"] = bool(interactive)
    config["sites"] = normalize_sites(sites)
    # Blocklist (inverse of the sites scope): domains the user never wants back. An explicit scope
    # wins on overlap, so the two fields can never contradict each other.
    config["excluded_sites"] = [d for d in normalize_sites(excluded_sites) if d not in config["sites"]]

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

    # Per-vendor tier/effort: ONE setting per vendor, applied to every role it plays. Overwrites the
    # profile so search and the judge seats share one codex effort and one Claude tier; gemini's tier
    # is carried and set as AGY_MODEL per call. Default max; the owner dials down only to experiment.
    tiers = normalize_vendor_tiers(vendor_tiers)
    config["vendor_tiers"] = tiers
    config["search_effort"] = config["judge_effort"] = tiers["codex"]
    config["claude_model"] = config["claude_search_model"] = tiers["claude"]
    config["gemini_model"] = GEMINI_TIERS[tiers["gemini"]]
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
    "price_basis",
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


# Pricing BASIS: the axis a price lives on. A subscription's monthly fee, a one-off purchase, a
# per-token API rate and a free tier are NOT comparable numbers — comparing them silently is the
# "cheapest = free" / "per-token beats monthly" bug. We carry the basis on every intent + finding,
# normalize what we can to a common monthly-USD axis, and TAG (never drop) what we can't.
PRICE_BASES = {"one_time", "subscription_monthly", "subscription_yearly", "usage_metered",
               "per_seat_monthly", "rental_monthly", "free", "unknown"}
_BASIS_CLASS = {
    "one_time": "one_time",
    "subscription_monthly": "recurring", "subscription_yearly": "recurring",
    "per_seat_monthly": "recurring", "rental_monthly": "recurring",
    "usage_metered": "metered", "free": "free", "unknown": "unknown",
}
_PERIOD_TO_MONTHLY = {"subscription_monthly": 1.0, "subscription_yearly": 1 / 12.0,
                      "per_seat_monthly": 1.0, "rental_monthly": 1.0}
_BASIS_SYNONYMS = {  # substring -> canon; longest key wins so "per 1k tokens" beats "per"
    "per month": "subscription_monthly", "monthly": "subscription_monthly", "/mo": "subscription_monthly",
    "per mo": "subscription_monthly", "a month": "subscription_monthly",
    "per year": "subscription_yearly", "yearly": "subscription_yearly", "annual": "subscription_yearly",
    "/yr": "subscription_yearly", "per annum": "subscription_yearly", "a year": "subscription_yearly",
    "per token": "usage_metered", "per 1k tokens": "usage_metered", "per 1m tokens": "usage_metered",
    "per request": "usage_metered", "per api call": "usage_metered", "metered": "usage_metered",
    "usage-based": "usage_metered", "usage based": "usage_metered", "pay as you go": "usage_metered",
    "pay-as-you-go": "usage_metered", "per gb": "usage_metered",
    "per seat": "per_seat_monthly", "per user": "per_seat_monthly",
    "one time": "one_time", "one-time": "one_time", "once": "one_time", "lifetime": "one_time",
    "perpetual": "one_time", "outright": "one_time",
    "free": "free", "$0": "free", "no cost": "free", "gratis": "free",
}
# Longest-first so "per 1k tokens" wins over "per"; precomputed once (canon_basis runs per finding).
_BASIS_SYNONYM_KEYS = sorted(_BASIS_SYNONYMS, key=len, reverse=True)


def canon_basis(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if not text:
        return "unknown"
    if text in PRICE_BASES:
        return text
    for key in _BASIS_SYNONYM_KEYS:
        if key in text:
            return _BASIS_SYNONYMS[key]
    return "unknown"


def basis_class(basis: object) -> str:
    return _BASIS_CLASS.get(canon_basis(basis), "unknown")


def to_monthly_usd(price_usd: object, basis: object, intent: dict | None) -> float | None:
    """Put a USD price on a common axis: USD per month. Returns None when the basis has no monthly
    equivalent (one_time / free / unknown, or metered with no usage assumption) — that None is what
    makes such an offer deliberately incomparable to a subscription."""
    if price_usd is None:
        return None
    b = canon_basis(basis)
    if b in _PERIOD_TO_MONTHLY:
        return round(float(price_usd) * _PERIOD_TO_MONTHLY[b], 4)  # per_seat priced at 1 seat
    if b == "usage_metered":
        units = (intent or {}).get("monthly_usage_units")
        return round(float(price_usd) * float(units), 4) if units else None
    return None


def set_monthly_usd(finding: dict) -> None:
    """Refresh the monthly-normalized USD (derived from price_usd + basis) so recurring cards show a
    consistent ~$/mo. Call anywhere price_usd/currency/basis is finalized. Metered needs an intent
    usage assumption, so it stays None here and is filled later by intent_rejection when available."""
    finding["price_usd_monthly"] = to_monthly_usd(finding.get("price_usd"), finding.get("price_basis"), None)


def parse_count(value: object) -> float | None:
    """Parse a plain quantity (e.g. monthly_usage_units) — NOT a price, so grouping commas mean
    thousands ('1,000,000' -> 1000000), not decimals. Returns a positive float or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    digits = re.sub(r"[,\s_]", "", str(value))
    match = re.search(r"\d+(?:\.\d+)?", digits)
    if not match:
        return None
    number = float(match.group())
    return number if number > 0 else None


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
    finding["price_basis"] = canon_basis(finding.get("price_basis"))
    set_monthly_usd(finding)  # consistent ~$/mo for every recurring finding, not only priced-out ones

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

    # price_basis defaults to the non-empty "unknown", so the loop above never upgrades it — a leg
    # that identified the basis should win over one that didn't.
    if basis_class(existing.get("price_basis")) == "unknown" and basis_class(incoming.get("price_basis")) != "unknown":
        existing["price_basis"] = incoming["price_basis"]

    # "Cheaper" is decided in USD (comparable across currencies), then native price/currency
    # follow the chosen candidate.
    if incoming.get("price") is not None:
        inc_usd = incoming.get("price_usd")
        cur_usd = existing.get("price_usd")
        if existing.get("price") is None or (inc_usd is not None and (cur_usd is None or inc_usd < cur_usd)):
            existing["price"] = incoming["price"]
            existing["currency"] = incoming.get("currency")
            existing["price_usd"] = inc_usd
            set_monthly_usd(existing)  # cheapest candidate changed the price; keep ~$/mo in step

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
# One stable desktop UA for every verify/live-check fetch. NOT rotated: at our request volume a
# rotating UA reads as MORE bot-like (and makes behavior nondeterministic across a run).
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
LIVE_BODY_CAP = 2_500_000
# Anti-bot backoff: retry an anti-bot response (429, bot-wall 403, once for 503) a couple of times
# with growing delay, so a protected portal's throttle is a soft "couldn't verify" (rescuable),
# not a false disproof. Bounded so worst-case added latency per URL stays ~20s.
BOT_BLOCK_MAX_RETRIES = 2
BOT_BLOCK_BACKOFF = (2.0, 5.0)      # seconds before retry 1, retry 2
BOT_BLOCK_RETRY_AFTER_CAP = 15.0    # honor a sane Retry-After, but never wait longer than this
BOT_BLOCK_TOTAL_CAP = 18.0          # cumulative backoff budget per URL (keeps worst case bounded)
BOT_WALL_TINY_BODY = 512            # a 403 with a body this small smells like a challenge stub
BOT_WALL_MARKERS = ("cloudflare", "captcha", "cf-chl", "just a moment", "attention required",
                    "checking your browser", "access denied", "are you a robot",
                    "verify you are human", "px-captcha", "datadome",
                    "please enable javascript and cookies")
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


class HostBlockRegistry:
    """Run-scoped, thread-safe set of hosts that already returned a bot-wall this run. Once a host
    has bot-walled, further URLs on it get a single polite attempt with no retries (don't hammer)."""

    def __init__(self) -> None:
        self._hosts: set[str] = set()
        self._lock = threading.Lock()

    def is_blocked(self, host: str) -> bool:
        with self._lock:
            return host in self._hosts

    def mark(self, host: str) -> None:
        with self._lock:
            self._hosts.add(host)


class _CacheFlight:
    """One in-progress compute for a key: waiters block on `done`, then read `result` once."""
    __slots__ = ("done", "result")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: dict | None = None


class UrlCheckCache:
    """Run-scoped, thread-safe memo of URL-check network I/O so the batch verify can hit a warm cache
    that the concurrent prefetch (make_prefetch_collector) filled while the search calls were still
    running. Two namespaces per NORMALIZED URL: 'verify' (verify_url results) and 'live'
    (live_listing_check results).

    ONLY definitive successes are retained (verify: result['ok']; live: the page fetch succeeded).
    Transient failures — timeout, 503, bot_blocked, network errors, and even a 4xx — are NOT
    retained, because a later rescue/coverage/frontier round must be free to re-fetch a URL that
    failed once (retaining the failure would make such an item impossible to ever verify this run).

    Single-flight per key: while one thread computes a key, concurrent waiters block and share that
    flight's result exactly once (even a failure) instead of issuing duplicate fetches; a failure is
    still not retained, so the NEXT lookup after the flight recomputes it. Unbounded on purpose: a
    run sees at most a few hundred URLs."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], dict] = {}
        self._inflight: dict[tuple[str, str], _CacheFlight] = {}
        self._guard = threading.Lock()

    @staticmethod
    def _retainable(result: object) -> bool:
        # Both namespaces converge on ok==True as "definitive success worth keeping for the run".
        return isinstance(result, dict) and result.get("ok") is True

    def get_or_compute(self, namespace: str, url: object, compute) -> dict:
        key = (namespace, normalize_url_for_key(url) or str(url))
        while True:
            with self._guard:
                if key in self._store:
                    return self._store[key]
                flight = self._inflight.get(key)
                if flight is None:
                    flight = _CacheFlight()
                    self._inflight[key] = flight
                    leader = True
                else:
                    leader = False
            if leader:
                result = None
                produced = False
                try:
                    result = compute()
                    produced = True
                finally:
                    with self._guard:
                        if produced and self._retainable(result):
                            self._store[key] = result
                        self._inflight.pop(key, None)
                    flight.result = result if produced else None
                    flight.done.set()
                return result
            # Waiter: share the in-flight leader's result once. A retained success is now in
            # _store; a non-retained failure is read from the flight (and never cached), so any
            # lookup that arrives AFTER this flight starts a fresh compute.
            flight.done.wait()
            with self._guard:
                if key in self._store:
                    return self._store[key]
            if flight.result is not None:
                return flight.result
            # Leader raised before producing a result — retry as a fresh flight.


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    """Retry-After in seconds when the header is present and a sane non-negative number (capped);
    None otherwise — the HTTP-date form is ignored and we fall back to fixed backoff."""
    try:
        raw = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
    except Exception:
        raw = None
    if not raw:
        return None
    try:
        secs = float(str(raw).strip())
    except ValueError:
        return None
    if not (secs >= 0):  # rejects negatives AND nan (nan >= 0 is False) in one shot
        return None
    return min(secs, BOT_BLOCK_RETRY_AFTER_CAP)


def _smells_like_bot_wall(exc: urllib.error.HTTPError) -> bool:
    """A 403 that is a genuine anti-bot CHALLENGE, not an ordinary authorization denial. Cloudflare
    (and other CDNs) front EVERY response on a proxied site, so `Server: cloudflare` / cf-ray alone
    is NOT evidence — origin 403s carry them too, and treating every CF-fronted 403 as a wall burns
    retries and wrongly blocks the whole host. We require POSITIVE challenge signal: a
    `cf-mitigated: challenge` header, a challenge marker in the body, or a tiny body TOGETHER WITH a
    CDN edge marker (cloudflare / cf-ray). A plain CF-proxied 403 with a normal body is a real 403."""
    try:
        headers = getattr(exc, "headers", None)
        cf_mitigated = edge_marker = False
        if headers is not None:
            server = str(headers.get("Server") or "").lower()
            cf_mitigated = "challenge" in str(headers.get("cf-mitigated") or "").lower()
            edge_marker = "cloudflare" in server or bool(headers.get("cf-ray"))
        if cf_mitigated:
            return True
        body = exc.read(BOT_WALL_TINY_BODY * 4) if hasattr(exc, "read") else b""
    except Exception:
        return False
    text = body.decode("utf-8", errors="replace").lower()
    if any(marker in text for marker in BOT_WALL_MARKERS):
        return True
    return edge_marker and 0 < len(body) <= BOT_WALL_TINY_BODY


def http_fetch(url: object, method: str = "GET", timeout: float = URL_TIMEOUT_SEC,
               read_body: bool = False, retry: bool = True,
               host_registry: HostBlockRegistry | None = None) -> dict:
    """Single fetch path for verify/live-check: realistic browser headers + bounded anti-bot
    backoff. Returns {ok, status, body, reason, bot_blocked}. Retries only anti-bot responses
    (429, bot-wall 403, and once for 503); 404/other 4xx-5xx are real signals and pass straight
    through. When a host already bot-walled this run it gets one attempt with no retries."""
    host = urllib.parse.urlsplit(str(url)).netloc.lower()
    def throttled() -> bool:
        # Re-read per attempt, not once up front: a concurrent fetch may bot-wall this host
        # mid-flight, and once it does we must stop spending our retry budget immediately.
        return bool(retry and host_registry and host and host_registry.is_blocked(host))
    slept = 0.0
    retries_used = 0
    while True:
        request = urllib.request.Request(str(url), headers=BROWSER_HEADERS, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", response.getcode()))
                body = response.read(LIVE_BODY_CAP).decode("utf-8", errors="replace") if read_body else None
                ok = 200 <= status < 400
                return {"ok": ok, "status": status, "body": body, "bot_blocked": False,
                        "reason": "ok" if ok else f"http_{status}"}
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            wall = status == 429 or (status == 403 and _smells_like_bot_wall(exc))
            if throttled() or not retry:
                allowed = 0
            elif wall:
                allowed = BOT_BLOCK_MAX_RETRIES
            elif status == 503:  # often an anti-bot / warm-up gate — give it one shot, not a full retry budget
                allowed = 1
            else:
                allowed = 0
            if retries_used < allowed and slept < BOT_BLOCK_TOTAL_CAP:
                delay = _retry_after_seconds(exc)
                if delay is None:
                    delay = BOT_BLOCK_BACKOFF[min(retries_used, len(BOT_BLOCK_BACKOFF) - 1)]
                delay = min(delay, BOT_BLOCK_TOTAL_CAP - slept)
                if delay > 0:
                    time.sleep(delay)
                slept += delay
                retries_used += 1
                continue
            if wall:
                if host_registry and host:
                    host_registry.mark(host)
                return {"ok": False, "status": status, "body": None, "bot_blocked": True, "reason": "bot_blocked"}
            return {"ok": False, "status": status, "body": None, "bot_blocked": False, "reason": f"http_{status}"}
        except urllib.error.URLError as exc:
            reason = exc.reason.__class__.__name__ if not isinstance(exc.reason, str) else exc.reason
            return {"ok": False, "status": None, "body": None, "bot_blocked": False, "reason": reason}
        except TimeoutError:
            return {"ok": False, "status": None, "body": None, "bot_blocked": False, "reason": "timeout"}
        except Exception as exc:
            return {"ok": False, "status": None, "body": None, "bot_blocked": False, "reason": exc.__class__.__name__}


def live_listing_check(url: object, timeout: float | None = None,
                       host_registry: HostBlockRegistry | None = None,
                       cache: UrlCheckCache | None = None) -> dict:
    """Adapter chain for live page facts: OLX embedded state → JSON-LD Offer → OpenGraph meta →
    generic price regex. Returns price, native currency, ad status, and the page <title>."""
    if cache is not None:
        return cache.get_or_compute(
            "live", url, lambda: live_listing_check(url, timeout=timeout, host_registry=host_registry))
    result: dict = {"ok": False, "live_price": None, "live_currency": None, "ad_status": None,
                    "page_title": None, "live_variants": {}, "reason": None}
    fetched = http_fetch(url, method="GET", timeout=timeout or URL_TIMEOUT_SEC * 2,
                         read_body=True, retry=True, host_registry=host_registry)
    if not fetched["ok"]:
        result["reason"] = fetched["reason"]
        result["bot_blocked"] = fetched.get("bot_blocked", False)
        return result
    html_body = fetched["body"] or ""
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


def apply_live_check(item: dict, intent: dict | None = None,
                     host_registry: HostBlockRegistry | None = None,
                     cache: UrlCheckCache | None = None) -> None:
    """Mutates a finding after its page was read: inactive ads get flagged for rejection,
    live price (compared in USD) overrides the model's claim, audit trail kept in
    price_candidates. Only runs for URLs with a known marketplace listing key.

    When the page bundles multiple tiers and the user (or the finding) names a required tier, the
    REQUESTED tier's price from the page wins over the listing-level/base price — fixing the
    'cheap Pro masquerading as Max 5x' failure."""
    if not listing_key(item.get("url")):
        return
    live = live_listing_check(item.get("url"), host_registry=host_registry, cache=cache)
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
    set_monthly_usd(item)  # live page overrode the price; keep ~$/mo in step
    item["disputed"] = False  # resolved by verified fact, not by vote


def apply_model_verdict(item: dict, verdict: dict, intent: dict | None = None) -> dict:
    """Fold a web-capable model's re-verification verdict (Q1) into a finding, in place, and return
    the synthetic url_check that stands in for the plain-HTTP liveness probe our client was
    bot-walled on. On a NOT-live / failed verdict the item stays rejected with the final,
    non-rescuable reason `model_check_failed`. On a live verdict the item is marked `model_verified`,
    its fields adopt the model's page-read values where present, and a synthetic live_check carries
    the page facts so the downstream semantic gates (content_mismatch) still have signal — but the
    calibrated confidence stays penalized vs a machine-live HTTP check (see calibrate_confidence)."""
    leg = verdict.get("leg")
    checked_at = verdict.get("checked_at") or utc_now()
    if not verdict.get("live"):
        # "Could not OPEN the page" (opened=false) is NOT evidence the offer is dead — the model's
        # own web tooling may be bot-walled exactly like our HTTP client. Only a verdict from a model
        # that actually READ the page may finalize; otherwise return None so the caller falls back to
        # the normal HTTP path and the item keeps its rescuable network rejection ("Unverified" slot).
        if not verdict.get("opened"):
            return None
        item["model_verified"] = False
        return {"ok": False, "reason": "model_check_failed", "method": "model",
                "model": leg, "checked_at": checked_at}
    final_url = verdict.get("url")
    if isinstance(final_url, str) and final_url.strip() and final_url.strip() != item.get("url"):
        # Keep item["url"] as the canonical key: rewriting it would change finding_dedupe_key and the
        # already-streamed rejected card for this offer would never reconcile with the promoted one.
        item["final_url"] = final_url.strip()
    for field in ("availability", "seller", "title"):
        val = verdict.get(field)
        if isinstance(val, str) and val.strip():
            item[field] = val.strip()
    price = parse_price(verdict.get("price"))
    if price is not None:
        cur = canon_currency(verdict.get("currency")) or item.get("currency")
        usd = to_usd(price, cur)
        if usd is not None:
            if item.get("price") is not None and item.get("price") != price:
                item["price_corrected_from"] = item.get("price")
            item["price"] = price
            item["currency"] = cur
            item["price_usd"] = usd
            set_monthly_usd(item)
    item["model_verified"] = True
    item["model_verified_by"] = leg
    item["live_check"] = {
        "ok": True, "method": "model", "model": leg,
        "live_price": item.get("price"), "live_currency": item.get("currency"),
        "page_title": verdict.get("title") or item.get("title"),
        "ad_status": "active", "reason": "model_confirmed",
    }
    return {"ok": True, "method": "model", "model": leg, "checked_at": checked_at}


def verify_url(url: object, timeout: float = URL_TIMEOUT_SEC,
               host_registry: HostBlockRegistry | None = None,
               cache: UrlCheckCache | None = None) -> dict:
    if not url:
        return {"ok": False, "reason": "missing_url", "status": None}
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"ok": False, "reason": "invalid_url", "status": None}
    # Cache only the network result (the two cheap early rejections above stay uncached and free).
    if cache is not None:
        return cache.get_or_compute(
            "verify", url, lambda: verify_url(url, timeout=timeout, host_registry=host_registry))

    # HEAD is a cheap liveness probe; many hosts 405/403/429 it, so a failed HEAD just falls through
    # to GET, which carries the anti-bot retry + per-host politeness (the HEAD probe never retries
    # and never marks the host, so it can't pre-throttle the real GET).
    head = http_fetch(url, method="HEAD", timeout=timeout, retry=False, host_registry=None)
    if head["ok"]:
        return {"ok": True, "reason": "ok", "status": head["status"], "method": "HEAD"}
    get = http_fetch(url, method="GET", timeout=timeout, retry=True, host_registry=host_registry)
    if get["ok"]:
        return {"ok": True, "reason": "ok", "status": get["status"], "method": "GET"}
    return {"ok": False, "reason": get["reason"], "status": get["status"],
            "method": "GET", "bot_blocked": get.get("bot_blocked", False)}


def rejection_reasons(finding: dict, url_check: dict | None = None, sites: list[str] | None = None,
                      intent: dict | None = None, excluded_sites: list[str] | None = None) -> list[str]:
    if finding.get("parse_failed"):
        return ["parse_failed"]

    reasons = []
    if not finding.get("url"):
        reasons.append("missing_url")
    elif sites and not url_in_sites(finding.get("url"), sites):
        reasons.append("off_site")
    elif url_check and not url_check.get("ok"):
        reasons.append(url_check.get("reason") or "url_unverified")

    # User-blocked domain (independent of the URL-liveness chain, so an off_site item on a blocked
    # host is still tagged blocked). url_in_sites does exact + subdomain matching. The `not in` guard
    # avoids a duplicate when check_one already short-circuited with reason "excluded_site".
    if (finding.get("url") and excluded_sites and url_in_sites(finding.get("url"), excluded_sites)
            and "excluded_site" not in reasons):
        reasons.append("excluded_site")

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
# content_mismatch is final (the live page sells something else). "excluded_by_keyword" is
# DELIBERATELY absent: an exclude-keyword hit is the fragile gate that can misfire, so it stays
# rescuable and lands in "Unverified — check manually" instead of vanishing.
NON_RESCUABLE_REASONS = {
    "parse_failed", "off_site", "out_of_stock", "adjudicated_reject", "listing_inactive",
    "off_intent", "wrong_tier", "not_below_official", "content_mismatch", "free_excluded",
    "excluded_site",
    # A web-capable model opened the page and said it is not live / not the product — a stronger
    # signal than our plain-HTTP failure, so the item is final (and never re-enters model-verify).
    "model_check_failed",
}
SEARCH_LEGS = ("codex", "gemini")

# Network-verification-class rejections: "we could not reach/read the page", NOT "the page is
# wrong". Our plain-urllib client is bot-walled (403/444) on many marketplaces a web-capable model
# can still open, so these are the ONLY rejections eligible for model-assisted re-verification (Q1);
# every semantic reason (off_intent, wrong_tier, content_mismatch, not_below_official, out_of_stock,
# off_site, excluded_by_keyword, parse_failed, adjudicated_reject, listing_inactive, free_excluded,
# excluded_site) means the item was disproven and disqualifies it.
_HTTP_STATUS_REASON_RE = re.compile(r"^http_\d{3}$")


def is_network_verify_reason(reason: object) -> bool:
    reason = str(reason or "")
    return reason in {"bot_blocked", "timeout", "url_unverified"} or bool(_HTTP_STATUS_REASON_RE.match(reason))


def model_verify_eligible(item: dict) -> bool:
    """True for a rejected item that was NEVER semantically rejected — every rejection reason is a
    network-verification-class failure (see is_network_verify_reason) — and that carries a URL and a
    model-claimed price to confirm/rank. `missing_price` is tolerated ONLY alongside a genuine
    network reason (a price we could not read on an unreachable page is a network artifact, not a
    semantic reject). Such an item may be exactly what the user wants; a model with its own browser
    can often open what bot-walled us, so it earns one model-assisted look (Q1)."""
    reasons = item.get("reasons") or []
    if not reasons or not item.get("url"):
        return False
    if item.get("price_usd") is None and item.get("price") is None:
        return False
    saw_network = False
    for reason in reasons:
        if is_network_verify_reason(reason):
            saw_network = True
        elif reason != "missing_price":
            return False
    return saw_network


def is_rescuable(item: dict) -> bool:
    reasons = item.get("reasons") or []
    return bool(reasons) and not any(reason in NON_RESCUABLE_REASONS for reason in reasons)


def sort_by_price(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda x: (x.get("price") is None, x.get("price") or 10**18, str(x.get("title") or "")))


def finding_dedupe_key(item: dict) -> str:
    """Stable identity for the live feed: prefer the URL, then the call record id (for url-less
    parse failures), then title+price+currency. MUST match the frontend liveKey() so the backend
    de-dupe and the UI de-dupe agree across rescue/frontier rounds."""
    return item.get("url") or item.get("record_id") or f"{item.get('title')}|{item.get('price')}|{item.get('currency')}"


def finding_settled_payload(item: dict, verdict: str, stage: str | None) -> dict:
    """Compact, card-renderable snapshot of one finding for the live SSE feed. Kept lean (scalars +
    two tiny checks) so streaming these never bloats events.jsonl. url_check is the raw URL
    reachability; live_check is the live-page result — kept separate so the UI badge matches the
    final card (which prefers live_check over url_check) instead of conflating the two."""
    uc = item.get("url_check") or {}
    lc = item.get("live_check") or {}
    return {
        "stage": stage,
        "verdict": verdict,
        "record_id": item.get("record_id"),
        "url": item.get("url"),
        "title": item.get("title"),
        "price": item.get("price"),
        "currency": item.get("currency"),
        "price_usd": item.get("price_usd"),
        "price_usd_monthly": item.get("price_usd_monthly"),
        "price_basis": item.get("price_basis"),
        "basis_flag": item.get("basis_flag"),
        "basis_note": item.get("basis_note"),
        "marketplace": item.get("marketplace"),
        "availability": item.get("availability"),
        "disputed": bool(item.get("disputed")),
        "parse_failed": bool(item.get("parse_failed")),
        "reasons": item.get("reasons") or [],
        "source_models": item.get("source_models") or ([item["source_model"]] if item.get("source_model") else []),
        "url_check": {"ok": bool(uc.get("ok")), "reason": uc.get("reason")} if uc else None,
        "live_check": {"ok": bool(lc.get("ok")), "reason": lc.get("reason")} if lc else None,
    }


def verify_findings(
    findings: list[dict],
    parse_rejections: list[dict] | None = None,
    sites: list[str] | None = None,
    intent: dict | None = None,
    run_dir: Path | None = None,
    stage: str | None = None,
    emitted: dict | None = None,
    excluded_sites: list[str] | None = None,
    host_registry: HostBlockRegistry | None = None,
    cache: UrlCheckCache | None = None,
    model_verdicts: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    # `model_verdicts` (Q1) is the run-scoped {dedupe_key -> verdict} store the model-verify stage
    # fills; consulted per finding below so a promotion (or a model rejection) is DURABLE across every
    # later re-verify (coverage/frontier rebuild verified/rejected from the full findings list).
    # Fresh per call if the caller didn't share one; execute_research shares it across all rounds
    # so a host that bot-walled in the primary stays throttled through rescue/frontier too.
    host_registry = host_registry or HostBlockRegistry()
    unique = dedupe_findings(findings)
    verified: list[dict] = []
    rejected: list[dict] = list(parse_rejections or [])

    def emit_settled(item: dict, verdict: str) -> None:
        # Each round re-verifies the ACCUMULATED findings, so without a guard the same item would
        # re-stream every round. `emitted` (shared across a run's rounds) makes us emit only on
        # first sight or a real change — bounding events.jsonl and the SSE replay.
        if run_dir is None:
            return
        check = item.get("live_check") or item.get("url_check") or {}
        sig = (verdict, item.get("price_usd"), item.get("price"), bool(check.get("ok")),
               len(item.get("source_models") or ([item["source_model"]] if item.get("source_model") else [])))
        if emitted is not None:
            if emitted.get(finding_dedupe_key(item)) == sig:
                return
            emitted[finding_dedupe_key(item)] = sig
        emit_event(run_dir, "finding_settled", **finding_settled_payload(item, verdict, stage))

    # Parse failures land in verification.json, so the live feed must carry them too or the live
    # rejected panel/count would disagree with the poll fallback (rows flickering in and out).
    for pr in (parse_rejections or []):
        emit_settled(pr, "rejected")

    def check_one(finding: dict) -> tuple[dict, dict]:
        item = dict(finding)
        # A user-blocked domain is rejected regardless of liveness — skip the network round-trip.
        if excluded_sites and item.get("url") and url_in_sites(item.get("url"), excluded_sites):
            return item, {"ok": False, "reason": "excluded_site"}
        verdict = model_verdicts.get(dedupe_key(finding)) if model_verdicts else None
        if verdict is not None:
            # A web-capable model already opened this page (our plain HTTP is bot-walled here); trust
            # its liveness verdict and skip the network round-trip. The semantic gates below
            # (rejection_reasons -> intent/tier/content) still run on the model-updated fields.
            mv_check = apply_model_verdict(item, verdict, intent)
            if mv_check is not None:
                return item, mv_check
            # None = the model could not OPEN the page either; fall through to the HTTP path so the
            # item keeps its original (rescuable) network rejection instead of a false final verdict.
        url_check = verify_url(item.get("url"), host_registry=host_registry, cache=cache)
        if url_check.get("ok"):
            apply_live_check(item, intent, host_registry=host_registry, cache=cache)
        return item, url_check

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_VERIFY_WORKERS, max(1, len(unique)))) as executor:
        futures = [executor.submit(check_one, finding) for finding in unique]
        for future in concurrent.futures.as_completed(futures):
            item, url_check = future.result()
            item["url_check"] = url_check
            reasons = rejection_reasons(item, url_check, sites, intent, excluded_sites)
            if reasons:
                item["reasons"] = reasons
                rejected.append(item)
                verdict = "rejected"
            else:
                verified.append(item)
                verdict = "verified"
            # Stream each result the moment it settles so the UI fills in live, tagged by the stage.
            emit_settled(item, verdict)

    return sort_by_usd(verified), sort_by_usd(rejected)


def record_stage_results(run_dir: Path, stage: str, verified: list[dict], rejected: list[dict]) -> None:
    """After a phase: persist the cumulative verified/rejected (so the 2s poll fallback and a
    mid-run reload stay progressive) and emit a stage_summary the UI uses for the per-stage yield
    view ("rescue 1 took us from 5 → 9 verified"). Counts match the persisted lists (and the live
    finding_settled stream, which now carries parse failures too) so the live view and the poll
    fallback never disagree."""
    write_json(run_dir / "verification.json", {"stage": stage, "verified": verified, "rejected": rejected})
    emit_event(run_dir, "stage_summary", stage=stage, verified_total=len(verified), rejected_total=len(rejected))


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
    excluded = config.get("excluded_sites") or []
    if excluded:
        site_rule += (f"\nNEVER plan tasks that target these user-BLOCKED domains and never list them "
                      f"in preferred_sites: {', '.join(excluded)}.")
    n = config["task_count"]
    structured = bool(config.get("structured_decompose"))
    n_angle = int(config.get("angle_variants_per_task") or 0)

    # §1.2 Least-to-most: when on, tasks form a (source_class x angle) coverage grid so different
    # source classes are each owned by their own task; otherwise free-form tasks (legacy behaviour).
    if structured:
        task_rule = (
            f"Produce a COVERAGE GRID, not free-form tasks: up to {n} independent web-search tasks so that\n"
            "DIFFERENT source-CLASSES are each covered by their own task. For EACH task set:\n"
            "- \"source_class\": one of official_store | big_marketplace | classifieds | refurb_used |\n"
            "  forums_telegram | regional — pick the classes this subject plausibly has, one task each.\n"
            "- \"angle\": one line naming the angle (e.g. \"exact SKU on official store\", \"used/refurb units\").\n"
            "Do NOT give two tasks the same (source_class, angle)."
        )
    else:
        task_rule = (
            f"Create up to {n} independent web-search tasks for finding current purchasable offers.\n"
            "Each task must be useful if run independently by another model; cover different venues."
        )

    # §3.1 Angle variants (gated): a SEPARATE field from surface query_variants.
    angle_rule = (
        f"\nALSO give {n_angle} \"angle_variants\" per task: queries approaching the SAME subject from\n"
        "ORTHOGONAL angles — equivalent/compatible model, bundle/lot wording, regional/slang names,\n"
        "use-case framing. These are NOT surface synonyms; ground them in how this product CLASS is\n"
        "actually sold. ALWAYS keep query_variants[0] as the exact literal-SKU match."
        if n_angle > 0 else ""
    )

    grid_schema = ',\n      "source_class": "big_marketplace", "angle": "what angle this task takes"' if structured else ""
    angle_schema = ',\n      "angle_variants": ["orthogonal-angle query 1"]' if n_angle > 0 else ""

    return f"""You are the thin planning brain for a multi-model offer research system.
The subject can be ANYTHING purchasable or rentable: a product, a service, a subscription plan,
an account, real estate, a vehicle.
Return ONLY valid JSON. No Markdown.

User request:
{user_prompt}

Work in three ordered steps (Plan-and-Solve — do not jump straight to the tasks):
1. EXTRACT the decision variables and constraints from the request: subject, region/currency,
   pricing basis, required tier, budget or price to beat.
2. DEVISE the full coverage plan — which source-classes and angles JOINTLY answer the request with
   no overlap between them.
3. Only THEN emit the tasks below.

FIRST, before emitting tasks, silently self-ask and RESOLVE (do NOT ask the user — proceed with the
most likely reading; this only enriches the plan):
- The canonical product/model/edition this maps to, plus its real-world ALIASES, transliterations
  (latin↔cyrillic, e.g. "макбук"), SKU/model codes (e.g. A2681), and common misspellings.
- Which tiers / variants / pack sizes exist, and which one the user means.
- The implied region / currency / language.
- Whether the request is genuinely ambiguous: if two readings would MATERIALLY change
  subject_keywords, set intent.ambiguous=true and list the competing readings in intent.alternatives
  as 2-4 short candidate readings. When ambiguous, ALSO emit intent.clarify_question: ONE compact
  question, phrased in the SAME language as the user request, that would resolve the ambiguity. The
  FIRST entry in intent.alternatives MUST be the assumed/default reading the system proceeds with if
  the question goes unanswered — so order alternatives with your best guess first. Keep alternatives
  short enough to serve as one-tap answer buttons. When NOT ambiguous, leave clarify_question null.
- The PRICING BASIS the user shops on: paid once (one_time), a recurring subscription (monthly /
  yearly), metered by usage (per token / per request / per GB), per seat, or a rental. If the user
  says "cheapest subscription / plan / paid tier", they want a PAID recurring offering — a FREE tier
  is NOT a valid answer. If they gave a reference price, note ITS basis too (e.g. an official
  per-token API price is a different basis than a monthly subscription and must not be compared raw).
Use the resolved aliases when writing queries and subject_keywords.

THEN classify the request into intent.complexity = single_sku | comparison | broad_category |
multi_constraint. For a trivial single_sku lookup you MAY return as few as 2 tasks; for broad or
multi_constraint use the full {n}. NEVER exceed {n} tasks.

{task_rule}
Tasks must be MUTUALLY EXCLUSIVE: each task's query targets offers the SIBLING tasks do NOT cover, so
the same listing is never chased twice.
{site_rule}
Write plain queries WITHOUT search operators like site: — put domains in preferred_sites instead.
For each task ALSO give 2-3 "query_variants": alternate SURFACE phrasings of the SAME query that
beat search engines' phrase-adjacency (a query "macbook air m2" misses a listing titled "Apple
MacBook Air 2022 M2"). Vary: word order, synonyms, transliteration, model/SKU codes, year/spec
reorderings. They search the SAME thing, not a new angle.{angle_rule}

ALSO extract an "intent" object that the verifier and judge will enforce:
- subject_keywords: words/phrases that a RELEVANT result's title MUST contain (the actual thing
  wanted, e.g. ["macbook air m2"] or ["claude", "max"]). Used to reject wrong products.
- exclude_keywords: SPECIFIC disqualifying product/category terms that identify the WRONG thing — a
  different product line, "for parts", "case only", a competing brand the user did not
  ask for. NEVER add generic words that co-occur with valid offers (matching is whole-word, so one
  bad term silently rejects the correct answers): do NOT list "prompt", "free", "trial", "api",
  "subscription", "cheap", or the subject's own domain words. Good: ["for parts", "refurbished"];
  bad: ["prompt", "free", "api"].
- required_tier: if the user demanded a minimum tier/variant (e.g. "Max 5x or higher"), name it
  in lowercase ("max_5x"); else null. Below-tier offers are rejected.
- official_price / official_currency: the official/reference price the user wants to BEAT (they
  said "cheaper than $100" → 100, "USD"); offers at-or-above this are rejected. null if none.
- cheaper_than_official: true if the user explicitly wants STRICTLY BELOW the official price.
- price_basis: the pricing dimension of the request — one of one_time | subscription_monthly |
  subscription_yearly | usage_metered | per_seat_monthly | rental_monthly | unknown. Use unknown only
  if truly indeterminate.
- official_price_basis: if official_price is on a DIFFERENT basis than price_basis (e.g. the
  reference is per-token but the user shops per month), name that basis here; else repeat price_basis.
- free_ok: true if a $0 / free option is acceptable; FALSE when the user explicitly wants something
  PAID ("cheapest subscription", "cheapest paid plan") — then a free offer must never be the answer.
- monthly_usage_units: if the request implies a usage volume that lets a per-token/metered price be
  converted to a monthly cost (e.g. "~1M tokens/month"), put that NUMBER here; else null.
- usage_unit: what monthly_usage_units counts ("tokens", "requests", "gb"); else null.

Schema:
{{
  "tasks": [
    {{"id": "task-1", "query": "specific search query", "focus": "what to verify",
      "query_variants": ["alt phrasing 1", "alt phrasing 2"]{angle_schema}{grid_schema},
      "preferred_sites": ["olx.ua", "prom.ua"]}}
  ],
  "intent": {{
    "subject_keywords": ["..."], "exclude_keywords": ["..."],
    "required_tier": null, "official_price": null, "official_currency": null,
    "cheaper_than_official": false, "price_basis": "unknown", "official_price_basis": "unknown",
    "free_ok": true, "monthly_usage_units": null, "usage_unit": null,
    "complexity": "single_sku", "ambiguous": false, "alternatives": [], "clarify_question": null
  }}
}}
"""


def build_plan_audit_prompt(user_prompt: str, tasks: list[dict], intent: dict, config: dict) -> str:
    """Adversarial reviewer of the DECOMPOSITION itself (no search results yet). It sees only the
    request, the planned tasks and the extracted intent, and proposes at most 2 genuinely-additive
    tasks that close a coverage gap — never rephrasings of what is already planned."""
    plan_view = [
        {k: t[k] for k in ("id", "query", "focus", "source_class", "angle", "preferred_sites") if k in t}
        for t in tasks
    ]
    sites = config.get("sites") or []
    site_rule = (
        f"HARD CONSTRAINT: research is restricted to {', '.join(sites)} — any extra_tasks must target "
        f"ONLY those domains in preferred_sites."
        if sites
        else "Extra tasks should name the venues they target in preferred_sites."
    )
    return f"""You audit the RESEARCH PLAN of a multi-model offer-research system BEFORE any searching.
You see ONLY the user's request, the planned tasks and the extracted intent — NO search results yet.
Return ONLY valid JSON. No Markdown.

User request:
{user_prompt}

Extracted intent:
{json.dumps(intent, ensure_ascii=False, indent=2, sort_keys=True)}

Planned tasks:
{json.dumps(plan_view, ensure_ascii=False, indent=2)}

Judge the plan on three axes:
(a) COVERAGE — do these tasks TOGETHER answer the user's question, or is a material part unaddressed?
(b) OVERLAP — do two tasks chase the same listings / duplicate each other's venues?
(c) MISSING ANGLES — is a whole channel absent (official store / big marketplace / classifieds /
    refurb-used / regional-local), or a needed query FORMULATION missing (local-language phrasing,
    exact model/SKU code, transliteration), or a venue CATEGORY typical for this domain left out?

If and only if a REAL gap exists, propose up to 2 extra tasks that CLOSE it. Each extra task MUST be
genuinely additive — a NEW source channel, angle, or formulation — never a reworded existing task.
If the plan is already complete, return verdict "ok" and an empty extra_tasks list. {site_rule}

Schema:
{{
  "verdict": "ok" | "gaps",
  "extra_tasks": [
    {{"id": "audit-1", "query": "specific search query", "focus": "what this task adds",
      "query_variants": ["one alternate surface phrasing"],
      "source_class": "classifieds", "angle": "why this closes a gap",
      "preferred_sites": []}}
  ],
  "notes": "one short sentence on the gap (or why the plan is complete)"
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


def normalize_task(raw: object, idx: object, run_sites: list[str]) -> dict | None:
    """Canonicalize ONE raw task dict into the task schema. Shared by decompose coercion and the
    plan auditor so both apply the same site-scoping / variant-dedup / structured-field rules.
    Returns None when the task has no usable query."""
    if not isinstance(raw, dict):
        return None
    query = str(raw.get("query") or "").strip()
    if not query:
        return None
    preferred = raw.get("preferred_sites") or []
    if not isinstance(preferred, list):
        preferred = [str(preferred)]
    preferred = [site for site in (normalize_site(s) for s in preferred) if site]
    if run_sites:
        preferred = [site for site in preferred if site in run_sites] or list(run_sites)

    def clean_variants(key: str) -> list[str]:
        raw_v = raw.get(key) or []
        if not isinstance(raw_v, list):
            raw_v = [str(raw_v)]
        out, seen = [], {query.lower()}
        for v in raw_v:
            v = str(v or "").strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                out.append(v)
        return out

    task = {
        "id": str(raw.get("id") or f"task-{idx}"),
        "query": query,
        "query_variants": clean_variants("query_variants")[:4],
        "focus": str(raw.get("focus") or "Find current purchasable offers with verified URLs."),
        "preferred_sites": preferred,
    }
    # Structured-decompose / angle-expansion fields (effort >=3). Preserved when the brain emits
    # them; harmless and ignored downstream when the effort profile keeps those features off.
    angle_variants = clean_variants("angle_variants")[:3]
    if angle_variants:
        task["angle_variants"] = angle_variants
    if raw.get("source_class"):
        task["source_class"] = str(raw.get("source_class")).strip().lower()
    if raw.get("angle"):
        task["angle"] = str(raw.get("angle")).strip()
    return task


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
        task = normalize_task(raw, idx, run_sites)
        if task is not None:
            tasks.append(task)

    # Floor of 2 (not 3): the complexity classifier may legitimately emit a 2-task plan for a
    # trivial single-SKU lookup; only fall back when the brain returned a degenerate result.
    return tasks if len(tasks) >= 2 else fallback_tasks(user_prompt, config)


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
        "ambiguous": False, "alternatives": [], "clarify_question": None, "complexity": None,
        "price_basis": "unknown", "official_price_basis": "unknown", "free_ok": True,
        "monthly_usage_units": None, "usage_unit": None, "official_price_monthly_usd": None,
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

    def displaylist(v):
        # Case-PRESERVING variant for user-facing text (alternatives are shown verbatim as the
        # clarify quick-answer buttons and the report's assumed-reading note).
        if isinstance(v, str):
            v = [v]
        return [str(x).strip() for x in (v or []) if str(x).strip()]

    intent["subject_keywords"] = strlist(raw.get("subject_keywords"))
    intent["exclude_keywords"] = strlist(raw.get("exclude_keywords"))
    intent["required_tier"] = canon_tier(raw.get("required_tier"))
    intent["cheaper_than_official"] = bool(raw.get("cheaper_than_official"))
    official = parse_price(raw.get("official_price"))
    intent["official_price_usd"] = to_usd(official, raw.get("official_currency") or "USD") if official else None
    # Pricing basis (the axis the user shops on / the reference price lives on) + free/usage knobs.
    intent["price_basis"] = canon_basis(raw.get("price_basis"))
    ob = canon_basis(raw.get("official_price_basis"))
    intent["official_price_basis"] = ob if ob != "unknown" else intent["price_basis"]
    intent["free_ok"] = raw.get("free_ok") is not False  # explicit false only; null/missing => True
    intent["monthly_usage_units"] = parse_count(raw.get("monthly_usage_units"))
    intent["usage_unit"] = str(raw.get("usage_unit") or "").strip().lower() or None
    intent["official_price_monthly_usd"] = (
        to_monthly_usd(intent["official_price_usd"], intent["official_price_basis"], intent)
        if intent["official_price_usd"] else None
    )
    intent["ambiguous"] = bool(raw.get("ambiguous"))
    intent["alternatives"] = displaylist(raw.get("alternatives"))[:4]
    intent["clarify_question"] = str(raw.get("clarify_question") or "").strip() or None
    complexity = str(raw.get("complexity") or "").strip().lower()
    intent["complexity"] = complexity if complexity in {"single_sku", "comparison", "broad_category", "multi_constraint"} else None
    return intent


def _flag_incomparable_basis(finding: dict, intent: dict) -> None:
    finding["basis_flag"] = "incomparable_basis"
    finding["basis_note"] = (
        f"offer is {canon_basis(finding.get('price_basis'))}, target price is "
        f"{canon_basis(intent.get('official_price_basis'))} — not directly comparable")


def keyword_hits(text: object, keywords: list[str] | None, prefix: bool = False) -> list[str]:
    """Keyword matcher returning which keywords occur in `text` at token boundaries. Unicode-aware
    (\\w spans non-ASCII scripts under re). Multi-word keywords match across any run of whitespace.
    Two modes, chosen by which failure direction is harmful:
    - prefix=False (whole-word): for EXCLUDE keywords, where a false hit kills a valid result —
      "free" must not fire on "freedom", "voice" not on "invoice". (A naive substring test here
      silently rejected valid LLM-API listings.)
    - prefix=True (word-start, suffix allowed): for SUBJECT keywords, where a false MISS kills a
      valid result — a base-form subject must still hit the inflected/declined variants that
      dominate RU/UA listing titles, "credit" must hit "credits". Only the trailing boundary is
      relaxed; the leading boundary still blocks "voice" from firing on "invoice"."""
    hay = str(text or "").lower()
    hits = []
    for raw in keywords or []:
        kw = str(raw or "").strip().lower()
        if not kw:
            continue
        pattern = r"\s+".join(re.escape(part) for part in kw.split())
        tail = "" if prefix else r"(?!\w)"
        if re.search(rf"(?<!\w){pattern}{tail}", hay, re.UNICODE):
            hits.append(kw)
    return hits


def intent_rejection(finding: dict, intent: dict | None) -> str | None:
    """Reject findings that don't match what the user actually asked for: wrong product
    (exclude keyword / no subject keyword), wrong tier, a free tier when a PAID one was asked for,
    or a price at/above the official reference ON THE SAME PRICING BASIS. Prices on a different,
    non-normalizable basis are TAGGED incomparable_basis and KEPT, never dropped. May mutate the
    finding (basis_flag / basis_note / price_usd_monthly) — it runs on a mutable per-item copy."""
    if not intent:
        return None
    text = " ".join(str(finding.get(f) or "") for f in ("title", "evidence", "marketplace")).lower()
    # An exclude-keyword hit is the fragile gate (a wrongly-generic keyword misfires here), so it is
    # recoverable — distinct reason, NOT final. A missing subject keyword is a genuine wrong-product
    # signal and stays final (off_intent).
    if intent.get("exclude_keywords") and keyword_hits(text, intent["exclude_keywords"]):
        return "excluded_by_keyword"
    subject = intent.get("subject_keywords") or []
    if subject and not keyword_hits(text, subject, prefix=True):
        return "off_intent"
    req_rank = tier_rank(intent.get("required_tier"))
    if req_rank is not None:
        ft = tier_rank(finding.get("tier"))
        # Reject only a KNOWN-lower tier. A finding with no reported tier is NOT rejected here
        # (failure-to-extract ≠ wrong tier; rescue/adjudication weigh it) — the synthesis prompt
        # is told the required tier so it can flag tier-unknown items rather than drop them.
        if ft is not None and ft < req_rank:
            return "wrong_tier"

    # Free exclusion: when the user wants a PAID offering, a free/$0 tier is not a valid "cheapest".
    # parse_price maps 0 -> None, so free is detected via the reported basis, not the price number.
    if not intent.get("free_ok", True) and canon_basis(finding.get("price_basis")) == "free":
        return "free_excluded"

    ceiling = intent.get("official_price_usd")
    if intent.get("cheaper_than_official") and ceiling and finding.get("price_usd") is not None:
        f_usd = finding["price_usd"]
        f_cls = basis_class(finding.get("price_basis"))
        ceil_cls = basis_class(intent.get("official_price_basis"))  # compare on the CEILING's axis
        # Unknown basis on either side: keep the legacy raw-USD comparison (back-compat).
        if f_cls == "unknown" or ceil_cls == "unknown":
            return "not_below_official" if f_usd >= ceiling else None
        # Same non-recurring class (one_time vs one_time, metered vs metered) compares raw USD;
        # every other case is only comparable on a common monthly axis (recurring, or cross-class).
        if f_cls == ceil_cls and f_cls != "recurring":
            fv, cv = f_usd, ceiling
        else:
            fv = to_monthly_usd(f_usd, finding.get("price_basis"), intent)
            cv = intent.get("official_price_monthly_usd") or to_monthly_usd(
                ceiling, intent.get("official_price_basis"), intent)
            if fv is not None:
                finding["price_usd_monthly"] = fv
        if fv is not None and cv is not None:
            return "not_below_official" if fv >= cv else None
        # Not reducible to one comparable number -> tag and KEEP (surfaced in the report).
        _flag_incomparable_basis(finding, intent)
        return None
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
    if intent.get("exclude_keywords") and keyword_hits(live_title, intent["exclude_keywords"]):
        return True
    subject = [kw for kw in (intent.get("subject_keywords") or []) if kw]
    if not subject:
        return False
    finding_title = str(finding.get("title") or "").lower()
    indexed_on_subject = any(kw in finding_title for kw in subject)
    # Prefix mode, same as the intent_rejection subject gate: the live title's inflected/plural
    # forms must still count as the subject being present.
    live_has_subject = bool(keyword_hits(live_title, subject, prefix=True))
    return indexed_on_subject and not live_has_subject


SITE_OPERATOR_RE = re.compile(r"\bsite:\S+", re.IGNORECASE)


def excluded_sites_rule(excluded: list[str] | None) -> str:
    """A negative-domain instruction for the search/plan prompts (empty when nothing is blocked, so
    the common case adds zero tokens). Enforcement is belt-and-suspenders — verification also rejects
    blocked hosts — but steering the models away avoids wasted, later-rejected findings."""
    return (
        f"- HARD CONSTRAINT: NEVER return URLs from these user-BLOCKED domains "
        f"(they are discarded by automated verification): {', '.join(excluded)}.\n"
        if excluded else ""
    )


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


# Source-CLASS taxonomy (research-grounded anti-herding): legs are leaned toward complementary
# classes so they stop returning the same popular sites. Hints are examples, not hard filters.
SOURCE_CLASS_HINTS = {
    "big_marketplace": "large general marketplaces (e.g. Amazon, Rozetka, Prom)",
    "classifieds": "classifieds / peer-to-peer (e.g. OLX, Kufar, Facebook Marketplace)",
    "official_store": "official brand stores, authorized resellers, provider plan pages",
    "refurb_used": "refurbished / used / open-box sellers",
    "regional": "regional or local-language sites for the user's region",
    "forums_telegram": "niche forums, communities, Telegram resale channels",
}


def build_search_prompt(user_prompt: str, task: dict, leg: str, config: dict, leg_focus: str | None = None) -> str:
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
    site_rule += excluded_sites_rule(config.get("excluded_sites"))
    # Per-leg source-class lean (anti-herding) — SOFT, with an escape so the cheapest mainstream
    # offer is never suppressed. Suppressed entirely when the run is pinned to specific sites.
    focus_rule = ""
    if leg_focus and not run_sites:
        focus_rule = (
            f"- SOURCE-CLASS LEAN: prioritize {SOURCE_CLASS_HINTS.get(leg_focus, leg_focus)} this round. "
            f"Other models cover other source classes, so look beyond the obvious top-2 marketplaces — "
            f"but DO still return a clearly better/cheaper offer from ANY source if you find one.\n"
        )
    if task.get("_angle"):
        focus_rule += (
            "- ORTHOGONAL ANGLE: this query approaches the subject from a different framing; explore "
            "that angle, but every result must still be the SAME thing the user actually wants.\n"
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
{site_rule}{focus_rule}
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
- Report the PRICE BASIS in "price_basis": one_time | subscription_monthly | subscription_yearly |
  usage_metered | per_seat_monthly | rental_monthly | free. A $0 / "Free" offer MUST be reported with
  price_basis="free" (leave price null) — do not silently omit it. Do not compare a monthly price to
  a yearly or per-token one; just report each offer's OWN basis and its native price.
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
      "price_basis": "one_time | subscription_monthly | subscription_yearly | usage_metered | per_seat_monthly | rental_monthly | free",
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


def do_not_report_block(known_urls: list[str] | None, cap: int = 20) -> str:
    """A compact 'already have these, find NEW ones' list for the rescue/frontier prompts so a
    round spends its budget on novel offers instead of re-surfacing verified URLs. Deduped + capped
    to keep the prompt bounded; empty string when nothing is known yet."""
    seen: set[str] = set()
    urls: list[str] = []
    for url in known_urls or []:
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
        if len(urls) >= cap:
            break
    if not urls:
        return ""
    return ("\n- Do NOT re-report these already-known/verified URLs; find NEW offers (or genuinely "
            "cheaper/better ones):\n" + "\n".join(urls))


def known_urls_minus_item(known_urls: list[str] | None, item: dict) -> list[str]:
    """Rescue-prompt view of the do-not-report list, minus the item's OWN url(s). A rescue asks the
    model to RECOVER this exact listing, so a disputed item living inside `verified` would otherwise
    appear in its own 'do NOT re-report these URLs' block — a contradiction. Other verified URLs stay
    (they still steer the round toward novel offers). Comparison is normalized (tracking-param safe)."""
    own = {normalize_url_for_key(item.get("url"))}
    for cand in (item.get("price_candidates") or []):
        if isinstance(cand, dict):
            own.add(normalize_url_for_key(cand.get("url")))
    own.discard(None)
    return [u for u in (known_urls or []) if normalize_url_for_key(u) not in own]


def build_frontier_prompt(user_prompt: str, ceiling_usd: float, run_sites: list[str], intent: dict | None,
                          excluded: list[str] | None = None, known_urls: list[str] | None = None) -> str:
    """Targeted search for offers STRICTLY cheaper than the current best credible price — the
    frontier round pushes the price floor down or proves nothing cheaper-and-credible exists."""
    site_rule = (f"- HARD CONSTRAINT: only URLs on these domains: {', '.join(run_sites)}.\n" if run_sites else "")
    site_rule += excluded_sites_rule(excluded)
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
- Only offers on the SAME pricing basis as the target count as cheaper. Convert period
  (yearly→monthly) or usage before claiming an offer is cheaper; never present a one-time price as
  cheaper than a subscription. Report each offer's price_basis.
- Return direct listing/offer URLs with native price + currency code. Unknown fields null.
- If there is genuinely nothing credible below ${ceiling_usd:.2f}, return an empty findings array.{do_not_report_block(known_urls)}

Schema:
{{"findings": [{{"title": "...", "price": 0, "currency": "USD", "url": "https://...",
  "marketplace": "...", "availability": "...", "condition": "...", "tier": null, "price_basis": null,
  "seller": "...", "location": null, "shipping": null, "evidence": "...", "confidence": 0.0}}]}}
"""


def credible_floor_usd(verified: list[dict]) -> float | None:
    """Lowest USD price among non-disputed verified findings — the current frontier. Skips items
    tagged incomparable_basis (a different pricing axis must not set a bogus 'cheapest' floor)."""
    prices = [v.get("price_usd") for v in verified
              if v.get("price_usd") is not None and not v.get("disputed") and not v.get("basis_flag")]
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
    elif item.get("model_verified"):
        # A web-capable model opened the page our HTTP client was bot-walled on (Q1). Trustworthy,
        # but weaker than a machine-read live page — kept clearly above an unverified item (0.4).
        live_score = 0.7; factors.append("model-verified (page opened by a model)")
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


def run_frontier_round(prompt: str, ceiling_usd: float, run_dir: Path, config: dict, intent: dict | None, round_no: int,
                       known_urls: list[str] | None = None, timeout_cap: int | None = None,
                       host_registry: HostBlockRegistry | None = None,
                       url_cache: UrlCheckCache | None = None) -> list[dict]:
    """One frontier sweep: each search leg hunts strictly below the ceiling."""
    run_id = run_dir.name
    # Skip force-disabled (breaker/quota) and user-disabled legs at assembly time (M2): call_model
    # would only emit skip records for them. Frontier fires one job per leg, so the slow leg (codex)
    # already gets at most one call here — inherently within codex_task_cap, no extra cap needed.
    search_legs = [leg for leg in (config.get("search_legs") or ["codex", "gemini"])
                   if not leg_disabled(run_id, leg) and not user_disabled(run_id, leg)]
    if not search_legs:
        return []
    fp = build_frontier_prompt(prompt, ceiling_usd, config.get("sites") or [], intent, config.get("excluded_sites"), known_urls)
    timeout = min(RAW_TIMEOUT_SEC, config["search_timeout_sec"])
    if timeout_cap is not None:
        timeout = min(timeout, timeout_cap)
    on_record, prefetch_shutdown = make_prefetch_collector(config, host_registry, url_cache)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_PRIMARY_WORKERS, len(search_legs))) as executor:
            futures = [
                executor.submit(call_model, leg, fp, run_dir, "frontier", f"frontier-{round_no}",
                                timeout, config["search_effort"], config.get("claude_search_model") if leg == "claude" else None)
                for leg in search_legs
            ]
            fast_total = sum(1 for leg in search_legs if leg not in SLOW_LEGS)
            return collect_with_straggler_drop(futures, run_dir, config, on_record=on_record,
                                               fast_quorum_total=fast_total)
    finally:
        prefetch_shutdown()


def covered_source_classes(verified: list[dict], tasks: list[dict]) -> set[str]:
    """Which source-classes already produced a credible verified finding (via task_id -> task)."""
    by_id = {t["id"]: t.get("source_class") for t in tasks if t.get("source_class")}
    covered = set()
    for f in verified:
        sc = by_id.get(str(f.get("task_id") or "").split("#")[0])
        if sc:
            covered.add(sc)
    return covered


def build_coverage_prompt(user_prompt: str, source_class: str, avoid_hosts: list[str],
                          run_sites: list[str], intent: dict | None, excluded: list[str] | None = None) -> str:
    """Targeted search of ONE source-class that earlier rounds left empty — chases UNCOVERED space
    (distinct from rescue, which recovers rejected items, and frontier, which chases a lower price)."""
    subj = ", ".join((intent or {}).get("subject_keywords") or []) or "the requested item"
    site_rule = (f"- HARD CONSTRAINT: only URLs on these domains: {', '.join(run_sites)}.\n" if run_sites else "")
    site_rule += excluded_sites_rule(excluded)
    avoid_rule = (f"- These hosts are already saturated; look ELSEWHERE, do not just re-return them: "
                  f"{', '.join(avoid_hosts)}.\n" if avoid_hosts and not run_sites else "")
    return f"""You are a COVERAGE-GAP research worker. Earlier rounds found NOTHING credible from one
class of sources. Find CURRENTLY AVAILABLE, credible offers for the SAME thing ({subj}) SPECIFICALLY
from: {SOURCE_CLASS_HINTS.get(source_class, source_class)}.
Use live web results. Return ONLY valid JSON. No Markdown.

Original user request:
{user_prompt}

Rules:
{site_rule}{avoid_rule}- It must be the SAME thing the user wants (right product/tier), working and honestly described.
- Return direct listing/offer URLs with native price + currency code. Unknown fields null.
- If this source class genuinely has nothing credible, return an empty findings array (do NOT pad).

Schema:
{{"findings": [{{"title": "...", "price": 0, "currency": "USD", "url": "https://...",
  "marketplace": "...", "availability": "...", "condition": "...", "tier": null,
  "seller": "...", "location": null, "shipping": null, "evidence": "...", "confidence": 0.0}}]}}
"""


def build_gap_search_prompt(user_prompt: str, gap_query: str, avoid_hosts: list[str],
                            run_sites: list[str], intent: dict | None, excluded: list[str] | None = None) -> str:
    """Targeted search for ONE semantic gap the coverage auditor flagged — a specific angle/channel of
    the user's question the current results do not address. Distinct from build_coverage_prompt, which
    chases an empty source CLASS; this chases a MISSING ANGLE expressed as a concrete query."""
    subj = ", ".join((intent or {}).get("subject_keywords") or []) or "the requested item"
    site_rule = (f"- HARD CONSTRAINT: only URLs on these domains: {', '.join(run_sites)}.\n" if run_sites else "")
    site_rule += excluded_sites_rule(excluded)
    avoid_rule = (f"- These hosts are already saturated; look ELSEWHERE, do not just re-return them: "
                  f"{', '.join(avoid_hosts)}.\n" if avoid_hosts and not run_sites else "")
    return f"""You are a COVERAGE-GAP research worker. The current results MISS a specific angle of the
user's question. Find CURRENTLY AVAILABLE, credible offers for the SAME thing ({subj}) that
SPECIFICALLY address this angle: {gap_query}
Use live web results. Return ONLY valid JSON. No Markdown.

Original user request:
{user_prompt}

Rules:
{site_rule}{avoid_rule}- It must be the SAME thing the user wants (right product/tier), working and honestly described.
- Return direct listing/offer URLs with native price + currency code. Unknown fields null.
- If this angle genuinely has nothing credible, return an empty findings array (do NOT pad).

Schema:
{{"findings": [{{"title": "...", "price": 0, "currency": "USD", "url": "https://...",
  "marketplace": "...", "availability": "...", "condition": "...", "tier": null,
  "seller": "...", "location": null, "shipping": null, "evidence": "...", "confidence": 0.0}}]}}
"""


def run_coverage_round(prompt: str, missing_classes: list[str], avoid_hosts: list[str],
                       run_dir: Path, config: dict, intent: dict | None, timeout_cap: int | None = None,
                       gap_queries: list[dict] | None = None,
                       host_registry: HostBlockRegistry | None = None,
                       url_cache: UrlCheckCache | None = None) -> list[dict]:
    """One coverage sweep, round-robin across legs (negative-space exploration — cross-check still
    happens at verify, so no need for all legs). Fires two kinds of job IN THE SAME executor batch:
    one per empty source-CLASS, plus one per semantic GAP query the auditor flagged. No serial phase."""
    run_id = run_dir.name
    # Skip force-disabled (breaker/quota) and user-disabled legs at assembly time (M2).
    search_legs = [leg for leg in (config.get("search_legs") or ["codex", "gemini"])
                   if not leg_disabled(run_id, leg) and not user_disabled(run_id, leg)]
    if not search_legs:
        return []
    run_sites = config.get("sites") or []
    timeout = min(RAW_TIMEOUT_SEC, config["search_timeout_sec"])
    if timeout_cap is not None:
        timeout = min(timeout, timeout_cap)
    excluded = config.get("excluded_sites")
    # Kind-tagged jobs so both class and gap searches share one round-robin over legs and one batch.
    jobs: list[tuple[str, object, str]] = []
    for sc in missing_classes:
        jobs.append(("class", sc, build_coverage_prompt(prompt, sc, avoid_hosts, run_sites, intent, excluded)))
    for gq in (gap_queries or []):
        query = str((gq or {}).get("query") or "").strip()
        if query:
            jobs.append(("gap", query, build_gap_search_prompt(prompt, query, avoid_hosts, run_sites, intent, excluded)))
    if not jobs:
        return []
    # Round-robin across legs, but cap the slow leg (codex) at codex_task_cap jobs per round (P1) —
    # its coverage calls get straggler-killed most of the time, so beyond the cap its round-robin
    # turns are handed to fast legs instead of stretching the round by the grace window for nothing.
    fast_legs = [leg for leg in search_legs if leg not in SLOW_LEGS]
    codex_cap = int(config.get("codex_task_cap") or 0)
    assigned: list[tuple[str, tuple]] = []  # (leg, job) — jobs past the slow cap with no fast leg are DROPPED
    slow_used = 0
    fast_rr = 0
    for i, job in enumerate(jobs):
        leg = search_legs[i % len(search_legs)]
        if leg in SLOW_LEGS:
            if slow_used < codex_cap:
                slow_used += 1
            elif fast_legs:
                leg = fast_legs[fast_rr % len(fast_legs)]
                fast_rr += 1
            else:
                # No fast leg to hand the turn to (e.g. only codex survives) — dropping the job is the
                # cap working as intended; keeping it on codex would rebuild the unbounded slow fan-out.
                continue
        assigned.append((leg, job))
    if not assigned:
        return []
    jobs = [job for _, job in assigned]
    job_legs = [leg for leg, _ in assigned]
    on_record, prefetch_shutdown = make_prefetch_collector(config, host_registry, url_cache)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_PRIMARY_WORKERS, len(jobs))) as executor:
            futures = [
                executor.submit(call_model, job_legs[i], built_prompt,
                                run_dir, "coverage", f"{kind}-{key}",
                                timeout, config["search_effort"],
                                config.get("claude_search_model") if job_legs[i] == "claude" else None)
                for i, (kind, key, built_prompt) in enumerate(jobs)
            ]
            fast_total = sum(1 for leg in job_legs if leg not in SLOW_LEGS)
            return collect_with_straggler_drop(futures, run_dir, config, on_record=on_record,
                                               fast_quorum_total=fast_total)
    finally:
        prefetch_shutdown()


def build_recheck_prompt(user_prompt: str, rejected_item: dict, config: dict,
                         known_urls: list[str] | None = None) -> str:
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
    excluded = config.get("excluded_sites") or []
    if excluded:
        site_rule += f"\n- HARD CONSTRAINT: do NOT recover the item on these user-BLOCKED domains: {', '.join(excluded)}."
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
- Return an empty findings array ONLY if you are confident no current purchasable offer exists for it.{site_rule}{do_not_report_block(known_urls)}

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


def build_model_verify_prompt(user_prompt: str, item: dict, config: dict) -> str:
    """Ask a WEB-CAPABLE model to open a candidate our plain-HTTP verifier could not reach (anti-bot
    wall / timeout / http error) and confirm it first-hand. STRICT JSON out; NO invented data — if it
    cannot open the page it returns live=false. Same house style as build_recheck_prompt."""
    compact = json.dumps(
        {
            "title": item.get("title"),
            "price": item.get("price"),
            "currency": item.get("currency"),
            "url": item.get("url"),
            "marketplace": item.get("marketplace"),
            "seller": item.get("seller"),
            "reasons": item.get("reasons"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"""You are a live-listing verifier for an offer research system.
Our automated HTTP client could NOT reach this page (it is behind an anti-bot wall or errored), so we
need you to open it with your OWN web tooling and report what is actually there.
Return ONLY valid JSON. No Markdown.

Original user request:
{user_prompt}

Candidate to verify (open the url yourself):
{compact}

Your job:
- OPEN the url. Confirm the page LOADS and is a real, currently-available offer for the SAME product
  the user wants (right item/tier, honestly described, in stock).
- Read the CURRENT price and currency straight off the page (not from the data above).
- If the url redirected, report the FINAL url.
- "opened" MUST be honest: true ONLY if you actually loaded and read the page content. If your own
  web tooling was also blocked / errored, return "opened": false, "live": false — do NOT guess.
- NO invented data: if the page is dead / sold / a different product, return "opened": true,
  "live": false and explain why in notes.

Return exactly this JSON shape:
{{
  "opened": true,
  "live": true,
  "price": 12345,
  "currency": "USD",
  "availability": "available / sold / unknown",
  "seller": "seller or null",
  "title": "the page's product title",
  "url": "final url if redirected, else the same url",
  "notes": "one short sentence of evidence"
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


# Maximal Marginal Relevance (Carbonell & Goldstein 1998): order the shortlist by
# λ·relevance − (1−λ)·max_similarity_to_already_picked, so near-duplicates (same host/seller/
# region/condition/price-band) stop crowding out credible alternatives. λ high → price/trust stay
# dominant; it only re-orders/demotes near-dups, never promotes an irrelevant cheaper item.
# Exact listing-ID dupes are already removed upstream by dedupe_findings; MMR adds same-host/near-dup
# spreading on top of that. Deterministic, stdlib-only.
MMR_LAMBDA = env_float("RESEARCH_MMR_LAMBDA", 0.7)
_MMR_WEIGHTS = {"host": 0.6, "seller": 0.2, "loc": 0.1, "cond": 0.05, "band": 0.05}


def _mmr_features(item: dict) -> dict:
    price = item.get("price_usd")
    return {
        "host": host_of(item.get("url")) or None,
        "seller": (str(item.get("seller") or "").strip().lower()[:40] or None),
        "loc": (str(item.get("location") or "").strip().lower() or None),
        "cond": (str(item.get("condition") or "").strip().lower() or None),
        "band": None if price is None else round(math.log10(price + 1) * 3),  # coarse log price band
    }


def _mmr_sim(fa: dict, fb: dict) -> float:
    return sum(w for k, w in _MMR_WEIGHTS.items() if fa.get(k) is not None and fa.get(k) == fb.get(k))


def mmr_order(items: list[dict], lam: float = MMR_LAMBDA) -> list[dict]:
    """items must already be in best-first relevance order (e.g. trust_rank_key sorted)."""
    n = len(items)
    if n <= 2:
        return list(items)
    feats = [_mmr_features(it) for it in items]
    rel = [1.0 - i / n for i in range(n)]  # rank position is the relevance proxy
    selected: list[int] = []
    remaining = list(range(n))
    while remaining:
        best_i, best_score = remaining[0], -1e18
        for i in remaining:
            sim = max((_mmr_sim(feats[i], feats[j]) for j in selected), default=0.0)
            score = lam * rel[i] - (1.0 - lam) * sim
            if score > best_score:
                best_score, best_i = score, i
        selected.append(best_i)
        remaining.remove(best_i)
    return [items[i] for i in selected]


def build_synthesis_prompt(
    user_prompt: str,
    tasks: list[dict],
    verified: list[dict],
    rejected: list[dict],
    config: dict,
    degraded_legs: list[str] | None = None,
    intent: dict | None = None,
    skipped_stages: list[str] | None = None,
) -> str:
    unconfirmed = [item for item in rejected if is_rescuable(item)]
    dead = [item for item in rejected if not is_rescuable(item)]
    ordered = mmr_order(sorted(verified, key=trust_rank_key))
    requested_sites = config.get("sites") or [s for t in tasks for s in (t.get("preferred_sites") or [])]
    found_hosts = host_distribution(verified)
    zero_result_sites = sorted(set(requested_sites) - set(found_hosts))
    context = {
        "user_prompt": user_prompt,
        "intent": intent or None,
        "restricted_to_sites": config.get("sites") or None,
        "degraded_legs": degraded_legs or None,
        "verification_stages_skipped_deadline": skipped_stages or None,
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
- PRICING BASIS: intent.price_basis says whether the user shops one_time / subscription
  (monthly|yearly) / usage-metered / per-seat. Compare like with like — each finding carries
  price_basis and, when computable, price_usd_monthly. NEVER call a smaller number the winner if it
  is on a DIFFERENT basis (e.g. a one-time purchase vs a monthly subscription, or a per-token rate
  vs a monthly plan); say plainly they are not directly comparable.
- Findings tagged basis_flag="incomparable_basis" could not be normalized to the user's basis (see
  basis_note): present them in a separate note explaining WHY; do not rank them against comparable
  offers or call them the cheapest.
- If intent.free_ok is false, a free / $0 offer is NOT a valid "cheapest" answer — exclude it from
  the recommendation and mention it only as context.
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
- If verification_stages_skipped_deadline is non-empty, some verification stages were skipped to stay
  within the run's time budget — add ONE short degradation note (not the lead, not a warning callout)
  saying which stages were skipped and that the results are less exhaustively cross-checked.
- If intent.ambiguous is true (or intent.alternatives is non-empty), the run proceeded on ONE assumed
  reading of an ambiguous request. State that assumption explicitly near the TOP of the report — which
  reading you answered (intent.assumed, when set, is the exact default reading the system used) and
  what the alternative readings were — so the user can correct it.
- Each finding carries confidence_calibrated {{score, band: high/medium/low, factors}} combining
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
    if leg == "gemini":
        gemini_label = run_gemini_model(run_id)
        if gemini_label:
            env["AGY_MODEL"] = gemini_label
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
        # Audit calls overlap a straggler-dropping phase; protect them from that phase's quorum kill.
        register_proc(run_id, record_id, proc.pid, protected=task_type in ("plan_audit", "gap_audit"))
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
    breaker_tripped = apply_call_to_breaker(run_id, leg, meta["success"], meta["dropped_as_straggler"])
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
                "excluded_sites": config.get("excluded_sites") or [],
                "vendor_tiers": config.get("vendor_tiers") or {},
                "interactive": bool(config.get("interactive")),
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


def audit_plan(prompt: str, tasks: list[dict], intent: dict, run_dir: Path, config: dict) -> list[dict]:
    """Concurrent plan reviewer. Advisory only: on ANY failure returns [] with no fallback tasks.
    Runs on the judge vendor (codex-first — codex has no concurrency cap, so it never steals a
    search slot). Returns accepted extra tasks (<=2, 1 variant each, tagged origin=plan_audit).
    Emits plan_audit_finished + records run.json.plan_audit so the audit is observable even when
    the primary search proceeds without waiting for it."""
    brain = judge_vendor(config)
    added: list[dict] = []
    verdict, notes = "error", ""
    try:
        record = call_model(
            brain,
            build_plan_audit_prompt(prompt, tasks, intent, config),
            run_dir,
            "plan_audit",
            "audit",
            timeout=180,
            effort="medium",
            claude_model=vendor_claude_model(brain, config),
        )
        if record.get("success"):
            payload = extract_json(record.get("stdout") or "")
            if isinstance(payload, dict):
                verdict = str(payload.get("verdict") or "").strip().lower() or "gaps"
                notes = str(payload.get("notes") or "").strip()
                added = coerce_audit_tasks(payload.get("extra_tasks"), tasks, config)
    except (ValueError, KeyError, TypeError):
        added = []  # advisory: never let a malformed audit fault the run
    emit_event(run_dir, "plan_audit_finished", verdict=verdict, added=len(added), notes=notes[:200])
    update_run(run_dir, plan_audit={"verdict": verdict, "added": len(added)})
    return added


def coerce_audit_tasks(raw_extra: object, tasks: list[dict], config: dict) -> list[dict]:
    """Validate the auditor's extra tasks through the SAME normalization as decompose, then keep only
    surgical, non-duplicate additions: drop any whose query matches an existing task's query or a
    query_variant (case-insensitive), force distinct audit ids, cap 1 variant each, cap 2 total."""
    if not isinstance(raw_extra, list):
        return []
    run_sites = config.get("sites") or []
    existing = set()
    for t in tasks:
        existing.add(str(t.get("query") or "").strip().lower())
        for v in (t.get("query_variants") or []):
            existing.add(str(v or "").strip().lower())
    out: list[dict] = []
    for idx, raw in enumerate(raw_extra, start=1):
        task = normalize_task(raw, f"audit-{idx}", run_sites)
        if task is None or task["query"].lower() in existing:
            continue
        task["id"] = f"audit-{idx}"  # force a distinct id space so it never collides with a plan task
        task["query_variants"] = task["query_variants"][:1]  # surgical, not broad
        task["origin"] = "plan_audit"
        existing.add(task["query"].lower())
        out.append(task)
        if len(out) >= 2:
            break
    return out


def build_gap_audit_prompt(user_prompt: str, intent: dict, verified: list[dict],
                           host_dist: dict[str, int]) -> str:
    """Semantic coverage auditor of the VERIFIED RESULTS (distinct from the plan auditor, which sees
    only the plan). It gets a compact digest — the request, the extracted intent, the top verified
    findings as one-liners and the host distribution (incl. hosts that returned nothing) — and names
    up to 3 MATERIAL gaps: angles/channels of the question the current results plainly do not cover.
    Quality over quantity: an empty list is the correct answer when coverage is adequate."""
    intent = intent or {}
    intent_view = {
        "subject": intent.get("subject_keywords") or [],
        "exclude": intent.get("exclude_keywords") or [],
        "required_tier": intent.get("required_tier"),
        "price_basis": intent.get("price_basis"),
    }
    lines = []
    for f in verified[:15]:
        price = f.get("price")
        currency = f.get("currency")
        if price is not None and currency:
            price_str = f"{price} {currency}"
        elif f.get("price_usd") is not None:
            price_str = f"${f.get('price_usd')}"
        else:
            price_str = "price?"
        title = (str(f.get("title") or "").strip()[:80] or "?")
        lines.append(f"- {title} | {price_str} | {host_of(f.get('url')) or '?'}")
    findings_digest = "\n".join(lines) or "(no verified findings yet)"
    dist_lines = [
        f"- {h}: {n} result(s)" + ("  <-- returned NOTHING" if not n else "")
        for h, n in sorted(host_dist.items(), key=lambda kv: -kv[1])
    ]
    dist_digest = "\n".join(dist_lines) or "(none)"
    return f"""You audit the COVERAGE of a multi-model offer-research run AFTER its first verified
results. Decide whether the question is answered from enough angles, or whether whole aspects are
still uncovered. Return ONLY valid JSON. No Markdown.

User request:
{user_prompt}

Extracted intent:
{json.dumps(intent_view, ensure_ascii=False, sort_keys=True)}

Top verified findings so far:
{findings_digest}

Where results came from (host distribution):
{dist_digest}

Name up to 3 MATERIAL gaps — aspects/angles/channels of the user's question the findings above
plainly DO NOT cover. A gap is material when it is a real part of what the user asked and is missing,
e.g.: a missing CHANNEL TYPE (official store / big marketplace / classifieds / refurb-used / regional),
a missing REGION or local-language FORMULATION, a missing PRODUCT VARIANT family, or a venue category
that returned ZERO results yet plausibly has offers. For EACH gap give ONE concrete search query plus
a one-line reason it matters.
Do NOT invent gaps when coverage is already adequate — return an empty list. Verdict quality beats
quantity: a shorter honest list is better than padded guesses.

Schema:
{{"gaps": [{{"query": "specific search query", "reason": "one line: what aspect this closes"}}]}}
"""


def coerce_gap_queries(raw_gaps: object, tasks: list[dict] | None, config: dict) -> list[dict]:
    """Validate the semantic auditor's proposed gaps: keep at most 3 {query, reason} entries whose
    query does not duplicate (case-insensitive) an existing task query/variant or an earlier kept
    gap. Any non-list / malformed payload yields []."""
    if not isinstance(raw_gaps, list):
        return []
    seen = set()
    for t in (tasks or []):
        seen.add(str(t.get("query") or "").strip().lower())
        for v in (t.get("query_variants") or []):
            seen.add(str(v or "").strip().lower())
    out: list[dict] = []
    for raw in raw_gaps:
        if not isinstance(raw, dict):
            continue
        query = str(raw.get("query") or "").strip()
        if not query or query.lower() in seen:
            continue
        seen.add(query.lower())
        out.append({"query": query, "reason": str(raw.get("reason") or "").strip()})
        if len(out) >= 3:
            break
    return out


def gap_audit(prompt: str, intent: dict, verified: list[dict], host_dist: dict[str, int],
              run_dir: Path, config: dict, tasks: list[dict] | None = None) -> list[dict]:
    """Concurrent semantic coverage auditor. Advisory only: on ANY failure returns [] (non-fatal).
    Runs on the judge vendor (codex-first — no concurrency cap, so it never steals a search slot).
    Returns up to 3 non-duplicate gap queries. Emits gap_audit_finished + records run.json.gap_audit
    so the audit is observable even when the run proceeds without acting on it."""
    brain = judge_vendor(config)
    gaps: list[dict] = []
    try:
        record = call_model(
            brain,
            build_gap_audit_prompt(prompt, intent, verified, host_dist),
            run_dir,
            "gap_audit",
            "gap",
            timeout=180,
            effort="medium",
            claude_model=vendor_claude_model(brain, config),
        )
        if record.get("success"):
            payload = extract_json(record.get("stdout") or "")
            if isinstance(payload, dict):
                gaps = coerce_gap_queries(payload.get("gaps"), tasks, config)
    except (ValueError, KeyError, TypeError):
        gaps = []  # advisory: never let a malformed audit fault the run
    emit_event(run_dir, "gap_audit_finished", gaps=len(gaps),
               reasons=[g["reason"][:80] for g in gaps])
    update_run(run_dir, gap_audit={"gaps": len(gaps)})
    return gaps


def _parse_record_findings(record: dict) -> list[dict]:
    """extract_json + coerce_findings for one SUCCESSFUL record's stdout. Raises ValueError on a bad
    payload (parse_model_records turns that into a parse rejection). Single source of the per-record
    parse so the prefetch warmer and the batch parser can never diverge."""
    payload = extract_json(record.get("stdout") or "")
    return coerce_findings(payload, record["leg"], record["task_id"], record["record_id"])


def findings_from_record(record: dict) -> list[dict]:
    """Best-effort findings for the prefetch path: [] for a failed call or unparseable payload
    (the batch parse_model_records still records those as parse rejections). A successful parse is
    memoized on the record under `_parsed_findings` so the later parse_model_records batch reuses it
    instead of parsing the same stdout a second time. The key is private (underscored) and stripped
    from any serialized copy (see parse_model_records) so it never leaks into JSON artifacts.
    A ValueError is NOT memoized, so parse_model_records still sees the failure and rejects it."""
    if not record.get("success"):
        return []
    cached = record.get("_parsed_findings")
    if cached is not None:
        return cached
    try:
        parsed = _parse_record_findings(record)
    except ValueError:
        return []
    record["_parsed_findings"] = parsed
    return parsed


def parse_model_records(records: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    findings: list[dict] = []
    parse_rejections: list[dict] = []
    parsed_records: list[dict] = []

    for record in records:
        parsed = dict(record)
        parsed.pop("_parsed_findings", None)  # private parse memo — never persist it (write_model_stats)
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

        cached = record.get("_parsed_findings")  # warmed by findings_from_record during prefetch
        if cached is not None:
            record_findings = cached
        elif not (record.get("stdout") or "").strip():
            # Ran successfully but produced NO output (M1): that is an ABSENCE of findings, not a
            # rejected finding — so it yields no parse_failed placeholder. It still counts as a
            # completed empty call in the per-model stats (parse_failed=False, no_sources=True). A
            # real malformed payload (non-empty, unparseable) below still becomes a parse rejection.
            record_findings = []
        else:
            try:
                record_findings = _parse_record_findings(record)
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
            else:
                record["_parsed_findings"] = record_findings

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


def collect_with_straggler_drop(futures: list, run_dir: Path, config: dict,
                                on_record: object = None, fast_quorum_total: int | None = None,
                                expect_findings: bool = True) -> list[dict]:
    """Collect fan-out results; once STRAGGLER_QUORUM of calls are in, give the rest a bounded
    grace window, then kill them. Killed calls return as failures and flow into rescue. `on_record`
    (optional) fires for each record the moment its future completes — used to prefetch that
    record's finding URLs into the run URL cache while the slower calls are still running. It runs on
    the collecting thread and must never block or raise (failures are swallowed here).

    `fast_quorum_total`, when given, is the number of FAST-leg (non-SLOW_LEGS) calls in this batch:
    the quorum is then measured only against fast-leg completions, so a slow frontier leg (codex)
    can never gate the phase. The grace window opens as soon as the fast legs have largely returned,
    and the slow stragglers get the profile grace on top before being killed. Falls back to the old
    all-calls quorum when it is None or 0 (e.g. a phase with no fast legs), preserving prior behaviour."""
    records: list[dict] = []
    latencies: list[float] = []
    deadline: float | None = None
    pending = set(futures)
    use_fast = bool(fast_quorum_total)
    # Quorum hygiene (S1): only a call that RAN AND SUCCEEDED can deliver findings, so only such
    # calls advance the quorum. A skipped-without-running call (disabled leg / no budget / cancelled)
    # or a ran-and-FAILED call (rc != 0, e.g. rc=5 quota) will never deliver — it shrinks the
    # effective quorum base (the still-live jobs are the real denominator) instead of counting as
    # progress. In fast-quorum mode only fast legs count toward the base at all (the slow frontier
    # legs never gate the phase); the legacy no-fast-total path applies the same rule over every leg.
    ok = 0    # calls that ran AND succeeded (fast-only under use_fast; all legs in the legacy path)
    dead = 0  # calls skipped-without-running or ran-and-failed (same leg scope as `ok`)
    base = fast_quorum_total if use_fast else len(futures)
    while pending:
        wait_timeout = max(1.0, deadline - time.monotonic()) if deadline is not None else None
        done, pending = concurrent.futures.wait(
            pending, timeout=wait_timeout, return_when=concurrent.futures.FIRST_COMPLETED
        )
        for future in done:
            record = future.result()
            records.append(record)
            latencies.append(record.get("latency_sec") or 0.0)
            if (not use_fast) or record.get("leg") not in SLOW_LEGS:
                if record.get("success"):
                    ok += 1
                else:
                    dead += 1
            if on_record is not None:
                try:
                    on_record(record)
                except Exception:
                    pass
            update_run(run_dir, progress={"done": len(records), "total": len(futures)})
        if not pending:
            break
        # The effective base shrinks as jobs prove they cannot deliver. When it reaches 0 (every job
        # that could have delivered is dead) the quorum DISENGAGES — no deadline is armed, so the
        # phase waits for the remaining (slow) calls up to their own per-call timeouts instead of
        # letting a grace timer kill the one leg still capable of delivering. This is intentional:
        # when only the slow leg can deliver, we wait for it.
        effective_base = base - dead
        if deadline is None and effective_base > 0 and ok >= max(1, math.ceil(effective_base * STRAGGLER_QUORUM)):
            median = sorted(latencies)[len(latencies) // 2] if latencies else 0.0
            deadline = time.monotonic() + max(config["straggler_grace_sec"], median * 0.5)
        if deadline is not None and time.monotonic() >= deadline:
            # Zero-findings reaper guard (S2): killing the still-pending calls while NOTHING has been
            # found would guarantee an empty phase (a useless run). Extend the grace window instead and
            # let the pending calls keep working; the natural bound is each call's own subprocess
            # timeout — when they finish, `pending` empties and the loop exits (so this cannot spin
            # forever, and a cancel that reaps the calls also completes their futures). Once ANY finding
            # is in, revert to the normal reap so a healthy phase is not stretched by one slow leg.
            # expect_findings=False (verdict-shaped batches, e.g. model_verify) skips the guard —
            # those records NEVER parse as findings, so the guard would suppress the reaper entirely.
            found = sum(len(findings_from_record(r)) for r in records) if expect_findings else 1
            if found == 0:
                emit_event(run_dir, "straggler_grace_extended", waiting_for=len(pending))
            else:
                killed = kill_stragglers(run_dir.name)
                if killed:
                    emit_event(run_dir, "stragglers_killed", record_ids=killed)
            # Re-arm every grace interval: a job that spawned its subprocess AFTER the first sweep
            # (e.g. a late audit-added task) is reaped on the next pass, so the post-quorum wait stays
            # bounded to the grace window instead of stretching to the full phase timeout.
            deadline = time.monotonic() + max(1.0, config["straggler_grace_sec"])
    return records


def prefetch_url(url: object, host_registry: HostBlockRegistry, cache: UrlCheckCache) -> None:
    """Warm the run URL cache for one finding URL exactly as the batch verify would touch it:
    verify_url, then live_listing_check only when reachable AND a marketplace listing (mirrors
    check_one / apply_live_check). All failures swallowed — the batch path redoes/reads the cached
    result per normal semantics; this only changes timing, never outcomes."""
    try:
        check = verify_url(url, host_registry=host_registry, cache=cache)
        if check.get("ok") and listing_key(url):
            live_listing_check(url, host_registry=host_registry, cache=cache)
    except Exception:
        pass


def make_prefetch_collector(config: dict, host_registry: HostBlockRegistry | None,
                            cache: UrlCheckCache | None) -> tuple[object, object]:
    """Build (on_record, shutdown) for collect_with_straggler_drop's prefetch. on_record parses a
    just-completed record's findings and submits each URL to a small pool that warms the shared URL
    cache while slower calls still run. shutdown() is non-blocking (wait=False) AND drops the queued
    backlog (cancel_futures=True): in-flight prefetches only populate a cache, so on phase end / run
    cancel the queue must be abandoned rather than keep fetching for minutes (a cancelled server) or
    wedging interpreter exit on non-daemon join (one-shot CLI). Already-running fetches finish
    naturally (they are bounded). Returns (None, no-op) when prefetch isn't wired, leaving
    collect_with_straggler_drop's original behaviour."""
    if host_registry is None or cache is None:
        return None, (lambda: None)
    excluded = config.get("excluded_sites") or []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PREFETCH_WORKERS)

    def on_record(record: dict) -> None:
        for finding in findings_from_record(record):
            url = finding.get("url")
            if not url or (excluded and url_in_sites(url, excluded)):
                continue
            try:
                executor.submit(prefetch_url, url, host_registry, cache)
            except RuntimeError:
                return  # pool already shut down for this phase; stop submitting

    return on_record, (lambda: executor.shutdown(wait=False, cancel_futures=True))


def task_query_set(task: dict, n: int, n_angle: int = 0) -> list[dict]:
    """Expand a task into up to n surface-form query variants (base query first), plus up to n_angle
    ORTHOGONAL-ANGLE variants (effort-gated). Each variant is a task-shaped dict with a distinct id;
    results union back via listing-ID dedupe. n=1, n_angle=0 → just the base query (no expansion)."""
    queries, seen = [str(task.get("query") or "").strip()], set()
    seen.add(queries[0].lower())
    for v in (task.get("query_variants") or []):
        v = str(v or "").strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            queries.append(v)
    surface = [q for q in queries if q][:max(1, n)]
    out = [
        {**task, "query": q, "id": task["id"] if i == 0 else f'{task["id"]}#v{i + 1}'}
        for i, q in enumerate(surface)
    ]
    if n_angle > 0:
        angles = []
        for v in (task.get("angle_variants") or []):
            v = str(v or "").strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                angles.append(v)
        for j, q in enumerate(angles[:n_angle], start=1):
            out.append({**task, "query": q, "id": f'{task["id"]}#a{j}', "_angle": True})
    return out


def run_primary_search(prompt: str, tasks: list[dict], run_dir: Path, config: dict,
                       extra_tasks_supplier: object = None, extra_tasks_sink: list | None = None,
                       host_registry: HostBlockRegistry | None = None,
                       url_cache: UrlCheckCache | None = None) -> list[dict]:
    # All three families search in parallel at every level. Claude searches at the per-profile
    # claude_search_model tier (opus) under its own tight concurrency + per-run budget, so the
    # capped daily pool isn't drained (paced_budget trims it, drop-from-search removes Claude when
    # the day is starved). Each task is also expanded into query variants (beats search
    # phrase-adjacency; effort-gated) — results union via listing-ID dedupe. Leg budgets +
    # straggler drop bound the extra fan-out.
    search_legs = config.get("search_legs") or ["codex", "gemini"]
    n_variants = config.get("query_variants_per_task", 1)
    n_angle = int(config.get("angle_variants_per_task") or 0)
    # Anti-herding (effort >=3): every leg still searches every task (cross-check preserved), but
    # each gets a DIFFERENT source-class lean via rotation so legs explore complementary negative
    # space instead of all returning the same popular sites. SOFT lean (return strong offers from
    # anywhere), and big_marketplace stays in the rotation, so the cheapest mainstream offer is never
    # suppressed. off (effort 1-2) -> leg_focus=None -> byte-for-byte the old identical-prompt behaviour.
    differentiate = bool(config.get("differentiate_legs"))
    classes = list(SOURCE_CLASS_HINTS)
    timeout = min(RAW_TIMEOUT_SEC, config["search_timeout_sec"])
    # Codex is the slow long-pole leg: rather than one slow call per task (which used to gate the
    # whole phase), it covers only the BASE query of the first codex_task_cap tasks — a few
    # high-value calls the fast-leg-aware quorum never blocks on. The fast legs carry full breadth.
    codex_cap = int(config.get("codex_task_cap") or 0)

    def make_jobs(task_list: list[dict], n_var: int, n_ang: int, slow_used: int = 0) -> tuple[list[tuple], int]:
        # codex_task_cap is a per-PHASE budget, so the count of slow-leg jobs already emitted is
        # THREADED through (main tasks -> late audit tasks). Without this, the audit call restarted
        # rank at 0 and fired codex again on audit tasks even after the cap was spent on main tasks.
        jobs: list[tuple] = []
        g_idx = 0  # index over flattened task-variants; drives the differentiate class rotation
        for task in task_list:
            for v_i, tv in enumerate(task_query_set(task, n_var, n_ang)):
                for l_idx, leg in enumerate(search_legs):
                    if leg in SLOW_LEGS:
                        if v_i != 0 or slow_used >= codex_cap:
                            continue  # codex: base query only, until the per-phase cap is spent
                        slow_used += 1
                    focus = classes[(l_idx + g_idx) % len(classes)] if differentiate else None
                    jobs.append((leg, tv, focus))
                g_idx += 1
        # Slow legs first so codex spawns immediately (max time to land inside its grace) while the
        # fast legs — bounded by their own semaphores — fill the remaining worker slots behind it.
        jobs.sort(key=lambda j: j[0] not in SLOW_LEGS)
        return jobs, slow_used

    def submit(executor, job_list: list[tuple]) -> list:
        return [
            executor.submit(
                call_model,
                leg,
                build_search_prompt(prompt, tv, leg, config, leg_focus=focus),
                run_dir,
                "search",
                tv["id"],
                timeout,
                config["search_effort"],
                config.get("claude_search_model") if leg == "claude" else None,
            )
            for leg, tv, focus in job_list
        ]

    jobs, slow_used = make_jobs(tasks, n_variants, n_angle)
    # Provision extra worker slots up front (the pool size is fixed at creation) so any late
    # audit-added tasks (<=2, 1 variant each, all search legs) run alongside the initial fan-out.
    # The reserve rides ON TOP of MAX_PRIMARY_WORKERS: a plain min(MAX, jobs+reserve) would zero it
    # out whenever the base fan-out already saturates the cap, leaving late audit jobs queued behind.
    reserve = 2 * len(search_legs) if extra_tasks_supplier is not None else 0
    workers = max(1, min(len(jobs) + reserve, MAX_PRIMARY_WORKERS + reserve))
    on_record, prefetch_shutdown = make_prefetch_collector(config, host_registry, url_cache)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = submit(executor, jobs)
            submitted = list(jobs)
            # Concurrent plan auditor: the initial jobs are already running, so a BOUNDED wait for the
            # audit costs ~0 extra wall-clock (searches progress meanwhile). collect_with_straggler_drop
            # fixes its totals/quorum at call time, so we submit the late jobs BEFORE collecting.
            if extra_tasks_supplier is not None:
                extra_tasks = wait_for_extra_tasks(extra_tasks_supplier, timeout)
                if extra_tasks:
                    extra_jobs, slow_used = make_jobs(extra_tasks, 1, 0, slow_used)
                    futures += submit(executor, extra_jobs)
                    submitted += extra_jobs
                    if extra_tasks_sink is not None:
                        # Only the tasks that were actually SEARCHED — a late audit that missed the
                        # bounded wait yields nothing here, so the coverage grid / tasks.json never lists
                        # a source_class that was never queried.
                        extra_tasks_sink.extend(extra_tasks)
            fast_total = sum(1 for leg, _, _ in submitted if leg not in SLOW_LEGS)
            return collect_with_straggler_drop(futures, run_dir, config, on_record=on_record,
                                               fast_quorum_total=fast_total)
    finally:
        prefetch_shutdown()


def wait_for_extra_tasks(supplier: object, search_timeout: int) -> list[dict]:
    """Bounded wait on the plan-audit supplier (a Future-like with .result(timeout)). Waits at most
    min(90s, search_timeout/4); on timeout or any error returns [] and leaves the audit running in
    the background (its result is recorded by audit_plan itself, just unused by this phase)."""
    wait_sec = min(90.0, max(1.0, search_timeout / 4))
    try:
        return list(supplier.result(timeout=wait_sec) or [])
    except Exception:
        return []


def run_rechecks(
    prompt: str,
    items: list[dict],
    run_dir: Path,
    config: dict,
    round_no: int,
    attempts: dict[str, set[str]],
    known_urls: list[str] | None = None,
    timeout_cap: int | None = None,
    host_registry: HostBlockRegistry | None = None,
    url_cache: UrlCheckCache | None = None,
) -> tuple[list[dict], int]:
    """Rescue pass for rejected/disputed items. `attempts` maps canonical item key -> legs that
    already tried it, so each round can hand the item to a model that has NOT tried yet (the
    other vendor first, then the originating one). Returns (records, dropped_by_cap)."""
    jobs = []
    seen_keys: set[str] = set()
    items_used = 0
    dropped = 0
    run_id = run_dir.name
    # Per-phase slow-leg (codex) cap (P1): those calls get straggler-killed most of the time, so an
    # unbounded share just stretches the round by the grace window for nothing. Hand codex only the
    # first codex_task_cap items (the cheapest — items arrive price-sorted); the rest cycle fast legs.
    codex_cap = int(config.get("codex_task_cap") or 0)
    slow_used = 0
    # Network-blocked items are NOT excluded from rescue even though the model-verify stage (Q1) will
    # also look at the cheapest of them: that stage is conditional (time budget, web-leg availability,
    # leg budget) and selects from a LATER snapshot of the rejected pool, so any exclusion here can
    # strand an item with zero recovery paths. The double-spend is bounded by max_recheck_items and
    # model_verify_cap; model-verify stays purely additive.
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
            if leg not in tried and not leg_disabled(run_id, leg) and not user_disabled(run_id, leg)
        ]
        if slow_used >= codex_cap:
            untried = [leg for leg in untried if leg not in SLOW_LEGS]
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
            if leg in SLOW_LEGS:
                slow_used += 1
            jobs.append((leg, item, f"recheck-{round_no}-{items_used}-{leg}"))
    if not jobs:
        return [], dropped

    timeout = min(RAW_TIMEOUT_SEC, config["recheck_timeout_sec"])
    if timeout_cap is not None:
        timeout = min(timeout, timeout_cap)
    on_record, prefetch_shutdown = make_prefetch_collector(config, host_registry, url_cache)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(jobs))) as executor:
            futures = [
                executor.submit(
                    call_model,
                    leg,
                    build_recheck_prompt(prompt, item, config, known_urls_minus_item(known_urls, item)),
                    run_dir,
                    "recheck",
                    task_id,
                    timeout,
                    config["search_effort"],
                    config.get("claude_search_model") if leg == "claude" else None,
                )
                for leg, item, task_id in jobs
            ]
            fast_total = sum(1 for leg, _, _ in jobs if leg not in SLOW_LEGS)
            records = collect_with_straggler_drop(futures, run_dir, config, on_record=on_record,
                                                  fast_quorum_total=fast_total)
    finally:
        prefetch_shutdown()
    return records, dropped


def select_model_verify_candidates(items: list[dict], config: dict) -> list[dict]:
    """The top-model_verify_cap cheapest network-blocked candidates (USD-ranked via sort_by_usd so
    mixed currencies compare correctly; native-price-only items sort last rather than as huge
    pseudo-USD numbers). Model-verify is purely ADDITIVE on top of rescue: rescue deliberately does
    NOT exclude these items, because this stage is conditional and selects from a later snapshot of
    the rejected pool — coupling the two sets stranded items with zero recovery paths."""
    cap = int(config.get("model_verify_cap") or 0)
    if cap <= 0:
        return []
    return sort_by_usd([it for it in items if model_verify_eligible(it)])[:cap]


def run_model_verify(prompt: str, rejected: list[dict], run_dir: Path, config: dict,
                     intent: dict | None, model_verdicts: dict,
                     host_registry: HostBlockRegistry | None = None,
                     url_cache: UrlCheckCache | None = None) -> int:
    """Model-assisted verification of network-blocked candidates (Q1). Takes the top-K cheapest
    rejected items whose failures are ALL network-verification-class (model_verify_eligible) and fires
    one web-capable model call per candidate to open the page first-hand. Each verdict is recorded in
    `model_verdicts` (keyed by dedupe_key) so the caller's re-verify promotes the live ones DURABLY
    (apply_model_verdict / verify_findings). One leg for the whole stage: prefer claude, else gemini,
    else skip. Returns the number of candidates that produced a usable verdict."""
    run_id = run_dir.name
    candidates = select_model_verify_candidates(rejected, config)
    if not candidates:
        return 0

    # One web-capable leg for the whole stage. call_model does NOT budget-gate task_type=model_verify,
    # so each candidate reserves its OWN leg-budget slot here; the stage is skipped when neither leg is
    # available (disabled/quota) or has any budget left.
    leg = None
    for cand_leg in ("claude", "gemini"):
        if user_disabled(run_id, cand_leg) or leg_disabled(run_id, cand_leg):
            continue
        if consume_leg_budget(run_id, cand_leg):
            leg = cand_leg
            break
    if leg is None:
        return 0
    jobs = [candidates[0]]  # the pick above already reserved this candidate's slot
    for cand in candidates[1:]:
        if consume_leg_budget(run_id, leg):
            jobs.append(cand)
        else:
            break

    emit_event(run_dir, "model_verify_started", count=len(jobs), leg=leg)
    timeout = min(config["recheck_timeout_sec"], 240)
    by_task: dict[str, dict] = {}
    call_jobs: list[tuple[str, dict]] = []
    for i, item in enumerate(jobs):
        task_id = f"mv-{i}"
        by_task[task_id] = item
        call_jobs.append((task_id, item))
    on_record, prefetch_shutdown = make_prefetch_collector(config, host_registry, url_cache)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(call_jobs))) as executor:
            futures = [
                executor.submit(call_model, leg, build_model_verify_prompt(prompt, item, config),
                                run_dir, "model_verify", task_id, timeout, config["search_effort"],
                                config.get("claude_search_model") if leg == "claude" else None)
                for task_id, item in call_jobs
            ]
            fast_total = sum(1 for _ in call_jobs if leg not in SLOW_LEGS)
            records = collect_with_straggler_drop(futures, run_dir, config, on_record=on_record,
                                                  fast_quorum_total=fast_total,
                                                  expect_findings=False)
    finally:
        prefetch_shutdown()

    checked = 0
    for record in records:
        item = by_task.get(record.get("task_id"))
        if item is None or not record.get("success"):
            # The model call itself failed/skipped — leave the item's network rejection as-is and
            # return the pre-consumed budget slot (call_model does not refund model_verify calls).
            if item is not None:
                refund_leg_budget(run_id, leg)
            continue
        try:
            payload = extract_json(record.get("stdout") or "")
        except ValueError:
            refund_leg_budget(run_id, leg)
            continue
        if not isinstance(payload, dict):
            refund_leg_budget(run_id, leg)
            continue
        checked += 1
        model_verdicts[dedupe_key(item)] = {
            "opened": bool(payload.get("opened")),
            "live": bool(payload.get("live")),
            "price": payload.get("price"),
            "currency": payload.get("currency"),
            "availability": payload.get("availability"),
            "seller": payload.get("seller"),
            "title": payload.get("title"),
            "url": payload.get("url"),
            "notes": payload.get("notes"),
            "leg": leg,
            "checked_at": utc_now(),
        }
    return checked


def aggregate_adjudications(verdicts: list[dict]) -> dict | None:
    """Self-consistency aggregate over N arbiter samples: majority action (accept wins ties — the
    arbiter only rejects on clear evidence), median accepted price, modal currency."""
    if not verdicts:
        return None
    actions = [v.get("action") for v in verdicts]
    action = "reject" if actions.count("reject") > actions.count("accept") else "accept"
    chosen = [v for v in verdicts if v.get("action") == action]
    out: dict = {"action": action, "reason": (chosen[0].get("reason") if chosen else None)}
    if action == "accept":
        prices = sorted(p for p in (parse_price(v.get("price")) for v in chosen) if p is not None)
        if prices:
            out["price"] = prices[len(prices) // 2]  # median resists a single outlier sample
            curs = [str(v.get("currency")) for v in chosen if v.get("currency")]
            if curs:
                out["currency"] = max(set(curs), key=curs.count)
    return out


def adjudicate_disputes(prompt: str, verified: list[dict], rejected: list[dict], run_dir: Path, config: dict) -> tuple[list[dict], list[dict]]:
    """Claude (thin arbiter, third vendor) settles items where codex and gemini still disagree."""
    disputed = [item for item in verified if item.get("disputed")][:MAX_ADJUDICATED_ITEMS]
    if not disputed:
        return verified, rejected

    n_samples = max(1, int(config.get("adjudicate_samples") or 1))

    def adjudicate_one(idx: int, item: dict) -> tuple[dict, dict | None]:
        adj_prompt = build_adjudication_prompt(prompt, item, config)
        arbiter = arbiter_vendor(config)  # prefer Claude, else any enabled vendor
        # Self-consistency (effort >=3): independent samples + majority vote on which price the
        # evidence supports. Fenced to dispute resolution ONLY (a single canonical answer exists) —
        # never to discovery, where a majority vote would amplify herding. n_samples=1 == old behaviour.
        verdicts: list[dict] = []
        for s in range(n_samples):
            sp = adj_prompt if n_samples == 1 else adj_prompt + f"\n(Independent assessment {s + 1} of {n_samples}.)"
            tag = f"dispute-{idx}" if n_samples == 1 else f"dispute-{idx}-s{s + 1}"
            record = call_model(
                arbiter, sp, run_dir, "adjudicate", tag,
                timeout=600, claude_model=vendor_claude_model(arbiter, config),
            )
            # Reserve: if the native pool is exhausted/down, fall back to Claude-via-agy (separate
            # quota pool) — only when Claude itself wasn't disabled by the user for this run.
            if not record["success"] and "claude" in (config.get("enabled_legs") or []):
                reserve = call_agy_claude(sp, run_dir, "adjudicate", f"{tag}-agy")
                if reserve["success"]:
                    record = reserve
            if record["success"]:
                try:
                    v = extract_json(record["stdout"])
                    if isinstance(v, dict) and v.get("action") in ("accept", "reject"):
                        verdicts.append(v)
                except ValueError:
                    pass
        return item, aggregate_adjudications(verdicts)

    results: list[tuple[dict, dict | None]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(disputed))) as executor:
        futures = [executor.submit(adjudicate_one, idx, item) for idx, item in enumerate(disputed, start=1)]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            update_run(run_dir, progress={"done": len(results), "total": len(disputed)})

    to_reject: list[str] = []
    for item, verdict in results:
        if not verdict:
            continue  # arbiter unavailable / no parsable verdict -> item stays flagged disputed
        if verdict.get("action") == "reject":
            item["adjudication"] = verdict.get("reason")
            to_reject.append(item.get("record_id"))
        elif verdict.get("action") == "accept":
            price = parse_price(verdict.get("price"))
            if price is not None:
                item["price"] = price
                if verdict.get("currency"):
                    item["currency"] = canon_currency(verdict["currency"]) or item.get("currency")
                # The adjudicated price is the new truth — recompute the derived USD fields, or
                # sort_by_usd / credible_floor_usd / synthesis rank it by its stale pre-verdict USD.
                item["price_usd"] = to_usd(item["price"], item.get("currency"))
                set_monthly_usd(item)
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
    # Chain-of-Verification (factored): derive DISCRETE verification questions and have the vendor
    # answer EACH from the live page (not from memory) — the factored form is the load-bearing part
    # of CoVe (independent sub-checks beat one joint "is it still good?" judgment).
    legs = [lg for lg in (config.get("search_legs") or ["codex", "gemini"]) if not leg_disabled(run_dir.name, lg)]
    if legs:
        leg = legs[0]
        req_tier = (intent or {}).get("required_tier")
        tier_q = f"4. tier_ok: does the page's tier/variant match the required '{req_tier}'?\n" if req_tier else "4. tier_ok: (no tier requirement — return true)\n"
        fp = (f"Open this exact URL and RE-READ the live page to fact-check a recommendation. Use ONLY "
              f"what the page shows now, not prior knowledge.\n"
              f"URL: {url}\nUser wants: {prompt}\n"
              f"Recommended: ~{top.get('price_usd')} USD ({top.get('price')} {top.get('currency')}).\n"
              f"Answer EACH question independently from the page:\n"
              f"1. live: is the listing currently active / in stock (not sold, removed, or expired)?\n"
              f"2. same_item: is the page selling the SAME thing the user wants (right product, not a "
              f"different or re-listed item)?\n"
              f"3. price_ok: is the page's current price within ~15% of the recommended price?\n"
              f"{tier_q}"
              f"Return ONLY JSON: {{\"live\": bool, \"same_item\": bool, \"price_ok\": bool, "
              f"\"tier_ok\": bool, \"reason\": \"one line\"}}. Be conservative: if the page won't load "
              f"or a fact is unclear, set THAT field false.")
        rec = call_model(leg, fp, run_dir, "factcheck", "top-pick", timeout=300,
                         effort=config["judge_effort"], claude_model=config.get("claude_search_model") if leg == "claude" else None, bypass_breaker=True)
        if rec["success"]:
            try:
                ans = extract_json(rec.get("stdout") or "")
            except ValueError:
                ans = None
            if isinstance(ans, dict):
                keys = ("live", "same_item", "price_ok", "tier_ok")
                if any(k in ans for k in keys):
                    # Conservative on a PARTIAL answer: a missing key is a failed check (matches the
                    # prompt's "if unclear, set THAT field false"), so a real refutation that drops a
                    # key is never silently lost.
                    checks = {k: bool(ans.get(k)) for k in keys}
                    confirmed = all(checks.values())
                    verdict["vendor_checks"] = checks
                elif "confirmed" in ans:  # tolerate a model that answers in the old joint form
                    confirmed = bool(ans["confirmed"])
                else:
                    confirmed = None
                if confirmed is not None:
                    verdict["vendor_confirmed"] = confirmed
                    verdict["vendor_reason"] = str(ans.get("reason") or "")[:200]
                    if confirmed is False:
                        failed = ",".join(k for k in keys if not ans.get(k)) or "joint"
                        verdict.update(ok=False, reason=f"vendor_refuted ({failed}): " + verdict["vendor_reason"])
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


def synthesize_report(prompt: str, tasks: list[dict], verified: list[dict], rejected: list[dict], run_dir: Path, config: dict, intent: dict | None = None, skipped_stages: list[str] | None = None) -> str:
    degraded = disabled_legs(run_dir.name)
    if not verified:
        return fallback_report(prompt, verified, rejected, degraded)
    # The judge seat is the run's whole value: if the first judge is down (quota), another ENABLED
    # vendor takes the seat rather than dumping an unranked fallback list on the user.
    for judge in judge_chain(config):
        record = call_model(
            judge,
            build_synthesis_prompt(prompt, tasks, verified, rejected, config, degraded, intent, skipped_stages),
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
            build_synthesis_prompt(prompt, tasks, verified, rejected, config, degraded, intent, skipped_stages),
            run_dir, "synthesize", "final-claude-agy", timeout=700,
        )
        if reserve["success"] and reserve.get("stdout", "").strip():
            return reserve["stdout"].strip() + "\n"
    return fallback_report(prompt, verified, rejected, degraded)


def budget_remaining_sec(started_monotonic: float, config: dict) -> float | None:
    """Wall-clock seconds left before the run's total time budget is spent, or None when no budget
    is configured (unbounded). time_budget_sec is the TOTAL run cap from the effort profile."""
    total = config.get("time_budget_sec")
    if not total:
        return None
    return float(total) - (time.monotonic() - started_monotonic)


def stage_fits_budget(started_monotonic: float, config: dict, reserve_sec: float = SYNTHESIS_RESERVE_SEC) -> bool:
    """Whether an OPTIONAL stage may still run: only when more than the synthesis reserve remains, so
    the value-delivering synthesis/review/fact-check always get to run inside the budget. Always True
    when no budget is configured (unbounded)."""
    remaining = budget_remaining_sec(started_monotonic, config)
    return remaining is None or remaining > reserve_sec


def clamp_round_timeout(configured_timeout: int, started_monotonic: float, config: dict,
                        reserve_sec: float = SYNTHESIS_RESERVE_SEC) -> int:
    """Shrink an optional round's per-phase timeout so a single round cannot eat into the synthesis
    reserve; never drops below a 60s floor. Returns the configured timeout unchanged when unbounded."""
    remaining = budget_remaining_sec(started_monotonic, config)
    if remaining is None:
        return configured_timeout
    return int(min(configured_timeout, max(60, remaining - reserve_sec)))


def emit_deadline_skip(run_dir: Path, stage: str, remaining_sec: float | None, skipped: list[str]) -> None:
    """Record that an optional stage was skipped to stay within the time budget: stream the event,
    accumulate the stage name (deduped) into `skipped`, and persist the list to run.json for the UI."""
    emit_event(run_dir, "stage_skipped_deadline", stage=stage,
               remaining_sec=int(remaining_sec) if remaining_sec is not None else None)
    if stage not in skipped:
        skipped.append(stage)
    update_run(run_dir, skipped_by_deadline=list(skipped))


def execute_research(run_dir: Path, prompt: str, config: dict) -> None:
    run_id = run_dir.name
    started = time.monotonic()
    sites = config.get("sites") or []
    excluded = config.get("excluded_sites") or []
    all_parsed_records: list[dict] = []
    verified: list[dict] = []
    rejected: list[dict] = []
    gap_queries: list[dict] = []  # semantic-gap follow-up queries, filled after the recheck loop
    emitted_findings: dict = {}  # shared across rounds so a finding streams once (not every re-verify)
    host_blocks = HostBlockRegistry()  # shared across rounds: a bot-walled host stays throttled all run
    url_cache = UrlCheckCache()  # shared across rounds: search-phase prefetch warms it for the batch verify
    model_verdicts: dict = {}  # Q1: {dedupe_key -> verdict} fed to every verify_findings so a model
    # promotion (or model rejection) of a network-blocked item is durable across later re-verifies.
    ACTIVE_RUNS.add(run_id)
    init_leg_health(run_id)
    set_user_disabled(run_id, config.get("disabled_legs") or [])
    set_run_gemini_model(run_id, config.get("gemini_model"))
    requested_budgets = {"gemini": config["gemini_call_budget"]}
    if "claude" in (config.get("search_legs") or []):
        requested_budgets["claude"] = config.get("claude_search_budget", 0)
    # Quota-aware pacing: clamp each leg's per-run budget to its remaining daily allowance so a
    # single run can't exhaust the day's quota. Surfaced in run.json for the UI/scoreboard.
    leg_budgets, pacing = {}, {}
    today_counts = daily_call_counts()  # read the log once, price every leg from the same snapshot
    for leg, requested in requested_budgets.items():
        reserve = CLAUDE_SEARCH_RESERVE if leg == "claude" else 0
        budget, remaining = paced_budget(leg, requested, reserve=reserve, counts=today_counts)
        leg_budgets[leg] = budget
        pacing[leg] = {"requested": requested, "granted": budget, "daily_remaining": remaining,
                       "daily_cap": DAILY_CAPS.get(leg)}
    # Starved-day degradation: if Claude's search budget paced down to 0, drop it from THIS run's
    # search legs (recheck/coverage/frontier read search_legs too) instead of burning job slots on
    # skipped_by_budget calls. Claude still adjudicates disputes — arbiter_vendor is independent of
    # search_legs. The trimmed config is local to this run; run.json keeps the requested profile.
    if "claude" in (config.get("search_legs") or []) and leg_budgets.get("claude", 0) <= 0:
        config = dict(config)
        config["search_legs"] = [l for l in config["search_legs"] if l != "claude"]
        pacing.setdefault("claude", {})["dropped_from_search"] = True
    init_leg_budget(run_id, leg_budgets)
    update_run(run_dir, pacing=pacing)

    def check_cancel() -> None:
        if run_cancelled(run_id):
            raise RunCancelled()

    skipped_by_deadline: list[str] = []

    def record_deadline_skip(stage: str) -> None:
        emit_deadline_skip(run_dir, stage, budget_remaining_sec(started, config), skipped_by_deadline)

    audit_executor = None
    audit_future = None
    gap_executor = None
    gap_future = None
    try:
        update_run(run_dir, status="running", phase="decomposing", progress=None)
        tasks, intent = decompose_tasks(prompt, run_dir, config)
        write_json(run_dir / "tasks.json", {"tasks": tasks, "intent": intent})
        update_run(run_dir, intent=intent)
        check_cancel()

        # ---- interactive clarify gate ----
        # Only interactive runs (UI / --ask) can pause here; the non-interactive CLI default and
        # API-without-flag never enter either branch and proceed exactly as before. When a question is
        # asked we park in a bounded poll loop, then either re-decompose ONCE with the answer or
        # proceed on the assumed default reading. The waited seconds are EXCLUDED from the time budget
        # by pushing `started` forward, so parking on a question never eats into the search window
        # (record_deadline_skip / stage gates all read the same `started`).
        ask_clarify, clarify_q, clarify_alts = should_ask_clarify(config, intent)
        if config.get("interactive") and not ask_clarify:
            emit_event(run_dir, "clarify_resolved", asked=False)
        elif ask_clarify:
            update_run(run_dir, phase="clarify", progress=None)
            emit_event(run_dir, "clarify_pending", question=clarify_q,
                       alternatives=clarify_alts, timeout_sec=CLARIFY_TIMEOUT_SEC)
            wait_started = time.monotonic()
            answer = wait_for_clarification(run_dir, run_id, CLARIFY_TIMEOUT_SEC, check_cancel=check_cancel)
            started += time.monotonic() - wait_started  # don't bill the user's think time to the budget
            if answer and not answer.get("skip") and answer.get("answer"):
                emit_event(run_dir, "clarify_resolved", asked=True, answered=True)
                update_run(run_dir, phase="decomposing", progress=None)
                # Rebind the local prompt so ALL downstream stages use the disambiguated request,
                # not only this re-decompose (run.json still holds the original for the UI header).
                prompt = build_clarified_prompt(prompt, answer["answer"])
                tasks, intent = decompose_tasks(prompt, run_dir, config)
                write_json(run_dir / "tasks.json", {"tasks": tasks, "intent": intent})
                update_run(run_dir, intent=intent,
                           clarify={"asked": True, "answered": True, "question": clarify_q,
                                    "answer": answer["answer"]})
            else:
                emit_event(run_dir, "clarify_resolved", asked=True, answered=False)
                assumed = clarify_alts[0] if clarify_alts else None
                intent["assumed"] = assumed  # flows into build_synthesis_prompt via the intent context
                write_json(run_dir / "tasks.json", {"tasks": tasks, "intent": intent})
                update_run(run_dir, intent=intent,
                           clarify={"asked": True, "answered": False, "question": clarify_q,
                                    "assumed": assumed})
            check_cancel()

        # Concurrent plan auditor (effort >=2): kick it off BEFORE the search so it overlaps the
        # primary-search window. run_primary_search does a bounded wait on this future and folds any
        # accepted extra tasks into the SAME fan-out — no serial phase, ~0 extra wall-clock.
        if config.get("plan_audit"):
            emit_event(run_dir, "plan_audit_started")
            audit_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            audit_future = audit_executor.submit(audit_plan, prompt, tasks, intent, run_dir, config)

        update_run(run_dir, phase="primary_search", progress=None)
        audit_added: list[dict] = []
        primary_records = run_primary_search(prompt, tasks, run_dir, config,
                                             extra_tasks_supplier=audit_future,
                                             extra_tasks_sink=audit_added,
                                             host_registry=host_blocks, url_cache=url_cache)
        if audit_added:
            tasks = tasks + audit_added
            write_json(run_dir / "tasks.json", {"tasks": tasks, "intent": intent})
        findings, parse_rejections, parsed_records = parse_model_records(primary_records)
        all_parsed_records.extend(parsed_records)
        write_json(run_dir / "findings.json", {"stage": "primary", "findings": findings, "parse_rejections": parse_rejections})
        check_cancel()

        update_run(run_dir, phase="verifying", progress=None)
        verified, rejected = verify_findings(findings, parse_rejections, sites, intent, run_dir=run_dir, excluded_sites=excluded, stage="primary", emitted=emitted_findings, host_registry=host_blocks, cache=url_cache, model_verdicts=model_verdicts)
        record_stage_results(run_dir, "primary", verified, rejected)
        check_cancel()

        # Concurrent semantic-gap auditor (effort >=2): kick it off BEFORE the rescue loop so its one
        # call hides inside the recheck window (~0 extra wall-clock). It deliberately audits the
        # POST-PRIMARY verified state — rescue only recovers already-known items, it never changes
        # coverage — and its follow-up queries are collected right after the loop.
        if config.get("gap_audit") and verified:
            emit_event(run_dir, "gap_audit_started")
            gap_dist = dict(host_distribution(verified))
            requested_sites = config.get("sites") or [s for t in tasks for s in (t.get("preferred_sites") or [])]
            for s in requested_sites:
                gap_dist.setdefault(s, 0)  # zero-result hosts feed the auditor a real coverage gap
            gap_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            gap_future = gap_executor.submit(gap_audit, prompt, intent, list(verified), gap_dist,
                                             run_dir, config, tasks)

        rescue_attempts: dict[str, set[str]] = {}
        recheck_dropped = 0
        for round_no in range(1, config["recheck_rounds"] + 1):
            # The first rescue round always runs (rescue philosophy); later rounds are optional and
            # yield to the synthesis reserve.
            if round_no > 1 and not stage_fits_budget(started, config):
                record_deadline_skip("recheck")
                break
            update_run(run_dir, phase=f"rechecking_{round_no}", progress=None)
            disputed = [item for item in verified if item.get("disputed")]
            cap = clamp_round_timeout(config["recheck_timeout_sec"], started, config) if round_no > 1 else None
            known = [v.get("url") for v in verified if v.get("url")]
            recheck_records, dropped = run_rechecks(prompt, rejected + disputed, run_dir, config, round_no,
                                                    rescue_attempts, known_urls=known, timeout_cap=cap,
                                                    host_registry=host_blocks, url_cache=url_cache)
            recheck_dropped += dropped
            if not recheck_records:
                break
            recheck_findings, recheck_parse_rejections, recheck_parsed = parse_model_records(recheck_records)
            all_parsed_records.extend(recheck_parsed)
            findings = findings + recheck_findings
            parse_rejections = parse_rejections + recheck_parse_rejections
            verified, rejected = verify_findings(findings, parse_rejections, sites, intent, run_dir=run_dir, excluded_sites=excluded, stage=f"rescue {round_no}", emitted=emitted_findings, host_registry=host_blocks, cache=url_cache, model_verdicts=model_verdicts)
            record_stage_results(run_dir, f"rescue {round_no}", verified, rejected)
            check_cancel()
        if recheck_dropped:
            # No silent caps: the report and UI must show how many candidates the budget skipped.
            update_run(run_dir, recheck_dropped=recheck_dropped)
        check_cancel()

        # The gap audit overlapped the rescue window; collect its follow-up queries now. Any failure
        # is advisory — gap_audit swallows it and returns [], so this never faults the run.
        if gap_future is not None:
            try:
                gap_queries = gap_future.result() or []
            except Exception:
                gap_queries = []

        # ---- model-assisted verification of network-blocked candidates (Q1) ----
        # Right after rescue + gap-audit collection, BEFORE coverage/gap/frontier, so a promoted offer
        # lifts the frontier ceiling and informs coverage. Only items whose failures are ALL
        # network-verification-class qualify (model_verify_eligible) — those our plain-HTTP client
        # bot-walled, never a semantic reject — and they are ALSO kept out of the rescue loop so we do
        # not waste an LLM call re-finding the same blocked URL. Advisory: wrapped like gap_audit so a
        # failure here never faults the paid run, and gated by stage_fits_budget.
        if config.get("model_verify_cap") and rejected and stage_fits_budget(started, config):
            update_run(run_dir, phase="model_verifying", progress=None)
            before = len(verified)
            try:
                checked = run_model_verify(prompt, rejected, run_dir, config, intent, model_verdicts,
                                           host_registry=host_blocks, url_cache=url_cache)
            except Exception:
                checked = 0  # advisory: a model-verify failure never faults the run
            if checked:
                verified, rejected = verify_findings(findings, parse_rejections, sites, intent, run_dir=run_dir, excluded_sites=excluded, stage="model_verify", emitted=emitted_findings, host_registry=host_blocks, cache=url_cache, model_verdicts=model_verdicts)
                record_stage_results(run_dir, "model_verify", verified, rejected)
            emit_event(run_dir, "model_verify_finished", promoted=max(0, len(verified) - before), checked=checked)
            check_cancel()

        # Coverage-gap rounds (effort >=3): search the (structured) source-classes that produced
        # nothing credible, steering away from saturated hosts. Distinct from rescue (recovers
        # rejected) and frontier (chases cheaper). Stops when no class is empty, a round adds no new
        # verified listing, or the budget runs out. Only ADDS candidates that pass the same gate.
        # The semantic-gap follow-ups MERGE into the coverage round's fan-out (only on round 1, so a
        # second coverage round never re-searches them) — no serial phase is added for gaps here.
        for round_no in range(1, config.get("coverage_rounds", 0) + 1):
            all_classes = {t.get("source_class") for t in tasks if t.get("source_class")}
            missing = sorted(all_classes - covered_source_classes(verified, tasks))
            round_gaps = gap_queries if round_no == 1 else []
            if not missing and not round_gaps:
                break
            if not stage_fits_budget(started, config):
                record_deadline_skip("coverage")
                break
            update_run(run_dir, phase=f"coverage_{round_no}", progress=None)
            avoid_hosts = [h for h, _ in sorted(host_distribution(verified).items(), key=lambda kv: -kv[1])[:3]]
            cap = clamp_round_timeout(config["search_timeout_sec"], started, config)
            coverage_records = run_coverage_round(prompt, missing, avoid_hosts, run_dir, config, intent,
                                                  timeout_cap=cap, gap_queries=round_gaps,
                                                  host_registry=host_blocks, url_cache=url_cache)
            if not coverage_records:
                break
            c_findings, c_parse_rej, c_parsed = parse_model_records(coverage_records)
            all_parsed_records.extend(c_parsed)
            findings = findings + c_findings
            parse_rejections = parse_rejections + c_parse_rej
            prev_keys = {dedupe_key(v) for v in verified}
            verified, rejected = verify_findings(findings, parse_rejections, sites, intent, run_dir=run_dir, excluded_sites=excluded, stage=f"coverage {round_no}", emitted=emitted_findings, host_registry=host_blocks, cache=url_cache, model_verdicts=model_verdicts)
            record_stage_results(run_dir, f"coverage {round_no}", verified, rejected)
            check_cancel()
            if not ({dedupe_key(v) for v in verified} - prev_keys):  # novelty-exhausted: stop
                break

        # Effort-2 semantic-gap mini-wave: at this level coverage rounds are OFF, so the merged path
        # above never ran. When the auditor found material gaps AND the budget allows it, fire ONE
        # bounded sweep of just those gap queries (reusing run_coverage_round with no missing classes).
        if config.get("coverage_rounds", 0) == 0 and gap_queries and stage_fits_budget(started, config):
            update_run(run_dir, phase="gap_search", progress=None)
            avoid_hosts = [h for h, _ in sorted(host_distribution(verified).items(), key=lambda kv: -kv[1])[:3]]
            cap = clamp_round_timeout(config["search_timeout_sec"], started, config)
            gap_records = run_coverage_round(prompt, [], avoid_hosts, run_dir, config, intent,
                                             timeout_cap=cap, gap_queries=gap_queries,
                                             host_registry=host_blocks, url_cache=url_cache)
            if gap_records:
                g_findings, g_parse_rej, g_parsed = parse_model_records(gap_records)
                all_parsed_records.extend(g_parsed)
                findings = findings + g_findings
                parse_rejections = parse_rejections + g_parse_rej
                verified, rejected = verify_findings(findings, parse_rejections, sites, intent, run_dir=run_dir, excluded_sites=excluded, stage="gap", emitted=emitted_findings, host_registry=host_blocks, cache=url_cache, model_verdicts=model_verdicts)
                record_stage_results(run_dir, "gap", verified, rejected)
                check_cancel()

        # Frontier rounds: push strictly below the current best credible price until a round finds
        # nothing cheaper-and-credible (dry) or the round budget is spent. Effort-gated.
        for round_no in range(1, config.get("frontier_rounds", 0) + 1):
            ceiling = credible_floor_usd(verified)
            if ceiling is None:
                break
            if not stage_fits_budget(started, config):
                record_deadline_skip("frontier")
                break
            update_run(run_dir, phase=f"frontier_{round_no}", progress=None)
            cap = clamp_round_timeout(config["search_timeout_sec"], started, config)
            known = [v.get("url") for v in verified if v.get("url")]
            frontier_records = run_frontier_round(prompt, ceiling, run_dir, config, intent, round_no,
                                                  known_urls=known, timeout_cap=cap,
                                                  host_registry=host_blocks, url_cache=url_cache)
            if not frontier_records:
                break
            f_findings, f_parse_rej, f_parsed = parse_model_records(frontier_records)
            all_parsed_records.extend(f_parsed)
            findings = findings + f_findings
            parse_rejections = parse_rejections + f_parse_rej
            verified, rejected = verify_findings(findings, parse_rejections, sites, intent, run_dir=run_dir, excluded_sites=excluded, stage=f"frontier {round_no}", emitted=emitted_findings, host_registry=host_blocks, cache=url_cache, model_verdicts=model_verdicts)
            record_stage_results(run_dir, f"frontier {round_no}", verified, rejected)
            new_floor = credible_floor_usd(verified)
            check_cancel()
            if new_floor is None or new_floor >= ceiling:  # dry round — nothing credible cheaper
                break

        if config["adjudicate_disputes"] and any(item.get("disputed") for item in verified):
            update_run(run_dir, phase="adjudicating", progress=None)
            verified, rejected = adjudicate_disputes(prompt, verified, rejected, run_dir, config)
            # Adjudication can move disputed items between buckets; persist + summarize so the yield
            # strip's last pill and the poll fallback match the final counts (not the pre-adjudicate ones).
            record_stage_results(run_dir, "adjudicate", verified, rejected)
            check_cancel()

        apply_trust(verified)  # structured seller/source trust for ranking + the UI
        apply_confidence(verified)  # calibrated 0..1 confidence per recommendation (after trust)
        write_json(run_dir / "findings.json", {"stage": "final", "findings": dedupe_findings(findings), "parse_rejections": parse_rejections})
        write_json(run_dir / "verification.json", {"stage": "final", "verified": verified, "rejected": rejected})
        write_model_stats(run_id, all_parsed_records, rejected)

        update_run(run_dir, phase="synthesizing", progress=None)
        report = synthesize_report(prompt, tasks, verified, rejected, run_dir, config, intent, skipped_by_deadline)
        check_cancel()

        # Review + fact-check run AFTER synthesis, so they gate on the small post-synthesis reserve
        # (just enough to finish writing final.md), NOT the full synthesis reserve. Because these two
        # skips are decided after synthesize_report already consumed skipped_by_deadline, the promised
        # in-report degradation note can't cover them — so a deadline skip here ALSO prepends a visible
        # callout to the report string (mirrors the "⚠ FINAL CHECK" mechanism below).
        if config["review_legs"]:
            if stage_fits_budget(started, config, reserve_sec=POST_SYNTHESIS_RESERVE_SEC):
                update_run(run_dir, phase="reviewing", progress=None)
                report = adversarial_review(prompt, report, verified, rejected, run_dir, config)
            else:
                record_deadline_skip("adversarial_review")
                report = ("> ⚠ NOT REVIEWED: the time budget ran out before the adversarial review "
                          "could run — treat these findings as not independently reviewed.\n\n" + report)

        # Final adversarial fact-check of the top pick — re-confirm the single most important
        # claim before presenting it; a failure becomes a warning callout at the top of the report.
        if config.get("final_factcheck") and verified:
            if stage_fits_budget(started, config, reserve_sec=POST_SYNTHESIS_RESERVE_SEC):
                update_run(run_dir, phase="factchecking", progress=None)
                fc = factcheck_top_pick(prompt, verified, run_dir, config, intent)
                update_run(run_dir, final_check=fc)
                if fc and fc.get("ok") is False:
                    report = (f"> ⚠ FINAL CHECK: the top recommendation could not be re-confirmed "
                              f"({fc['reason']}). Re-verify it yourself before buying.\n\n" + report)
            else:
                record_deadline_skip("final_factcheck")
                report = ("> ⚠ NOT FACT-CHECKED: the time budget ran out before the final fact-check "
                          "of the top pick — re-verify it yourself before buying.\n\n" + report)

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
        # A crash in a LATE phase (synthesis/review/factcheck) must not throw away the whole run's
        # paid work. Salvage whatever survived verification into a partial report, exactly like the
        # cancel path — but still record status=failed + traceback so the bug stays visible.
        update_run(run_dir, status="failed", phase="failed", error=str(exc), traceback=traceback.format_exc())
        try:
            if verified or rejected:
                report = fallback_report(prompt, verified, rejected, disabled_legs(run_id))
                report = (f"> ⚠ This run hit an error during finalization and could not produce a "
                          f"fully synthesized report ({exc}). The verified results below are partial "
                          f"but were collected before the failure.\n\n" + report)
            else:
                report = f"# Research failed\n\n{exc}\n"
            (run_dir / "final.md").write_text(report, encoding="utf-8")
            update_run(
                run_dir,
                verified_count=len(verified),
                rejected_count=len(rejected),
                final_path=str((run_dir / "final.md").relative_to(ROOT)),
            )
        except Exception:
            (run_dir / "final.md").write_text(f"# Research failed\n\n{exc}\n", encoding="utf-8")
    finally:
        # Both auditors are fast relative to the phases they overlap, so by now they have almost
        # always finished; shut their pools down (non-blocking) so no stray thread outlives the run.
        if audit_executor is not None:
            audit_executor.shutdown(wait=False)
        if gap_executor is not None:
            gap_executor.shutdown(wait=False)
        ACTIVE_RUNS.discard(run_id)
        clear_cancel(run_id)
        clear_clarification(run_id)
        clear_user_disabled(run_id)
        clear_run_gemini_model(run_id)
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

    for row in iter_jsonl_rows(MODEL_STATS):
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

    today = daily_call_counts()
    weak_today: dict[str, int] = {}
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    for row in iter_jsonl_rows(SERVED_MODELS):
        if str(row.get("ts", "")).startswith(day) and (row.get("weak_tier") or str(row.get("served", "")).upper() in {"QUOTA_EXHAUSTED", "FAILED"}):
            leg = row.get("leg")
            if leg:
                weak_today[leg] = weak_today.get(leg, 0) + 1

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


def scoreboard_history(days: int = 14, today: str | None = None) -> dict:
    """Per UTC-day x per-leg health history for the scoreboard (read-only, no side effects).
    Buckets model-stats.jsonl (quality rows: calls / success / latency) and served-models.jsonl
    (served-call counts + weak/quota events) by UTC day, returning the last `days` days up to
    `today` (defaults to now UTC), oldest-first and DENSE (one point per day per leg, zero-filled).
    Bounded scan: each append-only file is read once and filtered to the date window."""
    days = max(1, days)
    today = today or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    end = dt.datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    day_list = [(end - dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    window = set(day_list)

    agg: dict[tuple[str, str], dict] = {}

    def slot(day: str, leg: str) -> dict:
        return agg.setdefault((day, leg), {
            "calls": 0, "success": 0, "latency_sum": 0.0, "latency_n": 0,
            "served_calls": 0, "weak_or_quota": 0,
        })

    for row in iter_jsonl_rows(MODEL_STATS):
        day = str(row.get("ts", ""))[:10]
        leg = row.get("leg")
        if day not in window or not leg:
            continue
        s = slot(day, leg)
        s["calls"] += 1
        s["success"] += 1 if row.get("success") else 0
        lat = row.get("latency_sec")
        if isinstance(lat, (int, float)):
            s["latency_sum"] += lat
            s["latency_n"] += 1

    for row in iter_jsonl_rows(SERVED_MODELS):
        day = str(row.get("ts", ""))[:10]
        leg = row.get("leg")
        if day not in window or not leg:
            continue
        s = slot(day, leg)
        s["served_calls"] += 1
        if row.get("weak_tier") or str(row.get("served", "")).upper() in {"QUOTA_EXHAUSTED", "FAILED"}:
            s["weak_or_quota"] += 1

    series = []
    for leg in sorted({leg for (_, leg) in agg}):
        points = []
        for day in day_list:
            s = agg.get((day, leg))
            if s:
                calls = s["calls"]
                points.append({
                    "day": day,
                    "calls": calls,
                    "success_rate": round(s["success"] / calls, 3) if calls else None,
                    "avg_latency_sec": round(s["latency_sum"] / s["latency_n"], 1) if s["latency_n"] else None,
                    "served_calls": s["served_calls"],
                    "weak_or_quota": s["weak_or_quota"],
                })
            else:
                points.append({
                    "day": day, "calls": 0, "success_rate": None, "avg_latency_sec": None,
                    "served_calls": 0, "weak_or_quota": 0,
                })
        series.append({"leg": leg, "points": points})
    return {"days": day_list, "legs": series, "generated_at": utc_now()}


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

        if path == "/api/scoreboard/history":
            try:
                days = int(urllib.parse.parse_qs(parsed.query).get("days", ["14"])[0])
            except (ValueError, TypeError):
                days = 14
            self.send_json(200, scoreboard_history(max(1, min(90, days))))
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
                        raw = f.read()
                    # Consume only up to the last newline so a concurrently-appended (still partial)
                    # line is never split into two invalid-JSON frames the client would drop.
                    nl = raw.rfind(b"\n")
                    if nl != -1:
                        chunk = raw[: nl + 1]
                        pos += nl + 1
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

        match = re.fullmatch(r"/api/runs/([^/]+)/clarify", parsed.path)
        if match:
            run_id = Path(urllib.parse.unquote(match.group(1))).name
            run_dir = RUNS_DIR / run_id
            if not run_dir.exists():
                self.send_json(404, {"error": "run_not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (json.JSONDecodeError, ValueError):
                self.send_json(400, {"error": "invalid_json"})
                return
            entry = submit_clarification(run_dir, run_id, body)
            self.send_json(202, {"status": "clarify_received", "skip": entry["skip"]})
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
            config = make_config(payload.get("effort"), payload.get("sites"), payload.get("disabled"),
                                 vendor_tiers=payload.get("vendor_tiers"),
                                 excluded_sites=payload.get("excluded_sites"),
                                 interactive=payload.get("interactive"))
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
    parser.add_argument(
        "--exclude-site",
        action="append",
        default=None,
        help="Block a whole domain from results, the inverse of --site (repeatable or "
             "comma-separated). An explicit --site wins over --exclude-site on the same domain.",
    )
    parser.add_argument("--codex-tier", default=None, choices=CODEX_EFFORTS,
                        help="Codex reasoning effort for every role it plays (default: xhigh).")
    parser.add_argument("--gemini-tier", default=None, choices=tuple(GEMINI_TIERS),
                        help="Gemini tier for every role it plays (default: high).")
    parser.add_argument("--claude-tier", default=None, choices=CLAUDE_TIERS,
                        help="Claude tier for every role it plays (default: opus).")
    parser.add_argument("--ask", action="store_true",
                        help="Interactive: if the request is ambiguous, pause once to ask a "
                             "clarifying question (waits up to RESEARCH_CLARIFY_TIMEOUT_SEC, then "
                             "proceeds on the default reading). Off by default so scripts never block.")
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

    cli_tiers = {"codex": args.codex_tier, "gemini": args.gemini_tier, "claude": args.claude_tier}
    config = make_config(args.effort, ",".join(args.site) if args.site else None,
                         ",".join(args.disable) if args.disable else None,
                         vendor_tiers={k: v for k, v in cli_tiers.items() if v},
                         excluded_sites=",".join(args.exclude_site) if args.exclude_site else None,
                         interactive=args.ask)
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
