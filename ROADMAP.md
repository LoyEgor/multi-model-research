# Roadmap (agreed with the owner 2026-06-12; order may be re-shuffled by real-usage feedback)

## Phase 1 — UI v2 (DONE 2026-06-13)
- **1b. Frontend redesign — DONE** (Cursor agent on `ui-v2` per `docs/ui-v2-brief.md`, finished
  in-session): report-first layout with importance tiers, segmented effort control, collapsible
  technical panels, run-history sidebar with search, SSE-driven per-call activity, cancel,
  mock-fixture dev mode. Polish pass: single always-usable composer with compact/expanded
  states, uniform control spacing, four-size type scale, brighter/lower-contrast palette,
  degradation warnings render as callouts (never as the best pick), garbage runs filtered
  from history.
- **1a. Live backend — DONE 2026-06-12 (stdlib, no new deps):** per-run `events.jsonl`
  (lifecycle, per-call start/finish, breaker, stragglers), SSE at `GET /api/runs/<id>/events`,
  `POST /api/runs/<id>/cancel` (kills in-flight calls, produces a partial report), cancel
  button in the current UI. E2E-verified live (run cancelled mid-search; SSE frames captured).
  FastAPI migration deferred — stdlib SSE proved sufficient and keeps `start.command`
  dependency-free. After 1b lands, wire SSE into the new frontend.

## Phase 1.5 — shared legs library (DONE 2026-06-12)
- Vendor wrappers extracted to github.com/LoyEgor/llm-legs, consumed as submodule `lib/legs/`.
- find-truth migration to the same submodule — DELEGATED (brief in that repo); it also brings
  its dying Gemini-CLI leg onto agy before 2026-06-18.

## Phases 2–7 (owner-approved 2026-06-13, executed one phase at a time)

Reordered after a forensic audit of the Plati run + UI/UX review. All six phases shipped
2026-06-13. A final adversarial review workflow (4 dimensions × verify) ran over the accumulated
diff; 2 confirmed bugs were fixed (apply_live_check now always adopts the verified live price, not
only when it differs >10%; adjudication sorts by USD), 3 hardened (budget refund + scratch cleanup
on cancel-after-semaphore race; SSE hard-duration ceiling), 2 confirmed as intentional and
documented (codex has no concurrency cap by design; a finding with no extracted tier is not
hard-rejected — rescue philosophy). 48 unit tests pass.

### Phase 2 — Hygiene (clears the decks) — DONE 2026-06-13
- Spawn legs with `cwd` = throwaway per-call scratch dir so an agentic leg (agy) never writes to
  the repo root; pin the audit log to `data/` via `LLM_LEGS_DATA_DIR`. (agy's `--sandbox` is only
  terminal restrictions, NOT a write guard — cwd isolation is the real fix.)
- Delete the accumulated root scratch litter.
- Rewrite HANDOFF.md to current reality; banner the retired legacy wrapper; fix stale `lib/` refs.

### Phase 3 — UI/UX (frontend only; what the owner wants now) — DONE 2026-06-13
- One-line compact composer (input + effort `<select>` + collapse/search buttons; site only in
  the expanded view).
- Every result/content block full width.
- Stop the "jumping" — gate entrance animations to first mount / diff by id (no re-animate on the
  2 s poll).
- Consistent result order + shared sort across all result blocks.
- Move Cancel into the progress/timeline block, far right ("cancel this sequence").

### Phase 4 — Search quality / correctness (the core value) — DONE 2026-06-13
Shipped: intent spec from decompose (subject/exclude keywords, required tier, official price);
relevance gate (`off_intent`), tier gate (`wrong_tier`), below-official ceiling
(`not_below_official`) — all final (non-rescuable); USD normalization stage (FX table + per-item
`price_usd`, disputes & ranking now in USD); anti-monoculture `diversify()` + host-distribution /
zero-result-sites fed to synthesis; Stage 2 adapter chain (OLX state → JSON-LD → OpenGraph →
generic; listing keys for Plati/Digiseller/GGSEL/FunPay); domain-agnostic one-command benchmark
harness `bench/harness.py`. 40 unit tests pass.
Open within Phase 4 (deferred, not blockers): live variant-table NAVIGATION (we gate on the
model-reported tier, but don't yet parse a bundled page's variant table ourselves); LLM-extractor
fallback for unstructured pages with no JSON-LD/OG; a full live benchmark run has not been
executed yet (harness is ready — run it in Phase 6 validation).

(original Phase 4 plan kept below for reference)
- Relevance-vs-intent gate (reject wrong product / wrong tier — Perplexity/Cosverse/base-Pro).
- Variant/tier-aware extraction: the requested tier's price, not a masked base price.
- Strictly-below-official ceiling + official-price anchor.
- USD normalization stage: legs report native price+currency, one FX table converts (UAH may stay).
- Anti-monoculture: per-host cap + cross-cluster coverage; surface "no results from site X".
- Stage 2 adapter registry: Plati/Digiseller/GGSEL/FunPay listing keys + live-price, generic
  JSON-LD/OpenGraph, LLM-extractor fallback — replaces per-portal scrapers.
- Snapshot-anchored, one-command benchmark harness (the measuring stick for the above).

### Phase 5 — Three-model parallelism + legible execution — DONE 2026-06-13
Shipped: Claude as an effort-gated SEARCH leg (config.search_legs — codex+gemini at effort 1-2,
+claude at 3-4), searching on SONNET with its own per-run budget (claude_search_budget 6/10) and
a tight concurrency cap (RESEARCH_CLAUDE_CONCURRENCY=1); Opus stays the judge/arbiter seat.
Latency accounting fixed — call time is measured AFTER the concurrency semaphore is acquired and
queue-wait is recorded separately (queue_wait_sec in meta + call_finished events), killing the
"gemini looks slow/sequential" illusion. Per-call cancel: POST /api/runs/<id>/calls/<rec>/cancel
kills one in-flight call (its item flows to rescue) — the UI shows a ✕ on running calls. UI now
renders per-leg SWIMLANES from SSE events (each vendor a lane with live cells + queue-wait), so
parallel work reads as parallel. 43 unit tests pass; live effort-3 run validates 3-leg search.
Open (deferred): effort-scaled global concurrency (left at env default — global semaphore shared
across runs makes per-run scaling unsafe); absolute-time Gantt positioning (lanes use ordered
cells, not time-axis bars).
- Claude as an optional effort-gated search leg (sonnet for search, own budget) — three families
  cross-check from the start.
- Fix latency accounting (time after `semaphore.acquire`, record queue-wait separately);
  effort-scaled concurrency; overlap independent phases.
- Per-leg swimlane/Gantt timeline in the UI from SSE events; per-call cancel.

### Phase 6 — Domain generalization — DONE 2026-06-13
Validated on a real non-shopping query ("cheapest small house/dacha for one person in
Ivano-Frankivsk region", effort 2): decompose chose REAL-ESTATE venues on its own
(dom.ria.com, lun.ua, rieltor.ua, est.ua, m2bomber, flatfy.ua) — not goods marketplaces;
intent auto-extracted housing exclusions (rentals, apartments, land plots, commercial) and the
relevance gate fired (off_intent rejected 6); USD normalization handled mixed UAH/USD listings
and ranked them together ($4.5k–$28.8k); the report opened with the cheapest credible house and
explained why a cheaper candidate ranked lower. Tuning: generalized the search-prompt wording
(purchasable OR rentable; listing/offer URLs); fallback tasks were already venue-neutral.
Open (deferred): some protected portals (DOM.RIA) return HTTP 429 to the live-check fetcher
(anti-bot) → those live-checks fail and items fall to rescue; needs backoff / UA handling in the
Stage-2 adapter (not a logic bug).

(original Phase 6 plan kept below)
- A real non-shopping run (service / housing); tune venue-discovery so the system finds anything,
  not just goods. Category-specific hints only if generic discovery proves insufficient.

### Phase 7 — Capacity & resilience — DONE 2026-06-13 (final phase)
Shipped: quota-aware pacing — daily_call_counts() reads served-models.jsonl, and each run's
per-leg budget is clamped to the remaining daily allowance (DAILY_CAPS, env-overridable), so one
run can't exhaust the day; surfaced in run.json.pacing. Model scoreboard — build_scoreboard()
aggregates model-stats.jsonl (success/parse-fail/no-sources rates, avg latency, rejections) +
today's calls + weak/quota events, served at GET /api/scoreboard and shown in a UI panel; it is a
HEALTH INDICATOR ONLY, explicitly not used for routing. Claude reserve via agy — call_agy_claude()
runs a Claude tier on the Antigravity pool (separate quota) as the last fallback for the arbiter
(adjudicate_disputes) and the synthesis judge chain when the native Anthropic pool is exhausted;
isolated scratch cwd + stdin guard like the other legs, audited as leg "claude-agy". 46 tests pass.
Open (deferred): scoreboard is read-only (no time-series/history view); DAILY_CAPS are static
soft caps, not provider-reported quotas (no public quota API exists for these CLIs).

(original Phase 7 plan kept below)
- Quota-aware pacing (spread provider quotas across the day).
- Model scoreboard from `data/model-stats.jsonl` as a health indicator (NOT routing — premature
  for 3 legs).
- Claude arbiter reserve via agy (separate quota pool; agy also serves Claude/GPT-OSS).

## Improvement round 2 — search-result quality (brainstorm 2026-06-13)

Owner-requested follow-up: raise result quality further. Executed one phase at a time.

### R2 Phase 1 — Content-truth gate — DONE 2026-06-13
content_mismatch(): when a verified listing's LIVE page title carries none of the intent's
subject keywords (yet the finding was indexed under them) or carries an exclude keyword, reject
it as `content_mismatch` (final, non-rescuable) — closes the repurposed-listing trap (a
"macbook-air-m2" slug whose live page sells a Dyson). Conservative (only fires with a real
fetched title + indexed-on-subject signal) to avoid false positives. UI labels added. 50 tests.

### R2 backlog (not started — await explicit go per phase)
### R2 Phase 2 — variant/tier table extraction — DONE 2026-06-13
extract_variants() parses a bundled multi-tier page into {tier: price+currency} (head stripped to
avoid title pollution; requires an adjacent currency marker so a bare "20x" digit isn't mistaken
for a price); live_listing_check exposes live_variants; apply_live_check(intent) picks the
REQUESTED tier's price (intent.required_tier or the finding's tier) over the listing-level/base
price — fixing 'cheap Pro masquerading as Max 5x'. Sets variant_corrected + price_corrected_from;
UI badge "tier from page". 52 tests. (A window-slice bug — windows cut from the original instead
of the head-stripped body — was caught by tests and fixed.)
### R2 Phase 3 — query-variant expansion — DONE 2026-06-13
The decompose brain now emits per-task `query_variants` (surface rephrasings: word order, synonyms,
latin↔cyrillic, SKU codes, year/spec reorderings) — beats search-engine phrase-adjacency (a
"macbook air m2" query misses "Apple MacBook Air 2022 M2"). run_primary_search expands each task
into up to N queries (effort-gated: 1/1/2/3 for quick/standard/deep/max via task_query_set) and
fans out leg × query; results union via the existing listing-ID dedupe. Leg budgets + straggler
drop bound the extra fan-out; low effort is unchanged (N=1). 55 tests.
### R2 Phase 4 — frontier-bounded refinement — DONE 2026-06-13
After rescue, the pipeline runs frontier rounds (effort-gated: 0/0/1/2 for quick/standard/deep/max):
credible_floor_usd() = the cheapest non-disputed USD price; build_frontier_prompt asks each search
leg for credible offers STRICTLY below it (same product/tier, working, honestly described);
run_frontier_round fans out the legs; results re-verify through all gates + listing-ID union. The
loop stops on a DRY round (new floor not below the ceiling) or when the round budget is spent.
Frontier calls consume the per-leg budget; UI timeline gained a Frontier phase. 58 tests.
### R2 Phase 5 — seller/source trust model — DONE 2026-06-13
seller_trust() turns the legs' seller/condition/evidence signals + price-vs-credible-floor into a
0..1 score with explainable signals (established account, has reviews, business seller; penalties
for far-below-market, damaged/for-parts, scam wording, disputed, no-seller-info). apply_trust()
attaches it to every verified finding before synthesis; the synthesis context is ordered by
trust_rank_key (trust tier, then USD) and the judge is told to rank by fit × trust × price (never
lead with a low-trust bait-priced item). UI shows a trust badge with the signals as tooltip.
60 tests.
### R2 Phase 6 — adversarial fact-check of the final top pick — DONE 2026-06-13
factcheck_top_pick() re-confirms the single most important claim right before presenting it
(effort-gated: deep/max). A FRESH code re-fetch decides active / price-drift(>10%) / content-
mismatch (authoritative), and an INDEPENDENT search leg re-reads the page to confirm
({confirmed,reason}); any failure prepends a "⚠ FINAL CHECK" warning callout to the report and is
recorded in run.json.final_check (also shown in Run details + a Fact-check phase in the timeline).
ok None = host has no listing adapter (can't re-verify). 62 tests.
### R2 Phase 7 — confidence calibration — DONE 2026-06-13 (R2 backlog complete)
calibrate_confidence() produces a 0..1 score + band (high/medium/low) + explainable factors per
recommendation, weighting cross-model agreement (0.25), live verification (0.30), seller trust
(0.30) and the model's own confidence (0.15); disputes and inactive listings tank it.
apply_confidence() runs after apply_trust; the synthesis judge is told to state the top pick's
confidence honestly and prefer a high-confidence lead; UI shows a confidence badge with factors.
64 tests.

## Vendor on/off — per-run quota preservation (DONE 2026-06-13)

Owner-requested: before any research, optionally switch OFF one or two of the three vendors
(codex/gpt, gemini, claude) to spare that provider's quota — and the run must NOT break; the
remaining vendor(s) take over every role (search, judge, arbiter, reviewer, reviser, synthesis).

Shipped:
- `make_config(effort, sites, disabled)` computes `enabled_legs`/`disabled_legs` (via
  `normalize_vendors` + `VENDOR_ALIASES`, so "gpt"/"openai"/"chatgpt"→codex, "google"→gemini,
  "anthropic"/"opus"/"sonnet"→claude); filters `search_legs`/`review_legs` to enabled, and never
  lets `search_legs` go empty (falls back to all enabled). Disabling all three is ignored
  (re-enables everything) — a run always has at least one vendor.
- Role selectors instead of hardcoded vendors: `judge_chain` (enabled subset of
  codex→claude→gemini), `judge_vendor` (first of the chain — decompose/revise brain),
  `arbiter_vendor` (first enabled of claude→codex→gemini — dispute adjudication),
  `vendor_claude_model` (Claude model only when the leg is claude, else None).
- Belt-and-suspenders runtime guard: per-run `USER_DISABLED` set
  (`set_user_disabled`/`user_disabled`/`clear_user_disabled`); `call_model` returns
  `skipped_by_user` for a disabled leg even if some path slips through. The Claude reserve
  (`call_agy_claude`) is gated on `"claude" in enabled_legs` at both call sites, so a claude-off
  run never touches the Anthropic/agy Claude pool.
- Surfaces: CLI `--disable codex,gemini`; POST `/api/runs` body `disabled: [...]`; run.json config
  carries `enabled_legs`/`disabled_legs`; UI composer has three vendor toggle buttons
  (GPT/Gemini/Claude) with a hint, a guard that blocks turning off the last enabled model
  ("At least one model must stay on"), prefs persistence, prefill from a re-opened run, and a
  "Models off" line in Run details.
- Tests: `test_vendor_disable_config` (role reassignment for each disable combination) +
  `test_user_disabled_blocks_call_model` (the runtime guard). 66 tests.
- Adversarial review (Explore agent) traced every call_model / call_agy_claude site against all
  1- and 2-vendor-disabled combinations: no path can call a disabled vendor, no empty-list crash.

## Improvement round 3 — search quality & latency (2026-07-04)

Owner-requested follow-up, backed by an external best-practices research pass (Anthropic
multi-agent research system, OpenAI/Gemini Deep Research, STORM, reflective-retrieval papers).

### R3.1 — Run time-budget enforcement — DONE 2026-07-04
The previously-dead `time_budget_sec` in `EFFORT_PROFILES` (1500/2400/4200/5100s for
quick/standard/deep/max) is now real. `budget_remaining_sec()`/`stage_fits_budget()` gate every
OPTIONAL stage — recheck rounds after the first, coverage rounds, frontier rounds, adversarial
review, final factcheck — and skip one with a `stage_skipped_deadline` event once only
`SYNTHESIS_RESERVE_SEC` (`RESEARCH_SYNTHESIS_RESERVE_SEC`, default 900s) remains, so the
always-run synthesis stays inside the budget; recorded in `run.json.skipped_by_deadline`,
surfaced in the report as a degradation note (never a warning callout) and in the UI timeline as
"skipped (time budget)". `clamp_round_timeout()` also shrinks each optional round's own phase
timeout to whatever budget remains. Effort levels now have predictable wall-clock ceilings instead
of an unbounded worst case.

### R3.2 — Concurrent plan auditor — DONE 2026-07-04
At effort 2-4 (`config.plan_audit`), right after decompose a cheap judge-vendor call
(`audit_plan()`, `build_plan_audit_prompt()`, codex-first, effort medium, task_type
`"plan_audit"`, budget-exempt) audits the task list for coverage/overlap/missing angles WHILE the
primary search wave is already running in its own executor. It may add up to 2 surgical extra
tasks (`origin="plan_audit"`, 1 query variant each), deduped against the existing queries via the
same `normalize_task` validation, submitted into the SAME executor before collection starts —
near-zero added wall-clock. Advisory: any failure yields no extra tasks and never faults the run.
Events `plan_audit_started`/`plan_audit_finished`; `run.json.plan_audit`; `tasks.json` rewritten
with the accepted extras.

### R3.3 — Semantic gap audit — DONE 2026-07-04
At effort 2-4 (`config.gap_audit`), while rescue rounds run, a second concurrent judge-vendor call
(`gap_audit()`, `build_gap_audit_prompt()`, task_type `"gap_audit"`) reviews the post-primary
verified findings digest (top-15 one-liners + host distribution + zero-result hosts) and names up
to 3 MATERIAL uncovered angles, each with a concrete search query (strict JSON, explicit
do-not-invent-gaps instruction), coerced via `coerce_gap_queries()` (cap 3, deduped against the
task queries). At effort 3-4 the gap queries merge into the EXISTING coverage round's fan-out (no
new serial phase); at effort 2, where coverage rounds are off, they trigger one bounded
budget-gated mini-wave (phase `gap_search`) only when gaps were actually found.

### R3.4 — Interactive clarify gate — DONE 2026-07-04
The decomposer now also emits `intent.clarify_question` (in the user's language) plus
case-preserving `intent.alternatives` (first entry = the assumed default) whenever
`intent.ambiguous`. For interactive runs (the UI always sends `interactive: true`; CLI opt-in via
the new `--ask` flag; the default stays non-blocking) `should_ask_clarify()` pauses the run in
phase `clarify`, emits SSE `clarify_pending {question, alternatives, timeout_sec}`, and
`wait_for_clarification()` waits up to `RESEARCH_CLARIFY_TIMEOUT_SEC` (default 45s) for
`POST /api/runs/<id>/clarify {"answer"|"skip"}`. An answer triggers ONE re-decompose with the
clarification appended; a timeout or skip proceeds on the assumed reading (recorded in
`run.json.clarify` and `intent.assumed`, stated near the top of the report per the synthesis
prompt's assumption-statement instruction). The waited time is excluded from the run's time
budget (`started` is pushed forward by the wait duration). UI: a clarify card with quick-answer
buttons, free text, skip, and a countdown; "No questions — research is running. You can step
away." when nothing was asked.

Also shipped this round (free prompt upgrades, no new code paths): Plan-and-Solve phrasing +
mutual-exclusivity task boundaries in the decompose prompt; "Do NOT re-report these URLs" lists
(`do_not_report_block()`, cap 20) added to the recheck and frontier prompts; the assumption-
statement instruction in the synthesis prompt (used by R3.4 above).

Deliberately NOT implemented (evidence-based): sharing findings between parallel search legs
mid-flight — research shows it destroys the independent cross-check signal (sycophancy up to
85.5%, oracle gap up to 32.3%; "The Cost of Consensus", arXiv 2605.00914). Only coverage state
(already-searched URLs, via `do_not_report_block()`) is shared between rounds. Streaming
per-item verification was deferred (a seconds-level win for high implementation complexity).

Tests: 101 → 129, all green.

## Improvement round 4 — resilience & latency (2026-07-05)

Follow-up focused on live-fetch resilience against anti-bot walls and cutting the serial
URL-verification tail off the end of a run.

### R4.1 — Anti-bot live-check fetching — DONE 2026-07-05
Every verify/live-check network call now goes through one `http_fetch` helper with stable browser
headers (`BROWSER_UA`/`BROWSER_HEADERS`, deliberately NOT rotated — at our request volume a
rotating UA reads as MORE bot-like) and bounded anti-bot backoff: 429s and cloudflare-style
bot-wall 403s (`_smells_like_bot_wall`) get up to 2 retries (`Retry-After` honored via
`_retry_after_seconds`, capped at 15s; ~18s total sleep budget per URL via `BOT_BLOCK_TOTAL_CAP`),
a 503 gets one retry, and 404/other 4xx-5xx pass straight through unretried. A persistent bot-wall
is the new SOFT reason `bot_blocked` — rescuable, lands in "Unverified — check manually", never a
disproof. A run-scoped `HostBlockRegistry` gives every subsequent URL on an already-blocked host a
single polite attempt (no retries) for the rest of the run. `verify_url`, `live_listing_check`,
`apply_live_check`, and `verify_findings` all route through `http_fetch`; the UI got a `bot_blocked`
reason label. Closes the DOM.RIA-style HTTP 429 open item noted in Phase 6.

### R4.2 — Scoreboard time-series — DONE 2026-07-05
`scoreboard_history(days=14)` buckets `model-stats.jsonl` + `served-models.jsonl` by UTC day × leg
(calls, success_rate, avg_latency_sec, served_calls, weak_or_quota), dense and zero-filled, served
at `GET /api/scoreboard/history?days=N` (clamped 1-90). The UI scoreboard panel gained per-leg
14-day sparklines (inline SVG: a calls polyline plus a success-rate colored dot row, with hover
titles). Still a health indicator only, never used for routing — closes the "scoreboard has no
time-series view" open item from Phase 7.

### R4.3 — Streaming verification (prefetch) — DONE 2026-07-05
A run-scoped `UrlCheckCache` (thread-safe, single-flight per normalized URL, separate `verify`/
`live` namespaces) is shared across all rounds. `collect_with_straggler_drop` gained an optional
`on_record` callback; `make_prefetch_collector` builds one that parses each just-completed record's
findings and submits their URLs (via `prefetch_url`) to a small pool (`RESEARCH_MAX_PREFETCH_WORKERS`
env, default 4, `MAX_PREFETCH_WORKERS`) sharing the same `HostBlockRegistry`. All four round runners
(`run_primary_search`, `run_rechecks`, `run_coverage_round`, `run_frontier_round`) are wired to it,
so the cache is already warm by the time the batch `verify_findings` pass runs — removing the
serial URL-check tail that used to follow the slowest search call, with no change to verification
semantics (tests assert identical `verify_findings` output with and without a warm cache).

### R4.4 — llm-legs: silent Gemini quota exhaustion (submodule) — DONE 2026-07-05
Measured live 2026-07-05: when the Antigravity individual quota is exhausted, `agy --print`
silently returns rc 0 with EMPTY stdout+stderr — the `RESOURCE_EXHAUSTED` (429) "Resets in Nh"
error only reaches agy's internal log, never stdout/stderr. `lib/legs/ask_gemini.sh` now passes
`--log-file` to a temp file and, on empty output, greps stderr+log against `QUOTA_RE` for an
instant exit 5 (quota — the orchestrator drops the leg) carrying the reset hint, with no fallback
attempt; plain-empty output still exits 1 after the existing fallback chain. Self-contained stub
test (stubs `agy`, no real model call) at `lib/legs/tests/test_gemini_quota_detect.sh`. **Not yet
committed/pushed to `LoyEgor/llm-legs`, and the submodule pin in this repo is not bumped** — see
HANDOFF Known-open.

### Validation — Round 3 live-checked 2026-07-05
Concurrent with a gemini quota exhaustion mid-window and codex user-disabled: plan_audit confirmed
live (rode the primary search wave, verdict "gaps", added 2 tasks — flagged Russian-only queries,
added Ukrainian phrasing + a Facebook Marketplace angle); clarify no-question path confirmed
(`clarify_resolved` with `asked: false`); clarify ASK path confirmed on a claude-only effort-1 run
(a real Russian ambiguity question with 3 alternatives, answered via `POST /api/runs/<id>/clarify`,
triggering one re-decompose, all recorded in `run.json.clarify`); resilience confirmed (gemini
circuit breaker tripped, claude-only continuation still produced a report). Not yet live-validated:
the gap_audit happy path (needs ≥1 healthy search-leg pair) and the deferred `bench/harness.py`
benchmark run — both blocked on the gemini quota reset (~2026-07-09).

Tests: 134 → 150, all green.
