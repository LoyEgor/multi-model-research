#!/usr/bin/env bash
# Cross-vendor leg: Google, via the Antigravity CLI (`agy`), SUBSCRIPTION-ONLY.
# Prompt as $1 or stdin; the model's text answer goes to stdout.
#
# 2026-06-12: the legacy Gemini CLI transport was retired early (Google kills that CLI
# ~2026-06-18; owner decision to migrate now). The old multi-transport wrapper is preserved at
# lib/legacy/ask_gemini_cli.sh. agy >=1.0.7 headless facts (measured here):
#   - `--print` works on plain pipes (no PTY workaround needed), ~8-15 s per simple call;
#   - `--model "<label>"` pins the model per call (labels from `agy models`); the interactive
#     /model command merely writes the same label to ~/.gemini/antigravity-cli/settings.json;
#   - the served model is NOT reported back — the audit row records the PIN, unverified;
#   - quota state has no public command (the interactive quota panel uses an internal API), so
#     exhaustion is detected per call and mapped to exit 5 (the orchestrator drops the leg).
#
# Model chain (same Google family only — this leg must stay vendor-pure for cross-checking):
#   1) AGY_MODEL          (default "Gemini 3.1 Pro (High)")
#   2) AGY_MODEL_FALLBACK (default "Gemini 3.1 Pro (Low)" — same pro tier, lower reasoning)
# Flash/lite tiers are WEAK and never used in a judgment seat (GEMINI_ALLOW_WEAK=1 overrides).
#
# Every call logs {transport:"agy", requested, served(pinned, unverified)} to
# data/served-models.jsonl. Modes: --probe (no model call), --list-models.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/data/served-models.jsonl"
AGY_MODEL="${AGY_MODEL:-Gemini 3.1 Pro (High)}"
AGY_MODEL_FALLBACK="${AGY_MODEL_FALLBACK:-Gemini 3.1 Pro (Low)}"
AGY_PRINT_TIMEOUT="${AGY_PRINT_TIMEOUT:-5m}"
WEAK_RE='(^|[^a-z])(flash|lite|nano|mini|small|tiny)([^a-z]|$)'
QUOTA_RE='quota|exhausted|capacity|rate.?limit|resource.?exhausted|429'

log() { # $1 requested, $2 served, $3 weak(0/1)
  printf '{"ts":"%s","leg":"gemini","transport":"agy","requested":"%s","served":"%s","weak_tier":%s}\n' \
    "$(date -u +%FT%TZ)" "$1" "$2" "${3:-0}" >> "$LOG" 2>/dev/null || true
}
is_weak() { printf '%s' "$1" | grep -qiE "$WEAK_RE"; }

case "${1:-}" in
  --list-models)
    agy models; exit $? ;;
  --probe)
    if ! command -v agy >/dev/null 2>&1; then
      echo "gemini leg: agy CLI not installed — leg unavailable" >&2
      exit 1
    fi
    MODELS="$(agy models 2>/dev/null)"
    if ! printf '%s' "$MODELS" | grep -qF "$AGY_MODEL"; then
      echo "gemini leg: pinned model '$AGY_MODEL' not in 'agy models' list:" >&2
      printf '%s\n' "$MODELS" >&2
      exit 3
    fi
    echo "gemini leg alive: agy $(agy --version 2>/dev/null | head -1), model pinned: $AGY_MODEL (no probe call — quota economy)"
    exit 0 ;;
esac

PROMPT="${1:-}"
[ -z "$PROMPT" ] && PROMPT="$(cat)"

if ! command -v agy >/dev/null 2>&1; then
  echo "ask_gemini.sh: agy CLI not installed — leg unavailable" >&2
  log "$AGY_MODEL" "MISSING_CLI" 0
  exit 1
fi

ERRF="$(mktemp)"; trap 'rm -f "$ERRF"' EXIT
quota_hit=0
for MODEL in "$AGY_MODEL" "$AGY_MODEL_FALLBACK"; do
  [ -z "$MODEL" ] && continue
  if is_weak "$MODEL" && [ "${GEMINI_ALLOW_WEAK:-0}" != "1" ]; then
    echo "ask_gemini.sh: REFUSING weak-tier model '$MODEL' in a judgment seat (GEMINI_ALLOW_WEAK=1 to override)" >&2
    continue
  fi
  out="$(agy --print "$PROMPT" --model "$MODEL" --print-timeout "$AGY_PRINT_TIMEOUT" </dev/null 2>"$ERRF")"
  if [ -n "$(printf '%s' "$out" | tr -d '[:space:]')" ]; then
    log "$MODEL" "antigravity:pinned:$MODEL (unverified)" 0
    printf '%s\n' "$out"
    exit 0
  fi
  if grep -qiE "$QUOTA_RE" "$ERRF" 2>/dev/null; then
    quota_hit=1
    log "$MODEL" "QUOTA_EXHAUSTED" 0
    echo "ask_gemini.sh: quota signal on '$MODEL' — trying next model in chain" >&2
  else
    head -2 "$ERRF" | sed 's/^/ask_gemini agy stderr: /' >&2
    echo "ask_gemini.sh: agy produced no output on '$MODEL' — trying next model in chain" >&2
  fi
done

if [ "$quota_hit" = "1" ]; then
  echo "ask_gemini.sh: quota exhausted across the model chain — leg unavailable (quota)" >&2
  log "chain" "QUOTA_EXHAUSTED" 0
  exit 5
fi
echo "ask_gemini.sh: agy produced no usable output for any model in the chain — leg unavailable" >&2
log "chain" "FAILED" 0
exit 1
