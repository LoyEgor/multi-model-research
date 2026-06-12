#!/usr/bin/env bash
# Re-run of effort 3 and 4 on quota-healthy providers (first attempt ran through quota
# exhaustion windows — see bench/REPORT.md). Run ids land in bench/bench-runs-34.txt.
set -uo pipefail
cd "$(dirname "$0")/.."

PROMPT="найди самый дешевый MacBook Air M2 в Украине"
: > bench/bench-runs-34.txt

for EFFORT in 3 4; do
  echo "BENCH: effort $EFFORT starting $(date -u +%FT%TZ)"
  OUT="$(python3 research.py --effort "$EFFORT" --site olx.ua "$PROMPT" 2>&1)"
  RC=$?
  RUN_ID="$(printf '%s' "$OUT" | grep -m1 '^run_id:' | awk '{print $2}')"
  printf '%s\n' "$OUT" > "bench/bench-rerun-effort-$EFFORT.log"
  echo "$EFFORT ${RUN_ID:-FAILED} rc=$RC" >> bench/bench-runs-34.txt
  echo "BENCH: effort $EFFORT done run_id=${RUN_ID:-FAILED} rc=$RC $(date -u +%FT%TZ)"
done
echo "BENCH: all done"
