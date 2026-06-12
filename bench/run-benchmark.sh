#!/usr/bin/env bash
# Effort benchmark driver: the same prompt at effort 1..4, sequentially (fair quota/load),
# run ids recorded in bench/bench-runs.txt. Progress lines are grep-able ("BENCH:").
set -uo pipefail
cd "$(dirname "$0")/.."

PROMPT="найди самый дешевый MacBook Air M2 в Украине"
: > bench/bench-runs.txt

for EFFORT in 1 2 3 4; do
  echo "BENCH: effort $EFFORT starting $(date -u +%FT%TZ)"
  OUT="$(python3 research.py --effort "$EFFORT" --site olx.ua "$PROMPT" 2>&1)"
  RC=$?
  RUN_ID="$(printf '%s' "$OUT" | grep -m1 '^run_id:' | awk '{print $2}')"
  printf '%s\n' "$OUT" > "bench/bench-effort-$EFFORT.log"
  echo "$EFFORT ${RUN_ID:-FAILED} rc=$RC" >> bench/bench-runs.txt
  echo "BENCH: effort $EFFORT done run_id=${RUN_ID:-FAILED} rc=$RC $(date -u +%FT%TZ)"
done
echo "BENCH: all done"
