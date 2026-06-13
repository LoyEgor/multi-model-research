# multi-model-research

A **local, subscription-powered, multi-model research / orchestration harness.**

> New model entering this project: read this whole file first. It defines the goal, what every file
> is and why it's here, the verified facts you can't otherwise know, and the goals (not prescriptions)
> for what to build. Verify before you trust — re-run the smoke tests at the bottom.
> Then read `HANDOFF.md` — the latest audit findings, stack decision, and the owner-approved plan.

## Goal

You give a research prompt. A thin "brain" model decomposes it into sub-tasks, distributes them
across **different model families in parallel** (Google Gemini, OpenAI GPT, Anthropic Claude/Opus),
has them **cross-check each other**, and synthesizes a verified answer.

Two first-class goals, equally important:
1. **Better research than any single model** — parallel breadth + genuine cross-vendor disagreement +
   fact-verification, so independent models catch each other's errors and hallucinations.
2. **Token economy.** Gemini and GPT are FREE here (subscription); Opus/Claude is rate-limited. Push
   ALL heavy lifting (web research, reading, drafting) onto Gemini/GPT and keep Opus a THIN
   orchestrator/judge. Improve quality AND cut Opus consumption at the same time.

Domain-agnostic: extracted from a finance project (see Provenance) but carries none of that logic.

## The one hard architectural rule (token economy)

**Opus = thin brain only (decompose tasks, judge, final synthesis). Gemini/GPT = all the heavy work.**
Do NOT wrap each cheap-vendor call inside an Opus subagent — that re-spends the exact Opus tokens you
are trying to save. Keep the control-flow cheap:
- cheapest: orchestrate the parallel `ask_gemini.sh` / `ask_codex.sh` calls with a plain bash/Python
  fan-out (background jobs), then let ONE model read the assembled results in a single pass;
- or run any "glue/transport" subagents on Haiku/Sonnet, reserving Opus for the final judge/synthesis.

## Files

The three vendor legs live in the shared **[llm-legs](https://github.com/LoyEgor/llm-legs)**
library, consumed as a git submodule at `lib/legs/` (clone this repo with
`git clone --recurse-submodules`, or run `git submodule update --init` after a plain clone).
One wrapper fix there propagates to every consumer project via a pin bump. Descriptions below
document the legs as used here:

- `lib/legs/ask_gemini.sh` — **Google** leg via the Antigravity CLI (`agy`), subscription-only.
  Headless `--print` with the model PINNED per call (`AGY_MODEL`, default
  `"Gemini 3.1 Pro (High)"`; in-family fallback `AGY_MODEL_FALLBACK`, default
  `"Gemini 3.1 Pro (Low)"`). Weak flash/lite tiers are refused (`GEMINI_ALLOW_WEAK=1`
  overrides). Quota exhaustion is detected per call and exits 5 (the orchestrator drops the
  leg instantly). agy does NOT report the served model — the audit row records the pin,
  unverified. ~8-15 s per simple call. Modes: `--probe` (no model call), `--list-models`.
  The retired multi-transport Gemini-CLI wrapper lives in `lib/legacy/ask_gemini_cli.sh`
  (the Gemini CLI dies ~2026-06-18; migrated early 2026-06-12).
- `lib/legs/ask_codex.sh` — **OpenAI GPT** (gpt-5.5) leg via Codex CLI. Subscription, read-only sandbox.
  Does NOT hardcode the model (auto-tracks the CLI flagship) but **guards the tier** — rejects weak
  tiers (exit 3). Reasoning effort pinned `xhigh` (`CODEX_EFFORT=medium` for light calls). Logs
  `{requested, served}`. Mode: `--probe`. Overrides: `CODEX_MODEL`, `CODEX_ALLOW_WEAK=1`.
- `lib/legs/ask_claude.sh` — **Anthropic Claude** leg. Subscription (OAuth), headless `claude -p
  --output-format json` — runs from ANY environment, no Claude Code session required. Defaults to
  the `opus` alias; guards the tier by parsing `.modelUsage` (auxiliary haiku entries do not poison
  the guard — the requested-alias match wins, else dominant-by-outputTokens). Unsets
  `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` (billing trap), disallows mutating tools,
  `--setting-sources project` so user-level prefs never leak into judgments. Modes: `--probe`,
  `--extract-served-model`. Overrides: `CLAUDE_MODEL` (sonnet/opus are the working tiers),
  `CLAUDE_ALLOW_WEAK=1`. **Cost ceiling: Opus** — Mythos-class models (fable/mythos) are refused
  outright, exit 4, no override (owner decision 2026-06-11). THIN roles only (token economy):
  dispute adjudication + adversarial review; never heavy search.
- `data/served-models.jsonl` — append-only audit of which model actually served each Gemini/GPT call
  (so you can PROVE pro vs flash). Created on first call.
- `.gitignore` — ignores `.secrets/`, `.venv/`, `data/cache/`, logs.

Calling a leg: `lib/legs/ask_codex.sh "<prompt>"` or `echo "<prompt>" | lib/legs/ask_gemini.sh` → prints the
model's text answer to stdout. A leg that cannot serve a strong model exits non-zero and logs
`weak_tier:1` — treat that leg as unavailable rather than trusting a cheap model.

## MVP v1 runner

`research.py` is the first dependency-free orchestration layer for offer research (products,
services, subscriptions, accounts).

CLI:
- `python3 research.py "найди самый дешевый MacBook Air M2 в Украине"`
- `python3 research.py --effort 3 --site olx.ua "<prompt>"` — deeper run restricted to one site
- `python3 research.py --list-runs`

Local Web UI — easiest way to use the system:
- **Double-click `start.command` in Finder** (or run `./start.command`) — it starts the server
  and opens the browser. Ctrl+C in its Terminal window stops it.
- Manual equivalent: `python3 research.py --serve --port 8765` → open `http://127.0.0.1:8765`.
- The form has a prompt box, an **effort slider (1-4)**, an optional **site restriction** field,
  a clickable **run history**, live progress (model-call counter), and a degradation pill when
  a model leg goes down mid-run.

Effort levels (`--effort`, slider, or names quick/standard/deep/max). Scaling adds breadth and
verification layers; it NEVER swaps in weaker model tiers:

| level | tasks | rescue rounds | max rescue items | legs per rescue | adversarial review | Claude seat (adjudication/review) | codex effort |
|-------|-------|---------------|------------------|-----------------|--------------------|-----------------------------------|--------------|
| 1 quick | 3 | 1 | 4 | 1 | – | Sonnet | medium |
| 2 standard (default) | 4 | 1 | 6 | 1 | – | Sonnet | medium search / xhigh judge |
| 3 deep | 5 | 2 | 8 | 1 (rotates) | Gemini | Opus | medium search / xhigh judge |
| 4 max | 6 | 2 | 12 | 2 (both) | Gemini + Opus | Opus | xhigh everywhere |

The Claude seat is tiered by cost: Sonnet at low effort, Opus at high effort. **Opus is the hard
cost ceiling** — `lib/legs/ask_claude.sh` refuses Mythos-class models (fable/mythos) outright, no
override. Dispute adjudication runs at EVERY level (it is cheap and only fires when models
disagree); review layers appear from level 3.

What v1 does:
- asks Codex to decompose the prompt into independent search tasks (count from effort);
- runs Codex and Gemini in parallel for the primary search, with per-leg query templates
  (Codex never sees `site:` operators — it returns empty findings on them; Gemini gets them
  appended when a site restriction is active);
- asks both legs for structured findings with `title`, `price`, `currency`, `url`,
  `marketplace`, `availability`, `location`, `shipping`, `evidence`, `confidence`,
  `source_model`, and `checked_at`;
- dedupes by marketplace listing ID (OLX `-ID...`, Prom `pNNN-`, Rozetka) so the same listing
  behind different language prefixes merges; flags cross-leg price disagreements >10% as
  `disputed` (kept, never averaged);
- verifies URLs with stdlib HTTP `HEAD` and `GET` fallback; with `--site` it also rejects any
  off-domain URL (`off_site`);
- **verifies marketplace listings against the LIVE page** (Stage 2 minimal): for URLs with a
  known listing-ID pattern the page itself is fetched — non-active ads are rejected
  (`listing_inactive`), and the live page price overrides the model's claim
  (`price_corrected_from` keeps the trail). Search-index caches feed models stale prices that
  BOTH vendors agree on — only the page is the truth;
- **never silently discards a failed-verification item** — a broken/moved URL, timeout, or
  missing price is a *failure to verify*, not a disproof. Such items get RESCUE rounds: a model
  that has not tried yet (other vendor first, then the originator; BOTH legs at effort 4) hunts
  for the same item's working URL / live price. Items the rescue budget skipped are counted in
  `run.json.recheck_dropped` (shown in the UI). Only disproven/policy rejections are final:
  `out_of_stock`, `off_site`, `parse_failed`, `adjudicated_reject`;
- items still unverified after rescue go into the final report's **"Unverified — check
  manually"** section with URLs and what to check — they may be exactly what the user wanted;
- Claude (thin arbiter: Sonnet at effort 1-2, Opus at 3-4) adjudicates surviving price disputes
  by evidence at every effort level;
- uses Codex as the final judge/synthesizer; at effort >= 3 the draft is adversarially reviewed
  by another vendor (Gemini, then Claude at effort 4) and revised by Codex on concrete issues;
- marks a "running" run with no heartbeat for ~25 min as failed (stale-run detection);
- streams a live event feed: every run appends to `runs/<id>/events.jsonl` (lifecycle, every
  model call start/finish, breaker trips, straggler kills), served as SSE at
  `GET /api/runs/<id>/events`; runs can be cancelled (`POST /api/runs/<id>/cancel` or the UI
  button) — in-flight calls are killed and a partial report is still produced;
- survives provider failures: a leg with 3 consecutive failed calls is disabled for the rest of
  the run (circuit breaker; shown in the UI and warned about in the report), slow stragglers are
  killed once 75% of a phase has returned (their items go to rescue), per-phase timeouts come
  from the effort profile, and the judge seat falls back codex → claude → gemini;
- protects the scarce Gemini quota: the CLI call is hard-bounded (`GEMINI_CLI_TIMEOUT`, default
  240 s — the CLI HANGS in internal retries when the pro quota is exhausted, it does not fail),
  quota errors exit with code 5 and instantly disable the leg for the run (same for Codex
  usage-limit errors), search calls fail fast instead of riding the 5-12 min agy fallback,
  at most `RESEARCH_GEMINI_CONCURRENCY` (default 2) gemini calls run at once, and each run has
  a gemini call budget from its effort profile (6/9/12/16) — primary search is funded first.

Run artifacts are saved under `runs/<timestamp-slug>/`:
- `run.json` — status and metadata;
- `tasks.json` — decomposed tasks;
- `raw/` — raw stdout/stderr/meta for each model call;
- `findings.json` — parsed and deduplicated findings;
- `verification.json` — verified and rejected items with reasons;
- `final.md` — final user-facing report.

Aggregated model quality rows are appended to `data/model-stats.jsonl`.

Useful local tuning knobs:
- `RESEARCH_MODEL_TIMEOUT_SEC=900` — per model-call timeout;
- `RESEARCH_URL_TIMEOUT_SEC=12` — per URL check timeout;
- `RESEARCH_MAX_TASKS` — overrides the effort profile's task count when set (capped 3-6);
- `RESEARCH_MAX_RECHECK_ITEMS` — overrides the effort profile's per-round recheck cap when set;
- `RESEARCH_MAX_PRIMARY_WORKERS=6` — primary fan-out concurrency.

v1 limitations:
- Claude is used only in THIN seats (adjudication, review) at effort >= 3 — by design, not a gap.
- There are no site-specific OLX / Prom.ua scrapers; models do web search, code verifies the
  returned links and facts.
- The Web UI is local-only and uses Python stdlib, not a production web framework.
- For Ukrainian shopping prompts, missing currency defaults to UAH when a price exists.

## Verified facts that don't travel via memory (carry-over knowledge)

Confirmed live in the source project (2026-06-07/09); re-verify here.
- Cross-vendor BY SUBSCRIPTION runs headless, no per-call API: Claude (`claude -p --output-format
  json`), Codex (`codex exec --sandbox read-only --skip-git-repo-check`, gpt-5.5), Gemini
  (`gemini -m pro --approval-mode plan -o json`).
- **Gemini tier trap:** the CLI silently defaults to a flash-lite tier — you MUST force `-m pro` and
  VERIFY the served model (the wrapper does both by reading `stats.models[*].roles.main`, not by
  grepping every model name in stats). The only Gemini-3 *pro* id on the consumer CLI is
  `gemini-3.1-pro-preview` (`gemini-3.1-pro` / `-latest` return 404).
- **Web access is real** (verified): all three browse/ground. Codex `web_search` is a hosted tool
  (works even in the read-only sandbox); Gemini uses Google Search grounding.
- **Auth/billing traps:** a stray `ANTHROPIC_API_KEY` silently switches Claude to API billing —
  `unset` it in any headless/cron run; never `claude --bare` (ignores the OAuth). The Codex global
  config may be `danger-full-access` — the wrapper always forces `--sandbox read-only`.
- **Dated risk:** Gemini CLI subscription access reportedly ends ~**2026-06-18** → migrate to
  Antigravity CLI (`agy`; the wrapper already falls through to it) or a free Gemini Flash API key.
  `claude -p` draws from a capped Agent-SDK credit pool — another reason to keep Claude thin.

## Orchestration patterns proven in the source project (reuse the SHAPE; you design the impl)

- **Blind independent fan-out** — ask each vendor without it seeing the others; independence is the
  whole point, don't let them converge prematurely.
- **Adjudicate disagreements, don't average them** — an independent step that finds WHERE they diverge
  and WHY, and which side rests on a verified fact vs a generic default.
- **Verify load-bearing claims** against the live web (the free legs do this well).
- **Cross-vendor adversarial re-check** — have a DIFFERENT family try to refute the synthesis before
  you trust it (3 models agreeing ≠ proof; beware herding and false precision).
- **Resolve by verified fact, not by vote** — a verified minority can override a generic majority.

## GOALS, not prescriptions

Design the orchestrator yourself — the Claude Code Workflow tool, a plain bash/Python fan-out, or
something else. The only fixed, proven pieces are the two wrappers and the facts above. Optimize for
the two goals (better research + minimal Opus).

## MANDATORY before you rely on this — re-run the smoke tests HERE

Prior tests were in the source project; auth/paths must be re-confirmed in THIS folder:
1. `lib/legs/ask_codex.sh --probe` → reports a non-weak served model (gpt-5.5).
2. `lib/legs/ask_gemini.sh --probe` → reports `gemini-3.1-pro-preview` (NOT a flash/lite tier).
2b. `lib/legs/ask_claude.sh --probe` → reports a non-weak served model (e.g. `claude-opus-4-8`,
   weak_tier=0). Requires a logged-in `claude` CLI (subscription OAuth), works headless.
3. Ask each leg for a current web fact + source URL; confirm it's real (web grounding works).
4. Run a normal Gemini call such as `lib/legs/ask_gemini.sh "Reply with exactly: ok"`; `--probe` does
   not append an audit row.
5. `cat data/served-models.jsonl` → confirm Gemini's `served` is the pro tier with `weak_tier:0`.

## Provenance

Extracted from the `find-truth` finance risk-radar. Only the vendor-transport CORE + the verified
subscription/headless knowledge were carried over — none of the finance logic, data, strategy, or
reports. Wrapper comments may reference `pipeline/preflight.sh` (a find-truth file, not copied); the
`--probe` mode they mention is generic and reusable here.

## Guardrails

Subscription-only (no per-call API unless you deliberately add a key under `.secrets/`). Read-only
legs. The owner controls all git — do not `git init` / commit / push without an explicit instruction.
