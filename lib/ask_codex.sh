#!/usr/bin/env bash
# Cross-vendor leg: OpenAI GPT via Codex CLI, BY SUBSCRIPTION, READ-ONLY.
# Prompt as $1 or stdin. Prints the model's final message to stdout.
#
# AUTONOMY MODEL POLICY (owner request 2026-06-09): do NOT hardcode a model version.
#   - No -m flag => the Codex CLI default, which OpenAI moves to the CURRENT FLAGSHIP with CLI
#     updates (this is how gpt-5.5 was being served before any pin existed). New flagship ships
#     -> this leg picks it up with a normal `codex` update, no repo edit.
#   - GUARD instead of pin: the served model is extracted and tier-classified. If it looks like
#     a weak tier (mini/nano/lite/flash/...), the call FAILS (exit 3) rather than silently
#     feeding a cheap model's judgment into the pipeline (the workflow tolerates a dropped leg).
#     Override for emergencies: CODEX_ALLOW_WEAK=1. Force a specific model: CODEX_MODEL=<name>.
#   - Reasoning effort IS pinned (xhigh) — effort flags are stable across model generations.
#     Light calls may pass CODEX_EFFORT=medium.
#   - Every call logs {requested, served} to data/served-models.jsonl; `--probe` does a tiny
#     end-to-end call and reports the served model (used by pipeline/preflight.sh).
set -uo pipefail

# Never let a stray API key flip billing away from the subscription session.
unset OPENAI_API_KEY CODEX_API_KEY 2>/dev/null || true

EFFORT="${CODEX_EFFORT:-xhigh}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/data/served-models.jsonl"
WEAK_RE='(^|[-_.])(mini|nano|lite|flash|small|tiny|haiku)([-_.0-9]|$)'
MODEL_ARGS=()
[ -n "${CODEX_MODEL:-}" ] && MODEL_ARGS=(-m "$CODEX_MODEL")

PROBE=0
if [ "${1:-}" = "--probe" ]; then PROBE=1; PROMPT="Reply with exactly: ok"; shift || true
else
  PROMPT="${1:-}"
  [ -z "$PROMPT" ] && PROMPT="$(cat)"
fi

ERRF="$(mktemp)"; trap 'rm -f "$ERRF"' EXIT
set +e
OUT="$(codex exec --skip-git-repo-check --sandbox read-only \
        "${MODEL_ARGS[@]}" -c model_reasoning_effort="$EFFORT" "$PROMPT" 2>"$ERRF")"
RC=$?
set -e

# Best-effort served-model extraction from the startup banner (stderr first, then stdout).
served="$( { grep -m1 -ioE '(^|[[:space:]])model:?[[:space:]]+[a-z0-9._-]+' "$ERRF" 2>/dev/null \
          || printf '%s' "$OUT" | grep -m1 -ioE '(^|[[:space:]])model:?[[:space:]]+[a-z0-9._-]+'; } \
          | sed -E 's/.*[Mm]odel:?[[:space:]]+//' | head -1 )"

weak=0
if printf '%s' "${served:-}" | grep -qiE "$WEAK_RE"; then weak=1; fi
printf '{"ts":"%s","leg":"codex","requested":"%s","effort":"%s","served":"%s","weak_tier":%s,"rc":%d}\n' \
  "$(date -u +%FT%TZ)" "${CODEX_MODEL:-cli-default}" "$EFFORT" "${served:-unknown}" "$weak" "$RC" \
  >> "$LOG" 2>/dev/null || true

if [ "$PROBE" = "1" ]; then
  echo "codex served: ${served:-unknown} (weak_tier=$weak, rc=$RC)"
  [ $RC -ne 0 ] && exit $RC
  [ "$weak" = "1" ] && exit 3
  exit 0
fi

if [ $RC -ne 0 ]; then
  cat "$ERRF" >&2
  # Subscription usage-limit errors get a distinct exit code so the orchestrator can drop the
  # leg for the rest of the run instead of retrying into the same wall (observed 2026-06-12).
  if grep -qiE 'hit your usage limit|usage_limit|quota' "$ERRF" 2>/dev/null; then exit 5; fi
  exit $RC
fi
if [ "$weak" = "1" ] && [ "${CODEX_ALLOW_WEAK:-0}" != "1" ]; then
  echo "ask_codex.sh: REFUSING weak-tier model '${served}' in a judgment seat (set CODEX_ALLOW_WEAK=1 to override)" >&2
  exit 3
fi
printf '%s\n' "$OUT"
