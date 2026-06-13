# Roadmap (agreed with the owner 2026-06-12; order may be re-shuffled by real-usage feedback)

## Phase 1 — UI v2 (in progress, split into two tracks)
- **1b. Frontend redesign — DELEGATED** to a parallel agent (Cursor, branch `ui-v2`), spec in
  `docs/ui-v2-brief.md`: report-first layout with importance tiers, Claude-style segmented
  effort control, collapsible technical sections, run-history sidebar, mock fixtures in
  `ui/fixtures/`. UI is a single static file served from disk (`ui/index.html`).
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

## Phase 2 — daily use & pain collection (continuous)
- Owner runs real queries (not only marketplace ones); failures are captured as regression
  cases (`bench/cases/` planned + a "bad result" button in UI v2).

## Phase 3 — search depth (order set by Phase 2 findings)
- Frontier-bounded refinement: best-guess price bound -> swarm hunts strictly below it ->
  converges with per-candidate disqualification reasons.
- Query-variant expansion (the OLX phrase-adjacency lesson: single queries are not exhaustive).
- Live-page verification extension: Prom/JSON-LD adapters, title-vs-claim comparison (the
  repurposed-listing trap), availability extraction.
- Cross-round disqualification ledger.

## Phase 4 — capacity & resilience
- Claude arbiter reserve via agy (agy also serves Claude Opus/Sonnet — separate quota pool).
- Model scoreboard from `data/model-stats.jsonl` in the UI; stats-driven task/judge routing.
- Quota-aware pacing (spread provider quotas across the day).
- One-command repeatable benchmark with market-snapshot anchoring (see bench/REPORT.md
  methodology) — run after every search-affecting change.

## Phase 5 — domain generalization
- Non-marketplace test case (e.g. cheapest single-person house in a region), venue-discovery
  tuning, category-specific hints only if generic venue discovery proves insufficient.
