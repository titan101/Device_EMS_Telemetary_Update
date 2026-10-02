#!/usr/bin/env bash
# Weekly TACACS/syslog/NTP/SNMP watchdog: parses a RANCID-style folder of Junos
# 'display set' config dumps and generates an audit report + per-device fix
# files. Read-only — no live device connections, no deploy step.
set -euo pipefail

cd "$(dirname "$0")"

RANCID_FOLDER="${1:-/mnt/whplrancid101/PE_MX/configs/}"
DESIRED_STATE_FILE="${2:-config/desired_state.tacacs_migration.json}"
CHANGE_ID="WATCHDOG-$(date +%Y%m%d)"

if [ ! -f "$DESIRED_STATE_FILE" ]; then
  echo "Desired-state file not found: $DESIRED_STATE_FILE" >&2
  echo "Copy config/desired_state.tacacs_migration.example.json and fill in the real server IPs/users first." >&2
  exit 1
fi

./run_cli.sh discover-rancid "$CHANGE_ID" --folder "$RANCID_FOLDER" --platform mx
./run_cli.sh desired-set "$CHANGE_ID" --file "$DESIRED_STATE_FILE"
./run_cli.sh build "$CHANGE_ID"
./run_cli.sh report "$CHANGE_ID"
./run_cli.sh status "$CHANGE_ID"
