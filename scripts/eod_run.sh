#!/usr/bin/env bash
# End-of-day report wrapper for cron.
#
# Invokes app.scripts.eod_report for today's IST date, writing the report
# to reports/YYYY-MM-DD.md and the cron stdout/stderr to
# logs/cron/eod-YYYY-MM-DD.log. The CLI itself skips non-trading days.
#
# Schedule (set on the Linode via crontab):
#   5 10 * * 1-5 /root/trading-app/scripts/eod_run.sh
# That's 10:05 UTC = 15:35 IST, Mon-Fri (5 min after market close).
set -euo pipefail

APP_DIR="/root/trading-app"
cd "$APP_DIR"
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

LOGDIR="$APP_DIR/logs/cron"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/eod-$(date -u +%Y-%m-%d).log"

{
  echo "===================================================================="
  echo "[$(date -u)] eod_run.sh begin"
  echo "===================================================================="
  uv run python -m app.scripts.eod_report
  echo "[$(date -u)] eod_run.sh done"
} >> "$LOGFILE" 2>&1
