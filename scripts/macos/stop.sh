#!/bin/bash
# Graceful shutdown: the backend flushes recordings and finalized segments,
# marks in-flight work interrupted, and never deletes jobs, settings,
# credentials, or model downloads.
set -euo pipefail

PID_FILE="${TMPDIR:-/tmp}/audiotranscription.pid"
if [ ! -f "$PID_FILE" ]; then
  echo "Not running (no PID file at $PID_FILE)."
  exit 0
fi
PID="$(cat "$PID_FILE")"
if ! kill -0 "$PID" 2>/dev/null; then
  echo "Not running (stale PID file removed)."
  rm -f "$PID_FILE"
  exit 0
fi
kill -TERM "$PID"
for _ in $(seq 1 60); do
  kill -0 "$PID" 2>/dev/null || { echo "Stopped."; rm -f "$PID_FILE"; exit 0; }
  sleep 0.5
done
echo "Still shutting down after 30s; not forcing. Check logs before killing." >&2
exit 1
