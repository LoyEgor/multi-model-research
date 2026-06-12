# UI v2 brief — frontend redesign (parallel work track)

Audience: the agent working in Cursor on branch `ui-v2`. Scope: **only** files under `ui/`
(plus this doc if you need to log decisions). Do NOT touch `research.py`, `lib/`, `tests/` —
the backend is being developed in parallel; the API contract below is the interface.

## What this app is

A local multi-model research tool: the user types a request ("find the cheapest X"), picks an
effort level, optionally restricts to a site; three LLM vendors (GPT, Gemini, Claude) search in
parallel, cross-check each other, code verifies every claim against live pages, and a final
report is produced. The UI is served by a local Python server at `http://127.0.0.1:8765`.

## Deliverable

A redesigned `ui/index.html` — still a SINGLE static file (inline CSS/JS), no build step, no
external CDNs (the tool must work offline). Vanilla JS. It is served from disk per request, so
editing the file + refreshing the browser is the whole dev loop.

## Design requirements (owner's vision)

1. **Report first.** The model's final report is the hero of the page — rendered rich (the
   backend serves Markdown; render it client-side with a small embedded MD renderer you write —
   headings, bold, lists, links; sanitize: escape all raw HTML in the source, never inject it).
   Everything technical (verified/rejected cards, model-call progress, leg health, run config)
   collapses into expandable sections BELOW the report, collapsed by default for finished runs.
2. **Importance tiers.** Structure the rendered report visually: the first block (best pick) is
   large and prominent; alternatives are medium cards; cautions/unverified — visible but
   secondary; technical notes — small print inside collapsibles. Parse tiers from the report's
   heading structure (first section = hero, then per-section). Degrade gracefully when the
   structure is unexpected: just render the markdown.
3. **Effort switcher in the style of Claude's model picker**: a large segmented control with 4
   options (Quick / Standard / Deep / Max), each with a one-line description under the name,
   clear selected state. Not a bare range slider.
4. **Status while running**: phase timeline (decompose → search → verify → rescue → adjudicate →
   synthesize → review), live model-call counter (`run.progress.done/total`), a red badge when
   `run.leg_health[leg].disabled` or `run.degraded_legs` is set ("model down: gemini").
   Poll `GET /api/runs/<id>` every 2 s while `run.status == "running"`.
5. **Run history** as a left sidebar (or top drawer on mobile): clickable past runs with status,
   verified count, prompt preview. Current run highlighted.
6. Language of UI chrome: English. Reports arrive in the user's language — render as-is.
7. Look: clean, modern, calm; light theme; system font stack is fine; no frameworks.

## API contract (do not change; the backend guarantees this)

- `GET /api/runs` → `{"runs": [{run_id, status, phase, prompt, config, created_at,
  verified_count, rejected_count}]}` — newest first.
- `POST /api/runs` body `{"prompt": str, "effort": 1..4, "sites": "olx.ua, prom.ua"}` →
  `202 {"run_id": ...}`.
- `GET /api/runs/<run_id>` → `{"run": {...}, "tasks": [...], "verified": [...],
  "rejected": [...], "final_url": "/api/runs/<id>/final.md" | null}`.
  Notable `run` fields: `status` (queued|running|completed|failed), `phase`,
  `progress: {done, total} | null`, `config: {effort, effort_level, sites, ...}`,
  `leg_health`, `degraded_legs`, `recheck_dropped`, `verified_count`, `rejected_count`,
  `disputed_count`, `error`.
- `GET /api/runs/<run_id>/final.md` → the report, `text/markdown`.
- Notable item fields (verified/rejected arrays): `title, price, currency, url, marketplace,
  availability, condition, seller, location, evidence, confidence, source_models, disputed,
  price_candidates, adjudication, price_corrected_from, live_check, reasons (rejected only)`.

## Mock mode (so you can develop without the backend)

`ui/fixtures/` contains real captured payloads: `runs-list.json`, `run-completed.json`,
`run-running.json`, `final.md`. Implement a fallback: if `GET /api/runs` fails (no backend),
load fixtures via relative fetch (`fixtures/...`) and show a "mock data" badge. Serve the dir
with any static server (`python3 -m http.server` inside `ui/`). The fallback must not interfere
with real API operation.

## Acceptance checklist

- [ ] Single `ui/index.html`, no CDNs, works offline, valid against both fixtures.
- [ ] Report-first layout with importance tiers; technical sections collapsed by default.
- [ ] Claude-style segmented effort control wired to `POST /api/runs` (`effort: 1..4`).
- [ ] Site restriction input passes `sites`; run history sidebar; polling while running.
- [ ] Degradation badge, progress counter, disputed/adjudication/price-corrected markers on
      item cards (see fixture fields).
- [ ] Markdown renderer escapes raw HTML (no injection of model output into the DOM).
- [ ] Graceful states: empty history, failed run (`run.error`), missing report.
