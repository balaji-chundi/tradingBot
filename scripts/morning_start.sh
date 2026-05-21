#!/usr/bin/env bash
# Pre-market boot: refresh Angel One tokens and (re)start uvicorn.
#
# Cron schedules this Mon-Fri at 03:25 UTC = 08:55 IST so tokens are fresh
# and the dashboard + orchestrator are up well before the 09:15 IST open.
#
# Idempotent: safe to run multiple times. Reads creds from /root/trading-app/.env.
# Logs to /root/trading-app/logs/cron/morning-YYYY-MM-DD.log (UTC date).
#
# Exit codes:
#   0  success (tokens refreshed + uvicorn responding to /health)
#   2  auth refresh failed
#   3  uvicorn failed to come up
#   4  preflight (working tree missing)
set -euo pipefail

APP_DIR="/root/trading-app"
PID_FILE="/tmp/trading-app-uv.pid"
RUNTIME_LOG="/tmp/trading-app-uv.log"
PORT=8000
HOST="0.0.0.0"

cd "$APP_DIR" || { echo "[$(date -u)] APP_DIR missing"; exit 4; }
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

LOGDIR="$APP_DIR/logs/cron"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/morning-$(date -u +%Y-%m-%d).log"

{
  echo "===================================================================="
  echo "[$(date -u)] morning_start.sh begin"
  echo "===================================================================="

  echo "[$(date -u)] step 1/3: refresh Angel One JWT + symbol-token cache"
  if uv run python -m app.scripts.auth; then
    echo "[$(date -u)] auth_ok"
  else
    rc=$?
    echo "[$(date -u)] auth FAILED rc=$rc" >&2
    exit 2
  fi

  echo "[$(date -u)] step 2/3: stop any existing uvicorn"
  if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
      echo "[$(date -u)] killing existing pid=$OLD_PID (SIGTERM)"
      kill "$OLD_PID" || true
      # Give FastAPI lifespan a chance to drain (orchestrator.stop, dispose_engine).
      for _ in 1 2 3 4 5; do
        sleep 1
        kill -0 "$OLD_PID" 2>/dev/null || break
      done
      if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[$(date -u)] still alive, SIGKILL"
        kill -9 "$OLD_PID" 2>/dev/null || true
      fi
    fi
    rm -f "$PID_FILE"
  fi
  # Catch any stray process bound to the port (orphans, manual launches).
  fuser -k -TERM "${PORT}/tcp" 2>/dev/null || true
  sleep 1

  echo "[$(date -u)] step 3/3: start uvicorn on ${HOST}:${PORT}"
  nohup uv run uvicorn app.main:app --host "$HOST" --port "$PORT" \
    >> "$RUNTIME_LOG" 2>&1 &
  NEW_PID=$!
  echo "$NEW_PID" > "$PID_FILE"
  echo "[$(date -u)] spawned pid=$NEW_PID"

  # Wait up to 15s for /health
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if curl -sf --max-time 1 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      echo "[$(date -u)] /health ok after ${i}s"
      echo "[$(date -u)] morning_start.sh DONE"
      exit 0
    fi
    sleep 1
  done
  echo "[$(date -u)] uvicorn did not respond to /health within 15s" >&2
  exit 3
} | tee -a "$LOGFILE"
