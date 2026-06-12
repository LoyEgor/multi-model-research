#!/usr/bin/env bash
# Double-click launcher for the research Web UI (macOS opens .command files in Terminal).
# Starts the local server and opens the browser. Press Ctrl+C in this window to stop it.
set -uo pipefail
cd "$(dirname "$0")"

PORT="${RESEARCH_UI_PORT:-8765}"
URL="http://127.0.0.1:$PORT"

if curl -s --max-time 1 "$URL/api/runs" >/dev/null 2>&1; then
  echo "Server is already running at $URL — opening the browser."
  open "$URL"
  exit 0
fi

echo "Starting the research UI at $URL  (Ctrl+C here to stop)"
( sleep 1.5; open "$URL" ) &
exec python3 research.py --serve --port "$PORT"
