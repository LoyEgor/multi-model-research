# HANDOFF — state, audit findings, and agreed plan (updated 2026-06-11, evening)

For the next model continuing this project. Read `README.md` FIRST — it holds the goal, the
token-economy rule (Opus thin, Gemini/GPT do heavy work), verified subscription/CLI facts, and
smoke tests. This file adds the current implementation state and what remains of the
owner-approved plan. The owner communicates in Russian; everything written into the repo
stays in English.

## Where the project stands

`research.py` (single file, stdlib-only) is a working MVP v1.5:
decompose (Codex) → parallel search (Codex + Gemini per task, per-leg query templates) →
URL + site verification → listing-ID dedupe + cross-leg price-dispute detection → recheck
rounds (other leg) → Claude adjudication of disputes (effort >= 3) → synthesis (Codex) →
cross-vendor adversarial review + revision (effort >= 3) → `final.md`.

Three legs, all subscription-only, all callable from ANY environment (no Claude Code session
needed): `lib/ask_codex.sh` (gpt-5.5), `lib/ask_gemini.sh` (gemini-3.1-pro-preview),
`lib/ask_claude.sh` (opus alias; NEW 2026-06-11). Claude is THIN by design: adjudicator and
adversarial reviewer only, never heavy search.

Effort system (1-4 / quick / standard / deep / max) scales task count, recheck rounds/items,
review legs, and codex reasoning effort — see the README table. Site restriction (`--site` /
UI field) is enforced three ways: decompose constraint, per-leg query shaping, `off_site`
rejection in verification. Both knobs live in the Web UI (slider + input) and in `run.json`
under `config`.

Tests: `python3 -m unittest discover tests` — 28 tests, all passing (utilities, parsers for
gemini/claude served-model extraction, dedupe/dispute/off-site/effort/stale logic, circuit
breaker, straggler drop, rescue leg rotation). Consensus paths (adjudication
accept/reject/leg-down, review-revise loop) were smoke-tested with mocked `call_model`.

Resilience layer (2026-06-12, owner requirement "поиск должен выживать"):
- Per-run CIRCUIT BREAKER: 3 consecutive call failures disable a leg for the rest of the run
  (instant short-circuit instead of serial timeouts); `run.json.leg_health` + UI pill + report
  warning expose the degradation. Verified live: the effort-4 benchmark run survived BOTH
  providers' quota collapse and completed with a warned, degraded report.
- STRAGGLER DROP: per-phase timeouts from the effort profile (search 420-900 s, recheck
  300-600 s) + quorum rule — once 75% of a phase's parallel calls return, the rest get
  max(profile grace, 0.5x median latency) and are then killed; their items flow into rescue.
  Slow-but-alive transports (agy after Gemini quota exhaustion) no longer hold a run hostage.
- JUDGE FALLBACK CHAIN: synthesis tries codex -> claude -> gemini (each once, past the breaker)
  before surrendering to the unranked fallback list.
- run.json writer race FIXED (unique tmp per writer + RUN_JSON_LOCK) — concurrent heartbeat +
  breaker updates crashed the first effort-4 run.
- ask_claude.sh: removed nonexistent `MultiEdit` from --disallowedTools (CLI rejected calls).

GOOGLE LEG MIGRATED TO agy-ONLY (owner decision 2026-06-12, evening). The legacy Gemini CLI
transport (and its tests) is RETIRED early — the CLI dies ~2026-06-18 anyway; the old wrapper
is preserved at lib/legacy/ask_gemini_cli.sh. ask_gemini.sh is now a clean agy wrapper:
headless `--print`, per-call model pin (AGY_MODEL="Gemini 3.1 Pro (High)" -> in-family
fallback AGY_MODEL_FALLBACK="Gemini 3.1 Pro (Low)"), weak tiers refused, quota -> exit 5,
~8-15 s per simple call. Facts that matter: the interactive /model command just writes the
label into ~/.gemini/antigravity-cli/settings.json (our --model overrides per call); the
interactive quota panel has NO public CLI/API — quota state is only observable per-call via
error signals; agy does NOT report which model served — audit rows record the pin, unverified.
Idea parked: agy also serves Claude Opus/Sonnet and GPT-OSS — potential extra capacity for the
Claude arbiter seat when the Anthropic pool is exhausted (happened in the max benchmark run).

Quota protection (2026-06-12, after the benchmark showed quotas — not effort design — limit
quality): Gemini CLI hangs (not fails) on pro-quota exhaustion → the wrapper hard-bounds the
CLI call (GEMINI_CLI_TIMEOUT=240) and maps quota signals to exit 5; GEMINI_FAIL_FAST=1 (set by
the orchestrator for search calls) skips the retry and the slow agy fallback; ask_codex.sh maps
"hit your usage limit" to exit 5 as well; research.py: exit 5 -> force_disable_leg immediately
(no 3 strikes), a process-wide gemini concurrency semaphore (RESEARCH_GEMINI_CONCURRENCY=2,
parallel hammering is what kills the quota), and a per-run gemini call budget in the effort
profile (gemini_call_budget 6/9/12/16) so deep runs cannot burn the whole day's quota.

Effort benchmark (bench/REPORT.md): effort 1 and 2 matched the verified gold answer exactly
(top pick 20 500 with per-item disqualification reasons for everything cheaper); effort 3-4 ran
through provider quota exhaustion windows — resilience proven, marginal quality of deep/max
unmeasured; re-run them on a quota-healthy day. Provider quotas, not effort design, were the
limiting factor; quota-aware pacing is the next optimization.

Not a git repo. Owner controls all git — do NOT `git init`/commit/push without explicit instruction.

## Done (originally audit findings 1-8, plus owner feature requests)

1. ~~Gemini false weak-tier degrade~~ — FIXED (wrapper reads `stats.models[*].roles.main`).
   Verified live: runs now take ~6 min instead of 23.
2. ~~Same-listing dedup misses~~ — FIXED: canonical listing IDs for OLX (`-ID...html`),
   Prom (`/pNNN-`), Rozetka (`/pNNN/`) in `dedupe_key`; language prefixes can't split items.
4. ~~Codex empty on `site:` queries~~ — FIXED: `shape_query_for_leg` strips operators for
   codex (plain-language domain instruction instead), appends `site:` for gemini when a site
   restriction is active. Decompose prompt now forbids operators in queries.
5. **Partially done:** consensus — cross-leg price-dispute detection (>10% spread on the same
   canonical item → `disputed`, never averaged), recheck by the OTHER leg, Claude adjudication
   (resolve by verified fact / reject), adversarial review of the final draft by Gemini (and
   Claude at effort 4) with Codex revision rounds. NOT yet: iterate-until-convergence budget
   beyond one revision per reviewer.
5b. **Rescue mechanism (owner requirement 2026-06-11): failed verification ≠ rejection.**
   Items with broken/moved URLs, timeouts, or missing prices are re-hunted across rescue
   rounds by legs that have not tried them yet (`attempts` map in `run_rechecks`; both legs
   per item at effort 4); candidates skipped by the per-round cap are counted in
   `run.json.recheck_dropped`; whatever stays unverified lands in the report's
   "Unverified — check manually" section (see `is_rescuable` / `NON_RESCUABLE_REASONS`).
   Only disproven/policy reasons are final: out_of_stock, off_site, parse_failed,
   adjudicated_reject. The owner explicitly wants hard-to-find items SURFACED, not dropped.
7. **Partially done:** UI — effort slider, site field, run history, progress heartbeat
   (model-call counter in `run.json.progress`), disputed badges, arbiter notes. Still polling
   (2 s), still stdlib server. No per-call live cards, no raw-log viewer, no cancel.
8. Stale-"running" runs are now detected and marked failed (no heartbeat ~25 min,
   `ACTIVE_RUNS` guard for in-process runs).

NEW since the audit: `lib/ask_claude.sh` — headless subscription Claude leg with the same
guard pattern as the others. Gotcha discovered: `claude -p --output-format json` lists
auxiliary models (haiku) in `.modelUsage` next to the main one — same poisoning trap as
Gemini's flash stats; the wrapper resolves it by preferring the requested-alias match, else
the dominant model by outputTokens. Tests cover this.

Claude seat tiering (owner decision 2026-06-11): Sonnet at effort 1-2, Opus at effort 3-4,
adjudication active at EVERY level. **Opus is the hard cost ceiling** — the wrapper refuses
fable/mythos-class models (exit 4, no override): a Mythos-tier call costs far more than the
whole pipeline saves.

Synthesis is intent-fit, not cheapest-first: the top option must be a working,
honestly-described item from a credible seller; every CHEAPER option must get a one-line
reason why it lost (damaged/for-parts/keyword-bait/scam signals/dead link/category page).
Findings carry `condition` and `seller` fields for this. No cap on listed options.

`bench/` holds the effort benchmark: `gold.json` (human-grade verified reference for the
MacBook/OLX query + a ledger of cheaper-but-disqualified traps), `check_listing.py` (live
page checker — OLX ad status from __PRERENDERED_STATE__, price extraction; the seed of
Stage 2 adapters), `run-benchmark.sh` (effort 1-4 sequentially), `eval.py` (metrics vs gold).
Trap discoveries worth knowing: Gemini can fabricate ALL listing URLs in a single-call "top
10" (sequential -ID1..-ID10, every one 404); an OLX listing can be repurposed by its seller —
URL slug says "macbook-air-m2", live page sells a Dyson straightener (HTTP 200, status
active) — only page-content vs claim comparison catches this (Stage 2 argument #1).

## Audit findings still OPEN

3. **Verification is shallow — PARTIALLY CLOSED 2026-06-12.** `apply_live_check` in research.py
   now fetches every verified listing page (URLs matching LISTING_ID_PATTERNS), reads the live
   price + ad status from the page state, REJECTS non-active ads (`listing_inactive`, final),
   and overrides the model-claimed price with the live one (`price_corrected_from` audit field;
   resolve-by-verified-fact). Proven on the real stale-price case from the benchmark
   (claimed 20500 -> live 30000). Still open: Prom/generic JSON-LD extractors (only the
   OLX-state + generic "price" regex works now), title-vs-claim comparison (the repurposed-ad
   Dyson trap), availability extraction for non-marketplace stores.
6. **Model stats collected but unused** (Stage 4): nothing reads `data/model-stats.jsonl`
   for task/judge routing. Scoreboard + stats-driven assignment still to do.
8. (risk) **Gemini CLI subscription ends ~2026-06-18** — after that the leg falls to `agy`
   (slow, tier-unverifiable). The Claude leg now exists as the third arbiter/insurance.

## What is good and must be PRESERVED

- The three wrappers in `lib/` — battle-tested transports, subscription-only, read-only,
  tier guards, audit logging to `data/served-models.jsonl`. Extend, don't rewrite.
- Run-artifact layout under `runs/<id>/` (raw/, findings.json, verification.json, final.md).
- Code-verifies-claims principle (HTTP checks, rejection rules, dispute-by-fact) — extend it,
  never replace with model self-assessment.
- Effort NEVER downgrades model tiers — scaling only adds/removes breadth and layers.

## Remaining plan (owner-approved order, adjusted for what's done)

- **Stage 1 — structure + live UI.** Split `research.py` into a package
  (`legs / verify / consensus / store / web`); FastAPI + uvicorn + SSE (the only new
  dependency); per-run `events.jsonl` streamed to the UI; per-call live cards, raw-output
  viewer, cancel button, model scoreboard panel. Frontend stays one HTML file.
- **Stage 2 — real verification (core value).** Page-content adapters (OLX, Prom, generic
  JSON-LD): live price/availability vs claimed → `disputed` → existing recheck/adjudication.
- **Stage 3 — finish consensus.** Convergence budget (max rounds/calls) for the review loop;
  disagreement detection beyond price (availability, seller).
- **Stage 4 — stats-driven routing.** Scoreboard from `model-stats.jsonl` (empty-answer rate,
  rejection rate, parse failures, latency) in the UI; use it to assign tasks and judge seats.

## Operational notes for the next model

- Smoke tests (README "MANDATORY") — all three probes: codex, gemini, claude. All alive
  2026-06-11 evening (`data/served-models.jsonl` tail).
- Run: `python3 research.py [--effort 1-4|quick|standard|deep|max] [--site domain] "<prompt>"`.
  UI: `python3 research.py --serve --port 8765`. Tests: `python3 -m unittest discover tests`.
- `RESEARCH_MAX_TASKS` / `RESEARCH_MAX_RECHECK_ITEMS` env vars override the effort profile.
- Claude leg billing guard: the wrapper unsets `ANTHROPIC_API_KEY`; never add `--bare`.
  `claude -p` draws from a capped subscription credit pool — keep Claude seats thin.
