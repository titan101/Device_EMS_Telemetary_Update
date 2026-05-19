#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "python3 was not found. Ask your server admin for Python 3.10+ with venv support." >&2
  exit 1
fi

if [ ! -d ".venv" ]; then
  if ! "$PYTHON_BIN" -m venv .venv; then
    echo "Could not create .venv. Ask your server admin to enable the Python venv module." >&2
    exit 1
  fi
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

export DEVICE_EMS_HOST="${DEVICE_EMS_HOST:-127.0.0.1}"
export DEVICE_EMS_PORT="${DEVICE_EMS_PORT:-8502}"

echo "Starting Device EMS Telemetry Update"
echo "Local URL: http://127.0.0.1:${DEVICE_EMS_PORT}"
if [ "$DEVICE_EMS_HOST" = "0.0.0.0" ]; then
  echo "Server URL: http://SERVER_IP:${DEVICE_EMS_PORT}"
fi
echo "Press Ctrl+C to stop."

.venv/bin/streamlit run app.py \
  --server.address "$DEVICE_EMS_HOST" \
  --server.port "$DEVICE_EMS_PORT" \
  --server.headless true \
  "$@"
