#!/usr/bin/env bash
# Double-click launcher for the research Web UI (macOS opens .command files in Terminal).
# Starts the local server and opens the browser. Press Ctrl+C in this window to stop it.
set -uo pipefail
cd "$(dirname "$0")"

PORT="${RESEARCH_UI_PORT:-8765}"

# True only when the port serves OUR /api/runs (2xx via -f AND a JSON body with "runs"),
# so a foreign app answering 404 — or a stray 200 — on that path is not mistaken for us.
is_ours() {
  local body
  # 2s: a busy server (verification fan-out saturating the pool) can answer slower than 1s, and a
  # false negative here makes the launcher hop ports away from its own healthy instance.
  body="$(curl -sf --max-time 2 "http://127.0.0.1:$1/api/runs" 2>/dev/null)" || return 1
  case "$body" in *'"runs"'*) return 0 ;; *) return 1 ;; esac
}

# True when nothing holds the port (bind fails while a listener owns it).
port_free() {
  python3 - "$1" <<'PY'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
# The real server (ThreadingHTTPServer) binds with allow_reuse_address, so this probe must too —
# otherwise lingering TIME_WAIT sockets after a quick restart read as "port taken by a foreign app".
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

if is_ours "$PORT"; then
  URL="http://127.0.0.1:$PORT"
  echo "Server is already running at $URL — opening the browser."
  open "$URL"
  exit 0
fi

if ! port_free "$PORT"; then
  # PORT is held by a foreign app: reuse our server if it sits on a nearby port,
  # else start on the first free port in the scan window and warn.
  original="$PORT"
  chosen=""
  for ((p = original + 1; p <= original + 20; p++)); do
    if is_ours "$p"; then
      URL="http://127.0.0.1:$p"
      echo "Port $original is taken; our server is already running at $URL — opening the browser."
      open "$URL"
      exit 0
    fi
    if [[ -z "$chosen" ]] && port_free "$p"; then
      chosen="$p"
    fi
  done
  if [[ -z "$chosen" ]]; then
    echo "Port $original is taken and no free port found in $((original + 1))..$((original + 20)) — set RESEARCH_UI_PORT to override." >&2
    exit 1
  fi
  echo "Port $original is taken by another app — starting on $chosen instead; set RESEARCH_UI_PORT to override."
  PORT="$chosen"
fi

URL="http://127.0.0.1:$PORT"
echo "Starting the research UI at $URL  (Ctrl+C here to stop)"
( sleep 1.5; open "$URL" ) &
exec python3 research.py --serve --port "$PORT"
