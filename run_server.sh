#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

export DEVICE_EMS_HOST="${DEVICE_EMS_HOST:-0.0.0.0}"
export DEVICE_EMS_PORT="${DEVICE_EMS_PORT:-8502}"

exec ./run.sh "$@"
