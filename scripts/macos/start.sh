#!/bin/bash
# Start the native macOS deployment: loads configuration, starts the backend
# (which serves the UI), verifies readiness, and opens the local UI address.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="$ROOT/scripts/macos/native.env"
PID_FILE="${TMPDIR:-/tmp}/audiotranscription.pid"

[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }
APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-8080}"

[ -x "$ROOT/backend/.venv/bin/python" ] || {
  echo "Backend environment missing — run ./scripts/macos/setup.sh first." >&2; exit 1; }

# Prevent duplicate backend instances opening the same job store.
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Already running (PID $(cat "$PID_FILE")) — UI at http://localhost:$APP_PORT" >&2
  exit 0
fi

# Detect an occupied port and explain how to change it consistently.
if lsof -nP -iTCP:"$APP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $APP_PORT is already in use by another process." >&2
  echo "Set APP_PORT in scripts/macos/native.env to a free port and re-run;" >&2
  echo "the UI, API, and WebSocket routes all follow that single setting." >&2
  exit 1
fi

LOG_DIR="${DATA_DIR:-$HOME/Library/Application Support/AudioTranscription}/logs"
mkdir -p "$LOG_DIR"
cd "$ROOT/backend"
nohup .venv/bin/python -m app.main >> "$LOG_DIR/backend.log" 2>&1 &
echo $! > "$PID_FILE"

printf 'Starting'
for _ in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:$APP_PORT/api/health" >/dev/null 2>&1; then
    printf '\nReady — UI: http://localhost:%s\n' "$APP_PORT"
    open "http://localhost:$APP_PORT" 2>/dev/null || true
    exit 0
  fi
  printf '.'
  sleep 0.5
done
printf '\nBackend did not become ready; see %s/backend.log\n' "$LOG_DIR" >&2
exit 1
