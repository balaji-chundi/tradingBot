#!/usr/bin/env bash
# Push the working tree to the Linode and re-sync deps.
# Idempotent. Source of truth is whatever is on disk locally — we don't pull
# from git, so uncommitted changes deploy too (deliberate during early phases).
set -euo pipefail

REMOTE_HOST="${TRADING_APP_HOST:-root@172.105.58.133}"
REMOTE_DIR="${TRADING_APP_DIR:-/root/trading-app}"

cd "$(dirname "$0")/.."

echo "==> rsync to $REMOTE_HOST:$REMOTE_DIR"
rsync -az --delete \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  --exclude='.mypy_cache' \
  --exclude='.ruff_cache' \
  --exclude='data/' \
  --exclude='logs/' \
  --exclude='reports/' \
  --exclude='.env' \
  ./ "$REMOTE_HOST:$REMOTE_DIR/"

echo "==> uv sync on remote"
ssh -o BatchMode=yes "$REMOTE_HOST" "
  export PATH=\$HOME/.local/bin:\$PATH
  cd $REMOTE_DIR
  uv sync --extra dev --quiet
  echo 'uv sync ok'
"

echo "==> done"
