# Effort benchmark report — 2026-06-11/12

## WINDOW 2 (2026-06-12 16:35-19:25 UTC, healthy-ish quotas, paired same-market comparison)

Same prompt/site, market snapshot taken at window start (`gold-candidates-v4-snapshot.json`).
Run order: deep -> max -> control quick. All on identical code (straggler drop + per-phase
timeouts + quota protection active).

| effort | wall min | calls | model min | verified | top pick (claimed) | live at eval | verdict |
|--------|----------|-------|-----------|----------|--------------------|--------------|---------|
| 1 quick (control) | **9.0** | 13 | 28.5 | 10 | 20 300 (risky Dnipro lot, caveats given) | price moved 18.3-20.3k | Gemini leg died at start -> codex-only; ranked answer + honest warning, but WITHOUT cross-vendor checks it picked the lot two-leg runs had disqualified (hidden defects found by Gemini's page read yesterday) |
| 3 deep | 17.4 | 33 | 59.4 | 20 | 20 500 (ID10EvHY) | **30 000 — STALE** | full pipeline, no degradation; both legs herded on a cached search-index price |
| 4 max | 39.9 | 44 | 146.1 | 15 | 22 100 (macon.sale) | **22 100 — CONFIRMED** | survived claude+gemini quota deaths mid-run; 2 disputes adjudicated; the only run whose top price matched the live page |

Window-2 conclusions:
1. Effort DOES buy reliability: max produced the only live-confirmed top pick; quick without its
   second leg missed defect knowledge and picked a risky lot. Depth = more cross-checking, and
   cross-checking is what catches traps.
2. The dominant error class at EVERY level was stale/unverified prices from search-index caches
   (models agree on the same wrong number — herding, disputes stay silent). FIXED post-benchmark:
   `apply_live_check` now fetches every verified marketplace listing page, rejects non-active ads
   (`listing_inactive`), and overrides the claimed price with the live page price
   (`price_corrected_from` keeps the audit trail). The exact 20 500 -> 30 000 case now corrects
   automatically (verified live).
3. Speed at equal effort vs window 1: quick 22.2 -> 9.0 min, deep 35 -> 17.4 min — straggler
   drop + per-phase timeouts + quota fail-fast pay off.
4. agy transport (v1.0.7) re-measured: direct `--print` with pinned "Gemini 3.1 Pro (High)"
   answers in ~8-15 s — promoted from emergency PTY fallback to transport #2 with its own quota
   pool; Gemini leg now survives CLI-quota death at full speed.

---

## WINDOW 1 (2026-06-11, first series)

Query: «найди самый дешевый MacBook Air M2 в Украине», `--site olx.ua`, same prompt at every
effort level, sequential runs. Gold reference: `bench/gold.json` v2 (multi-query union sweep of
the OLX index + page-by-page code verification; best credible pick at build time: 20 500 UAH).

## Results

| effort | wall min | model calls | model min | verified/rejected | top pick (at run time) | quality verdict |
|--------|----------|-------------|-----------|-------------------|------------------------|-----------------|
| 1 quick | 22.2 | 12 | 49 | 16/15 | **20 500 — gold match (gap 0)**, cheaper 18 850 explained (broken camera, screen bleed) | PASS — optimal answer |
| 2 standard | 40.4 | 16 | 112 | 15/11 | **20 500 — gold match (gap 0)** | PASS — optimal, slower (ran through Gemini quota hole) |
| 3 deep | 35.0 | 27 | 104 | 7/18 | 22 999, credible, intent-ranked, cheaper options explained | PARTIAL — sound answer, missed 20 500 (Gemini leg degraded all run) |
| 4 max | 55.4 | 38 | 188 | 8/33 | degraded fallback list with WARNING banner | RESILIENCE PASS / QUALITY N/A — both providers' quotas collapsed mid-run |

All four runs: top-5 report URLs 5/5 live, 5/5 direct listing pages, off-site = 0.

## What actually limited quality: provider quotas, not effort design

- Effort 1 ran on healthy quotas → optimal answer at minimal cost. The floor target
  ("level 1 must beat a Google search") is met: gold match + per-item disqualification reasons.
- Effort 2-3 ran inside a Gemini Pro quota exhaustion window (CLI mixes flash into responses —
  tier guard rejects them — then falls to the slow agy transport or hangs to timeout).
- Effort 4 (rerun 02:04 UTC) hit BOTH quotas: all 6 Gemini search calls hung to the 900 s
  timeout; Codex returned "You've hit your usage limit… try again at 8:13 AM" from recheck
  round 2 onward. The circuit breaker disabled both legs, the run completed with a fallback
  report and an explicit degradation warning instead of hanging or crashing.
- Conclusion: effort levels scale cost linearly (12 → 38 calls, 49 → 188 model-min), but the
  marginal quality of levels 3-4 could not be measured fairly — they ran on crippled providers.
  Re-run effort 3-4 on a quota-healthy day for the real comparison. What IS proven: under
  provider failure, higher effort burns quota faster for no gain — quota-aware pacing
  (rate limiting Gemini calls, spreading runs) matters more than more parallelism.

## Incidents the benchmark surfaced (all fixed during the series)

1. `run.json` writer race (fixed-name tmp file, concurrent heartbeat + breaker writers) crashed
   the first effort-4 run → unique tmp per writer + RUN_JSON_LOCK.
2. `ask_claude.sh` listed the no-longer-existing `MultiEdit` in --disallowedTools → CLI rejected
   review calls (rc=1, empty stdout) → removed; long-prompt repro passes.
3. Fallback report (no judge available) lists by raw price — a "на запчастини" parts listing
   topped the effort-4 degraded output → judge seat now falls back codex → claude → gemini
   before surrendering to the unranked list.
4. Single-query sweeps are non-exhaustive (OLX phrase-adjacency matching) — found via the
   owner's challenge; gold rebuilt as multi-query union. Same lesson encoded for the system:
   query-variant expansion is a planned optimizer.

## Marketplace volatility note

The gold anchor listing (ID10EvHY) changed price 20 500 → 30 000 within hours of gold v2.
"Gap vs gold" is only meaningful against run-time state; effort 1-2 verifiably matched gold at
run time (interim eval). Any future benchmark should snapshot the gold price at run start.

## Live-trap ledger (why code verification is non-negotiable)

- Gemini single-call "top 10" fabricated 100% of its listing URLs (sequential -ID1..-ID10, all 404).
- OLX listing repurposed by seller: slug "macbook-air-m2", live content = Dyson hair straightener,
  HTTP 200, ad status active. Only page-content vs claim comparison catches it (Stage 2 argument).
- Keyword-bait: M1 laptops titled "m2 m3" at attractive prices; admitted-defect items described
  as "ідеальний стан".
