#!/usr/bin/env bash
# Starts the console. Builds the venv on first use.
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d venv ]]; then ./setup.sh; fi
if [[ -x venv/bin/python ]]; then exec venv/bin/python webapp.py "$@"; fi
exec venv/Scripts/python.exe webapp.py "$@"
