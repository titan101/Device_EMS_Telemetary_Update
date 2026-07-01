from __future__ import annotations

import json
import re
from pathlib import Path

from .models import RunState, run_from_dict, run_to_dict


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RUNS_DIR = DATA_DIR / "runs"
REPORTS_DIR = DATA_DIR / "reports"
FIX_FILES_DIR = DATA_DIR / "fix_files"
VAULT_FILE = DATA_DIR / "credential_vault.json"


def ensure_data_dirs() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    FIX_FILES_DIR.mkdir(parents=True, exist_ok=True)


def safe_change_id(change_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", change_id.strip())
    return cleaned or "unspecified_change"


def run_path(change_id: str) -> Path:
    ensure_data_dirs()
    return RUNS_DIR / f"{safe_change_id(change_id)}.json"


def save_run(run: RunState) -> Path:
    ensure_data_dirs()
    run.touch()
    path = run_path(run.change_id)
    path.write_text(json.dumps(run_to_dict(run), indent=2), encoding="utf-8")
    return path


def load_run(change_id: str) -> RunState:
    path = run_path(change_id)
    if not path.exists():
        return RunState(change_id=change_id)
    return run_from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_runs() -> list[str]:
    ensure_data_dirs()
    runs = sorted(RUNS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return [path.stem for path in runs]


def latest_run() -> RunState:
    runs = list_runs()
    if runs:
        return load_run(runs[0])
    return RunState(change_id="CHG000000")
