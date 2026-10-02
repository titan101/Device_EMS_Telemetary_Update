#!/usr/bin/env bash
# One-shot environment setup: finds a working python3, builds ./venv, installs
# requirements. Safe to re-run. Override the interpreter with PYTHON_BIN=...
set -euo pipefail

cd "$(dirname "$0")"
VENV_DIR="${VENV_DIR:-venv}"

find_python() {
  if [[ -n "${PYTHON_BIN:-}" ]] && "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "$PYTHON_BIN"; return
  fi
  # Run each candidate instead of trusting `which`: the Windows Store stub
  # answers `which` but can't actually run anything.
  for candidate in python3 python; do
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$candidate"; return
    fi
  done
  echo "ERROR: no python >= 3.10 found. Install python3 (Ubuntu: apt install python3 python3-venv)." >&2
  exit 1
}

PY="$(find_python)"
echo "Using $("$PY" --version 2>&1) at $(command -v "$PY")"

if [[ -d "$VENV_DIR" ]] && ! "$VENV_DIR"/bin/python -c 'import sys' 2>/dev/null \
   && ! "$VENV_DIR"/Scripts/python.exe -c 'import sys' 2>/dev/null; then
  mv "$VENV_DIR" "$VENV_DIR.broken.$(date +%Y%m%d_%H%M%S)"
  echo "Moved a broken $VENV_DIR aside."
fi

if [[ ! -d "$VENV_DIR" ]]; then
  if ! "$PY" -m venv "$VENV_DIR" 2>/dev/null; then
    echo "python -m venv failed (missing ensurepip?) -- trying --without-pip + ensurepip"
    "$PY" -m venv --without-pip "$VENV_DIR"
  fi
fi

if [[ -x "$VENV_DIR/bin/python" ]]; then VENV_PY="$VENV_DIR/bin/python"; else VENV_PY="$VENV_DIR/Scripts/python.exe"; fi

if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
  "$VENV_PY" -m ensurepip --upgrade 2>/dev/null || {
    echo "ensurepip unavailable -- fetching get-pip.py"
    if command -v curl >/dev/null; then curl -sSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    else wget -qO /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py; fi
    "$VENV_PY" /tmp/get-pip.py
  }
fi

"$VENV_PY" -m pip install --upgrade pip >/dev/null
# requirements.txt belongs to the legacy Streamlit app (its own .venv via run.sh);
# the console and CLI need only this short list.
"$VENV_PY" -m pip install -r requirements-console.txt
mkdir -p runs logs
echo
echo "Ready. Run it with:  $VENV_PY cli.py --help     (console: ./run_webapp.sh)"
