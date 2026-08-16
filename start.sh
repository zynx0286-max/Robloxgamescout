#!/bin/sh
# Runs the snapshot collector in the background and the portfolio API in the
# foreground. Keeps the API process attached so hosting platforms (Render,
# Fly, Docker) can health-check and restart it properly.
#
# Both halves share one container filesystem, which keeps the SQLite
# database (scoutbot.db) in sync between writer and reader.
set -e

if command -v python >/dev/null 2>&1; then
    PY=python
else
    PY=python3
fi

: "${PORTFOLIO_WORKER_INTERVAL:=60}"

echo "Starting portfolio collector (interval=${PORTFOLIO_WORKER_INTERVAL}s) in background..."
"$PY" portfolio_worker.py --interval "${PORTFOLIO_WORKER_INTERVAL}" &
COLLECTOR_PID=$!

echo "Starting portfolio API on 0.0.0.0:${PORT:-8000}..."
"$PY" -m uvicorn portfolio_api:app --host 0.0.0.0 --port "${PORT:-8000}"

# uvicorn exit -> stop the collector and exit.
kill "${COLLECTOR_PID}" 2>/dev/null || true
