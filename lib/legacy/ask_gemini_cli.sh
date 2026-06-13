#!/usr/bin/env bash
# ============================================================================================
# RETIRED 2026-06-12 — NOT ON ANY CODE PATH. Kept as a forensic fossil only.
# The live Google leg is the agy-only wrapper in the llm-legs submodule (lib/legs/ask_gemini.sh).
# The Gemini CLI this file drove reaches EOL ~2026-06-18; delete this file after that date.
# ============================================================================================
# Cross-vendor leg: Google Gemini, SUBSCRIPTION-ONLY (owner decision 2026-06-09: no API keys,
# no per-call billing; if the leg dies, the pipeline degrades to 2 vendors — accepted).
# Prompt as $1 or stdin; model's text to stdout.
#
# Transport order:
#   1) Gemini CLI (subscription) until Google kills it 2026-06-18: `-m pro` alias auto-tracks the
#      flagship tier; read-only plan mode. A flash/lite quota-fallback is REJECTED (weak tier must
#      not sit in a judgment seat) and we fall through. Hard-bounded (the CLI HANGS on quota
#      exhaustion); quota errors exit 5.
#   2) Antigravity CLI `agy` DIRECT headless (the subscription successor, separate quota pool):
#      agy >=1.0.7 `--print` works on plain pipes and `--model` pins "Gemini 3.1 Pro (High)".
#      Fast (~8 s probe). Caveat: served model is NOT verifiable — logged as pinned/unverified.
#      Auth: run `agy` interactively once to log in before relying on this path.
#   3) agy via PTY workaround (legacy, 5-12 min/call) — only when the direct path yields nothing
#      (github.com/google-antigravity/antigravity-cli/issues/76 on old versions). Fail-fast
#      callers (orchestrator search) stop before this path.
#   4) (dormant) API per-call — ONLY if a key file ever appears at .secrets/gemini_api_key.
#      The owner has decided NOT to create one; the path stays dead code by default.
#   5) Fail with a clear message (exit 5 when the cause is quota) — the workflow's LEG
#      UNAVAILABLE protocol drops the leg without fabricating an opinion.
#
# Every call logs {transport, requested, served, weak_tier} to data/served-models.jsonl.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/data/served-models.jsonl"
KEYFILE="$ROOT/.secrets/gemini_api_key"
CACHEF="$ROOT/data/cache/gemini-api-model.txt"
CLI_MODEL="pro"
FALLBACK_MODEL="gemini-3.1-pro"
WEAK_RE='(^|[-_.])(flash|lite|nano|mini|small|tiny)([-_.0-9]|$)'

log() { # $1 transport, $2 requested, $3 served, $4 weak(0/1)
  printf '{"ts":"%s","leg":"gemini","transport":"%s","requested":"%s","served":"%s","weak_tier":%s}\n' \
    "$(date -u +%FT%TZ)" "$1" "$2" "$3" "${4:-0}" >> "$LOG" 2>/dev/null || true
}
is_weak() { printf '%s' "$1" | grep -qiE "$WEAK_RE"; }
api_key() { cat "$KEYFILE" 2>/dev/null; }
extract_served_model() {
  # Gemini CLI stats can include internal/service models. Only roles.main is the response model.
  jq -er '
    [(.stats.models // {}) | to_entries[] | select((.value.roles.main.totalRequests // 0) > 0) | .key]
    | sort
    | if length > 0 then join(",") else empty end
  ' 2>/dev/null
}

strip_tty() { # remove ANSI escapes + CR that the PTY workaround introduces
  sed -E $'s/\x1b\\[[0-9;?]*[A-Za-z]//g; s/\x1b\\][^\x07]*\x07//g' | tr -d '\r'
}

# ---------------- helper modes ----------------
case "${1:-}" in
  --extract-served-model)
    extract_served_model; exit $? ;;
  --list-models)
    curl -s --max-time 30 "https://generativelanguage.googleapis.com/v1beta/models?key=$(api_key)" \
      | jq -r '.models[].name'; exit $? ;;
  --probe)
    if command -v gemini >/dev/null 2>&1; then
      out="$(env -u GEMINI_API_KEY -u GOOGLE_API_KEY gemini -m "$CLI_MODEL" -p 'Reply with exactly: ok' --approval-mode plan -o json 2>&1)"
      js="$(printf '%s' "$out" | awk 'f||/^[[:space:]]*{/{f=1;print}')"
      if printf '%s' "$js" | jq -e '.response' >/dev/null 2>&1; then
        served="$(printf '%s' "$js" | extract_served_model || true)"
        if [ -z "$served" ]; then
          echo "gemini CLI response has no roles.main served model — rejecting" >&2
          exit 3
        fi
        if ! is_weak "$served"; then echo "gemini CLI alive: $served"; exit 0; fi
        echo "gemini CLI serving WEAK tier: $served" >&2; exit 3
      fi
      reason="$(printf '%s' "$out" | grep -oE '"message": "[^"]+"' | head -1)"
      echo "gemini CLI call FAILED: ${reason:-unparseable output} (capacity 429 = Google-side, retry later)" >&2
    fi
    if command -v agy >/dev/null 2>&1; then
      echo "agy present ($(agy --version 2>/dev/null | head -1 || echo 'version unknown')) — will be tried via PTY workaround; no probe call (weekly compute cap)"
      exit 0
    fi
    echo "gemini leg: NO subscription transport (no gemini CLI, no agy) — pipeline will run 2-vendor" >&2
    exit 1 ;;
esac

PROMPT="${1:-}"
[ -z "$PROMPT" ] && PROMPT="$(cat)"

# ---------------- transport 1: Gemini CLI (subscription, until 2026-06-18) ----------------
# v0.46 prints a migration BANNER into stdout even in headless mode -> the JSON must be
# EXTRACTED from mixed output, not assumed pure (root cause of the 2026-06-10 false "leg dead").
# QUOTA BEHAVIOR (observed 2026-06-11/12): when the pro quota is exhausted, the CLI does NOT
# fail — it loops internal retries and hangs for many minutes. So the CLI call gets a hard
# timeout, and quota signals turn into exit 5 (distinct from generic failure) so the
# orchestrator can drop the leg instantly. GEMINI_FAIL_FAST=1 (set by the orchestrator for
# time-critical search calls) skips the retry AND the slow agy fallback.
QUOTA_RE='exhausted your capacity|RESOURCE_EXHAUSTED|MODEL_CAPACITY|429'
extract_json() { awk 'f||/^[[:space:]]*{/{f=1;print}'; }
if command -v gemini >/dev/null 2>&1; then
  GERR="$(mktemp)"; GOUT="$(mktemp)"
  call_cli() { # hard-bounded CLI call; output lands in $GOUT
    env -u GEMINI_API_KEY -u GOOGLE_API_KEY \
      gemini -m "$CLI_MODEL" -p "$PROMPT" --approval-mode plan -o json >"$GOUT" 2>"$GERR" &
    local pid=$!
    # The CLI traps SIGTERM and keeps retrying when the quota is dead, and it runs a heavy
    # child node process (observed live 2026-06-12) — escalate to SIGKILL, children included.
    ( sleep "${GEMINI_CLI_TIMEOUT:-240}"
      kill -TERM "$pid" 2>/dev/null
      sleep 8
      pkill -KILL -P "$pid" 2>/dev/null
      kill -KILL "$pid" 2>/dev/null
    ) >/dev/null 2>&1 &
    local watcher=$!
    wait "$pid"; local rc=$?
    kill "$watcher" 2>/dev/null; wait "$watcher" 2>/dev/null
    extract_json <"$GOUT"
    return $rc
  }
  quota_hit() { grep -qiE "$QUOTA_RE" "$GERR" 2>/dev/null; }
  raw="$(call_cli)"
  if ! printf '%s' "$raw" | jq -e '.response' >/dev/null 2>&1 && quota_hit; then
    if [ "${GEMINI_FAIL_FAST:-0}" = "1" ]; then
      log "cli" "$CLI_MODEL" "QUOTA_EXHAUSTED" 0
      echo "ask_gemini.sh: pro quota exhausted (fail-fast) — leg should be dropped for this run" >&2
      rm -f "$GERR" "$GOUT"
      exit 5
    fi
    echo "ask_gemini: 429/quota on $CLI_MODEL — retrying once in 20s" >&2
    sleep 20
    raw="$(call_cli)"
  fi
  STILL_QUOTA=0
  if ! printf '%s' "$raw" | jq -e '.response' >/dev/null 2>&1 && quota_hit; then STILL_QUOTA=1; fi
  [ -s "$GERR" ] && head -2 "$GERR" | sed 's/^/ask_gemini cli stderr: /' >&2
  rm -f "$GERR" "$GOUT"
  if command -v jq >/dev/null 2>&1 && printf '%s' "$raw" | jq -e '.response' >/dev/null 2>&1; then
    served="$(printf '%s' "$raw" | extract_served_model || true)"
    if [ -z "$served" ]; then
      log "cli" "$CLI_MODEL" "unknown" 1
      echo "ask_gemini.sh: CLI response has no roles.main served model — rejecting, trying next transport" >&2
    elif is_weak "$served" && [ "${GEMINI_ALLOW_WEAK:-0}" != "1" ]; then
      log "cli" "$CLI_MODEL" "$served" 1
      echo "ask_gemini.sh: CLI served weak tier '$served' — rejecting, trying next transport" >&2
    else
      log "cli" "$CLI_MODEL" "$served" 0
      printf '%s' "$raw" | jq -r '.response'
      exit 0
    fi
  fi
fi

# ---------------- transport 2: Antigravity CLI, direct headless (subscription) ----------------
# agy >=1.0.7: --print works without the PTY workaround and --model pins the tier explicitly.
# Measured 2026-06-12: 8 s for a probe with "Gemini 3.1 Pro (High)" — fast enough for ANY call,
# so fail-fast no longer skips agy (it skips only the slow PTY path below). Separate quota pool
# from the Gemini CLI — this is the designated successor when the CLI dies (~2026-06-18).
# Limitation: agy does not report the served model; the audit row records the PIN, unverified.
AGY_MODEL="${AGY_MODEL:-Gemini 3.1 Pro (High)}"
if command -v agy >/dev/null 2>&1; then
  out="$(agy --print "$PROMPT" --model "$AGY_MODEL" --print-timeout "${AGY_PRINT_TIMEOUT:-5m}" </dev/null 2>/dev/null)"
  if [ -n "$(printf '%s' "$out" | tr -d '[:space:]')" ]; then
    log "agy" "$AGY_MODEL" "antigravity:pinned:$AGY_MODEL (unverified)" 0
    printf '%s\n' "$out"
    exit 0
  fi
  echo "ask_gemini.sh: agy --print produced no output (model '$AGY_MODEL') — trying PTY workaround" >&2
fi

# ---------------- transport 3: Antigravity CLI via PTY workaround (legacy, slow) ----------------
# Fail-fast callers (orchestrator search calls) stop here: the PTY path takes 5-12 min per call
# and a dropped task is rescued by the other leg far cheaper than that.
if [ "${GEMINI_FAIL_FAST:-0}" = "1" ]; then
  if [ "${STILL_QUOTA:-0}" = "1" ]; then
    log "none" "-" "QUOTA_EXHAUSTED" 0
    echo "ask_gemini.sh: quota exhausted, fail-fast set — skipping slow PTY fallback" >&2
    exit 5
  fi
  log "none" "-" "FAILED_FAST" 0
  echo "ask_gemini.sh: CLI and agy-direct failed, fail-fast set — skipping slow PTY fallback" >&2
  exit 1
fi
if command -v agy >/dev/null 2>&1; then
  TMPD="$(mktemp -d)"; trap 'rm -rf "$TMPD"' EXIT
  if [ "$(uname)" = "Darwin" ]; then
    # BSD script: child runs under a real PTY; transcript lands in the file (stdout tee silenced).
    # Args pass directly (no shell) -> any prompt content is safe; only argv size (~1MB) bounds it.
    script -q "$TMPD/t" agy -p "$PROMPT" </dev/null >/dev/null 2>&1 || true
    out="$(cat "$TMPD/t" 2>/dev/null)"
  else
    # util-linux script: -c takes a shell string; pass the prompt via env to survive quotes/newlines.
    export AGY_PROMPT="$PROMPT"
    out="$(script -qec 'agy -p "$AGY_PROMPT"' /dev/null </dev/null 2>/dev/null)"
  fi
  out="$(printf '%s' "$out" | strip_tty)"
  if [ -n "$(printf '%s' "$out" | tr -d '[:space:]')" ]; then
    log "agy" "default" "antigravity:default" 0
    printf '%s\n' "$out"
    exit 0
  fi
  echo "ask_gemini.sh: agy produced no output via PTY workaround" >&2
fi

# ---------------- transport 4 (DORMANT by owner decision): API per-call ----------------
if [ -r "$KEYFILE" ]; then
  TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
  printf '%s' "$PROMPT" > "$TMP"
  MODEL="$FALLBACK_MODEL"
  [ -f "$CACHEF" ] && MODEL="$(cut -d'|' -f2 "$CACHEF" 2>/dev/null || echo "$FALLBACK_MODEL")"
  resp="$(jq -n --rawfile t "$TMP" '{contents:[{role:"user",parts:[{text:$t}]}]}' \
    | curl -s --max-time 600 -X POST -H 'Content-Type: application/json' -d @- \
        "https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent?key=$(api_key)")"
  if printf '%s' "$resp" | jq -e '.candidates[0].content.parts' >/dev/null 2>&1; then
    log "api" "$MODEL" "$MODEL" 0
    printf '%s' "$resp" | jq -r '[.candidates[0].content.parts[].text] | join("")'
    exit 0
  fi
fi

if [ "${STILL_QUOTA:-0}" = "1" ]; then
  echo "ask_gemini.sh: pro quota exhausted and no usable fallback — leg unavailable (quota)" >&2
  log "none" "-" "QUOTA_EXHAUSTED" 0
  exit 5
fi
echo "ask_gemini.sh: no working subscription transport (gemini CLI dead/weak, agy missing/silent) — leg unavailable" >&2
log "none" "-" "FAILED" 0
exit 1
