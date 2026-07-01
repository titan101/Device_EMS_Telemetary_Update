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

.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt >/dev/null

.venv/bin/python -m device_ems_telemetary_update.cli "$@"
