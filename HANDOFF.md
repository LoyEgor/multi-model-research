# HANDOFF — current state & next steps (updated 2026-07-05, resilience & latency)

For the next model continuing this project. Read `README.md` first (the stable spec: goal,
token-economy rule, verified CLI facts, smoke tests). Read `ROADMAP.md` for the agreed phase
plan. This file is the LIVING state doc: what is true right now, what is in flight, what is
open. The owner communicates in Russian; everything written into the repo stays in English.

## Where the project stands

`research.py` (single file, stdlib-only — no FastAPI, deliberately, so `start.command` runs with
zero install) is a working orchestrator:
decompose (Codex) → optional interactive clarify gate (ambiguous requests only) → parallel search
(Codex + Gemini per task, a concurrent plan auditor riding the same wave at effort 2-4) → URL +
live-page verification → listing-ID dedupe + cross-leg price-dispute detection → rescue rounds
(other leg, a concurrent gap auditor riding the rescue window at effort 2-4) → Claude adjudication
of disputes → synthesis (Codex) → cross-vendor adversarial review (effort ≥3) → `final.md`. Every
OPTIONAL stage past the first rescue round is gated by the effort profile's wall-clock time budget.

Three subscription legs, in the shared **llm-legs** git submodule at `lib/legs/`
(`ask_codex.sh` gpt-5.5, `ask_gemini.sh` Google via Antigravity `agy --print` pinned to
`Gemini 3.1 Pro (High)`, `ask_claude.sh` opus/sonnet). Clone with `--recurse-submodules`;
update the pin with `git -C lib/legs pull && git add lib/legs`. find-truth consumes the same
submodule. The retired multi-transport Gemini-CLI wrapper is a banner-marked fossil at
`lib/legacy/ask_gemini_cli.sh` (delete after the CLI EOL ~2026-06-18).

UI v2 shipped: report-first layout with importance tiers, segmented effort control, collapsible
panels, run-history sidebar with search, mock-fixture dev mode. Live backend (stdlib): per-run
`events.jsonl`, SSE at `GET /api/runs/<id>/events`, `POST /api/runs/<id>/cancel` (kills in-flight
calls, produces a partial report). UI is a single static file `ui/index.html` served from disk
(edit + refresh, no build).

Resilience: per-run circuit breaker (3 consecutive failures disable a leg; quota exit-5 disables
instantly), straggler drop (per-phase timeouts + 75% quorum kill), judge fallback chain
codex→claude→gemini, `RESEARCH_GEMINI_CONCURRENCY` semaphore (default 2) + per-effort gemini
call budget (6/9/12/16). Legs are spawned with `cwd` = a throwaway per-call scratch dir
(`runs/<id>/scratch/<record_id>`, auto-removed) so an agentic leg (agy) cannot litter the repo
root; the audit log is pinned to `data/` via `LLM_LEGS_DATA_DIR`.

Vendor on/off (per-run quota preservation): any one or two of the three vendors can be switched
off before a run (UI toggles / CLI `--disable gpt,gemini` / POST body `disabled`). The run never
breaks — `make_config` computes `enabled_legs`/`disabled_legs` and filters `search_legs`/
`review_legs`; role selectors (`judge_vendor`, `arbiter_vendor`, `judge_chain`,
`vendor_claude_model`) replace every hardcoded vendor, so the remaining model(s) cover search,
judge, arbiter, reviewer and synthesis. A per-run `USER_DISABLED` guard in `call_model`
(`skipped_by_user`) and an `enabled_legs` gate on the agy Claude reserve are the belt-and-
suspenders. Disabling all three is ignored (a run always keeps ≥1 vendor).

Improvement round 3 — search quality & latency (2026-07-04), backed by an external best-practices
pass (Anthropic multi-agent research system, OpenAI/Gemini Deep Research, STORM, reflective-
retrieval papers): (1) the per-effort `time_budget_sec` (1500/2400/4200/5100s) is now enforced —
`budget_remaining_sec`/`stage_fits_budget` gate every OPTIONAL stage (recheck rounds after the
first, coverage, frontier, adversarial review, final factcheck), skipping one with a
`stage_skipped_deadline` event once only `SYNTHESIS_RESERVE_SEC` remains, recorded in
`run.json.skipped_by_deadline` and surfaced as a report degradation note + UI timeline "skipped
(time budget)"; (2) a concurrent plan auditor (effort 2-4, `config.plan_audit`) rides the primary
search wave and may fold up to 2 extra tasks (`origin="plan_audit"`) into the same fan-out before
collection; (3) a concurrent semantic-gap auditor (effort 2-4, `config.gap_audit`) rides the
rescue window and surfaces up to 3 material uncovered angles, merged into the coverage round's
fan-out at effort 3-4 or firing one bounded mini-wave at effort 2; (4) an interactive clarify gate
— decompose also emits `intent.clarify_question`/`intent.alternatives`; interactive runs (UI always
sends `interactive: true`, CLI opt-in via `--ask`) pause in phase `clarify`, emit SSE
`clarify_pending`, and wait up to `RESEARCH_CLARIFY_TIMEOUT_SEC` (default 45s, excluded from the
time budget) for `POST /api/runs/<id>/clarify`; an answer triggers one re-decompose, timeout/skip
proceeds on the assumed default reading (stated near the top of the report). Deliberately NOT
done: sharing findings between parallel search legs mid-flight — research shows it collapses the
independent cross-check signal (sycophancy up to 85.5%); only coverage state (searched URLs) is
shared, via the existing "do not re-report these URLs" prompt block.

Improvement round 4 — resilience & latency (2026-07-05): (1) every verify/live-check fetch now
goes through one `http_fetch` helper with stable browser headers (`BROWSER_UA`/`BROWSER_HEADERS`,
deliberately NOT rotated — rotation reads as more bot-like at our volume) and bounded anti-bot
backoff: 429s and cloudflare-style bot-wall 403s get up to 2 retries (`Retry-After` honored, capped
at 15s; ~18s total sleep budget per URL), a 503 gets one retry, and 404/other 4xx-5xx pass straight
through unretried; a persistent bot-wall classifies as the new SOFT reason `bot_blocked`
(rescuable — lands in "Unverified — check manually", never treated as a disproof), and a run-scoped
`HostBlockRegistry` gives every URL on an already-blocked host a single polite attempt for the rest
of the run. `verify_url`/`live_listing_check`/`apply_live_check`/`verify_findings` all route through
it; the UI has a `bot_blocked` reason label. (2) `scoreboard_history(days=14)` buckets
`model-stats.jsonl` + `served-models.jsonl` by UTC day × leg (calls, success_rate, avg_latency_sec,
served_calls, weak_or_quota), served at `GET /api/scoreboard/history?days=N` (clamped 1-90); the UI
scoreboard panel gained per-leg 14-day sparklines (inline SVG calls polyline + a success-rate
colored dot row, hover titles) — still a health indicator only, not routing; this closes the
"scoreboard has no time-series view" open item from Phase 7. (3) streaming verification: a
run-scoped `UrlCheckCache` (thread-safe, single-flight per normalized URL, separate verify/live
namespaces) is shared across rounds; `collect_with_straggler_drop` gained an optional `on_record`
callback, and all four round runners (`run_primary_search`, `run_rechecks`, `run_coverage_round`,
`run_frontier_round`) now parse each completed record's findings as it lands and prefetch its URLs
on a small pool (`RESEARCH_MAX_PREFETCH_WORKERS`, default 4) sharing the same `HostBlockRegistry` —
so by the time the batch `verify_findings` pass runs, the cache is already warm. This removes the
serial URL-check tail that used to follow the slowest search call, without changing verification
semantics (tests assert identical output with and without a warm cache). (4) `lib/legs/ask_gemini.sh`
fix, **uncommitted in the llm-legs submodule**: measured live 2026-07-05 — when the Antigravity
individual quota is exhausted, `agy --print` silently returns rc 0 with EMPTY stdout+stderr (the
`RESOURCE_EXHAUSTED` 429 "Resets in Nh" error only reaches agy's internal log). The wrapper now
passes `--log-file` to a temp file and, on empty output, greps stderr+log with `QUOTA_RE` for an
instant exit 5 (quota — the orchestrator drops the leg) carrying the reset hint, with no fallback
attempt; plain-empty output still exits 1 after the chain. Self-contained stub test at
`lib/legs/tests/test_gemini_quota_detect.sh`. **Open action item:** this fix is not yet
committed/pushed to `LoyEgor/llm-legs` and the pin in this repo is not bumped. (5) Round 3 live
validation (2026-07-05, run concurrently with a gemini quota exhaustion mid-window and codex
user-disabled): plan_audit confirmed live (ran alongside primary search, verdict "gaps", added 2
tasks — flagged Russian-only queries, added Ukrainian phrasing + a Facebook Marketplace angle);
clarify no-question path confirmed (`clarify_resolved` with `asked: false`); clarify ASK path
confirmed on a claude-only effort-1 run (a real Russian ambiguity question with 3 alternatives,
answered via `POST /api/runs/<id>/clarify`, triggering a re-decompose, all recorded in
`run.json.clarify`); resilience confirmed (gemini breaker tripped, claude-only continuation still
produced a report). Not yet live-validated: the gap_audit happy path (needs ≥1 healthy search-leg
pair) and the deferred `bench/harness.py` benchmark run — both blocked on the gemini quota reset
(~2026-07-09).

Live-page verification (minimal Stage 2): `apply_live_check` fetches verified marketplace
listings, rejects non-active ads (`listing_inactive`), and overrides the model-claimed price
with the live page price (`price_corrected_from`). Currently OLX-only (listing-ID patterns +
generic price regex) — Plati/Prom/JSON-LD adapters are the open Stage 2 work.

Tests: `python3 -m unittest discover tests` — 150 tests, all passing.

Git: public repos github.com/LoyEgor/{multi-model-research, llm-legs}; find-truth private. The
owner controls git — do NOT commit/push without explicit per-action instruction.

## Active plan

See `ROADMAP.md` (single source of truth for phases). In flight: Phase 2 (hygiene) →
Phase 7 (capacity), being executed sequentially. The owner approved the order 2026-06-13.

## Known-open / next work (mirrors ROADMAP, only genuinely-open items)

- **Search quality (Phase 4, the core):** relevance-vs-intent gate (wrong-product/wrong-tier
  items leak in — Plati run surfaced Perplexity/Cosverse and base-Pro masquerading as Max 5x);
  variant/tier-aware extraction; strictly-below-official ceiling + official-price anchor; USD
  normalization stage (one FX table; legs report native price+currency); per-host breadth cap +
  cluster coverage; Stage 2 adapter registry (Plati/Digiseller/GGSEL/FunPay + JSON-LD/OG +
  LLM-extractor fallback) replacing per-portal scrapers; snapshot-anchored benchmark harness.
- **Three-model parallelism (Phase 5) — DONE 2026-06-13:** Claude is an effort-gated search leg
  (config.search_legs adds claude at effort 3-4, sonnet, own budget + 1-wide concurrency; opus
  stays the judge seat). Latency now timed after the semaphore (queue_wait_sec recorded). Per-call
  cancel endpoint + UI ✕. UI per-leg swimlanes from SSE.
- **Domain generalization (Phase 6) — DONE 2026-06-13:** validated on a housing query — decompose
  picked real-estate portals on its own, intent auto-extracted housing exclusions, USD ranked
  mixed UAH/USD, report was optimal-first with reasons. The DOM.RIA-style HTTP 429 anti-bot on the
  live-check fetcher is closed by round 4's `http_fetch` backoff (see above) — not yet re-validated
  live against DOM.RIA specifically.
- **Capacity (Phase 7) — DONE 2026-06-13:** quota-aware pacing (daily_call_counts + per-run
  budget clamped to DAILY_CAPS remaining); model scoreboard (build_scoreboard → GET
  /api/scoreboard + UI panel; health indicator, NOT routing); Claude reserve via agy
  (call_agy_claude — last fallback for arbiter + synthesis judge when the Anthropic pool is
  exhausted; separate quota pool, audited as leg "claude-agy"). Open: DAILY_CAPS are static soft
  caps (no provider quota API). Scoreboard time-series — DONE round 4 (`scoreboard_history`).
- **Gemini quota (llm-legs submodule fix pending commit):** the agy individual quota is exhausted
  until ~2026-07-09. The round-4 `ask_gemini.sh` fix for the silent-empty exhaustion trap (see
  above) lives uncommitted in the `lib/legs` submodule checkout — commit + push it to
  `LoyEgor/llm-legs`, then bump the pin here. The gap_audit happy-path live validation (needs ≥1
  healthy search-leg pair) and the deferred `bench/harness.py` benchmark run are both blocked on
  the same reset. Side note: agy's model list now also shows a "Gemini 3.5 Flash" family — worth
  checking for a 3.5 Pro tier once the quota resets.

## Trap ledger (why code-verifies-claims is non-negotiable)

- Gemini single-call "top 10" once fabricated 100% of its listing URLs (sequential -ID1..-ID10,
  all 404).
- An OLX listing was repurposed by its seller: slug said macbook, live page sold a Dyson
  straightener (HTTP 200, status active) — only page-content vs claim catches it.
- Both legs agreed on a stale cached price (20 500 vs live 30 000) — disputes stay silent when
  models herd; the live-page price is the only truth.
- OLX search is phrase-adjacency-sensitive — a single query is NOT exhaustive; union multiple
  query variants.

## Operational notes

- Run: `python3 research.py [--effort 1-4|quick|standard|deep|max] [--site domain] "<prompt>"`.
- UI: double-click `start.command`, or `python3 research.py --serve --port 8765`.
- Tests: `python3 -m unittest discover tests`. Smoke the legs: `lib/legs/ask_*.sh --probe`.
- `bench/` holds the effort-benchmark methodology + report; `data/*.jsonl` (audit + model stats)
  and `runs/` are gitignored, machine-local.
