# HANDOFF — current state & next steps (updated 2026-06-13, vendor on/off)

For the next model continuing this project. Read `README.md` first (the stable spec: goal,
token-economy rule, verified CLI facts, smoke tests). Read `ROADMAP.md` for the agreed phase
plan. This file is the LIVING state doc: what is true right now, what is in flight, what is
open. The owner communicates in Russian; everything written into the repo stays in English.

## Where the project stands

`research.py` (single file, stdlib-only — no FastAPI, deliberately, so `start.command` runs with
zero install) is a working orchestrator:
decompose (Codex) → parallel search (Codex + Gemini per task) → URL + live-page verification →
listing-ID dedupe + cross-leg price-dispute detection → rescue rounds (other leg) → Claude
adjudication of disputes → synthesis (Codex) → cross-vendor adversarial review (effort ≥3) →
`final.md`.

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

Live-page verification (minimal Stage 2): `apply_live_check` fetches verified marketplace
listings, rejects non-active ads (`listing_inactive`), and overrides the model-claimed price
with the live page price (`price_corrected_from`). Currently OLX-only (listing-ID patterns +
generic price regex) — Plati/Prom/JSON-LD adapters are the open Stage 2 work.

Tests: `python3 -m unittest discover tests` — 66 tests, all passing.

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
  mixed UAH/USD, report was optimal-first with reasons. Open: DOM.RIA-style HTTP 429 anti-bot on
  the live-check fetcher (Stage-2 adapter backoff/UA, not a logic bug).
- **Capacity (Phase 7) — DONE 2026-06-13:** quota-aware pacing (daily_call_counts + per-run
  budget clamped to DAILY_CAPS remaining); model scoreboard (build_scoreboard → GET
  /api/scoreboard + UI panel; health indicator, NOT routing); Claude reserve via agy
  (call_agy_claude — last fallback for arbiter + synthesis judge when the Anthropic pool is
  exhausted; separate quota pool, audited as leg "claude-agy"). Open: scoreboard has no
  time-series view; DAILY_CAPS are static soft caps (no provider quota API).

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
