"""runs/<run>/: targets, run.json, status.json and the per-device folders."""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

from . import status

RUN_FILE = "run.json"
TARGETS_FILE = "targets.txt"
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
PLATFORMS = ("mx", "acx", "ex", "srx")


class RunError(RuntimeError):
    pass


def safe_run_id(name: str) -> str:
    cleaned = _SAFE_RE.sub("_", (name or "").strip()).strip("_.")
    return cleaned or datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_targets(text: str) -> tuple[list[str], dict[str, str]]:
    """One device per line, optional ',platform'. Returns (devices, platform hints)."""
    devices: list[str] = []
    hints: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"[,\s;]+", line) if p.strip()]
        device = parts[0]
        if not _DEVICE_RE.match(device):
            raise RunError(f"not a device name or address: {device!r}")
        if device in devices:
            continue
        devices.append(device)
        if len(parts) > 1:
            platform = parts[1].lower()
            if platform not in PLATFORMS:
                raise RunError(f"{device}: platform {platform!r} is not one of {', '.join(PLATFORMS)}")
            hints[device] = platform
    if not devices:
        raise RunError("no devices given")
    return devices, hints


def create_run(runs_root: Path, run_id: str, devices: list[str], hints: dict[str, str],
               cm_number: str = "", note: str = "") -> Path:
    run_dir = runs_root / run_id
    if run_dir.exists():
        raise RunError(f"run {run_id} already exists at {run_dir} -- pick another name or add to it with --add")
    run_dir.mkdir(parents=True)
    write_targets(run_dir, devices, hints)
    meta = {"run_id": run_id, "created": datetime.now().isoformat(timespec="seconds"), "cm_number": cm_number,
            "note": note, "device_count": len(devices)}
    (run_dir / RUN_FILE).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return run_dir


def write_targets(run_dir: Path, devices: list[str], hints: dict[str, str]) -> None:
    lines = [f"{d},{hints[d]}" if d in hints else d for d in devices]
    (run_dir / TARGETS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_run(runs_root: Path, run_id: str) -> tuple[Path, dict, list[str], dict[str, str]]:
    run_dir = runs_root / safe_run_id(run_id)
    if not run_dir.is_dir():
        raise RunError(f"no run called {run_id!r} under {runs_root}")
    try:
        meta = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {"run_id": run_dir.name, "cm_number": "", "note": ""}
    try:
        devices, hints = parse_targets((run_dir / TARGETS_FILE).read_text(encoding="utf-8"))
    except OSError:
        devices, hints = [], {}
    return run_dir, meta, devices, hints


def list_runs(runs_root: Path) -> list[dict]:
    out = []
    if not runs_root.is_dir():
        return out
    for run_dir in sorted(runs_root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not run_dir.is_dir() or not (run_dir / RUN_FILE).exists():
            continue
        _, meta, devices, _ = load_run(runs_root, run_dir.name)
        counts = state_counts(run_dir, devices)
        out.append({"run_id": run_dir.name, "meta": meta, "devices": len(devices), "counts": counts,
                    "updated": datetime.fromtimestamp(run_dir.stat().st_mtime).isoformat(timespec="minutes")})
    return out


def device_states(run_dir: Path, devices: list[str]) -> list[dict]:
    data = status.load_status(run_dir)
    rows = []
    for device in devices:
        records = data["devices"].get(device) or {}
        state, detail = status.device_state(records)
        rows.append({"device": device, "state": state, "detail": detail, "records": records})
    return rows


def state_counts(run_dir: Path, devices: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in device_states(run_dir, devices):
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return counts


def export_csv(run_dir: Path, devices: list[str], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["device", "state", "detail", "platform", "model", "version", "credential", "confirmed_at",
                    "rollback_due_at"])
        for row in device_states(run_dir, devices):
            r = row["records"]
            disc = r.get(status.DISCOVER) or r.get(status.rehearsal_key(status.DISCOVER)) or {}
            conf = r.get(status.CONFIRM) or {}
            dep = r.get(status.DEPLOY) or {}
            w.writerow([row["device"], row["state"], row["detail"], disc.get("platform", ""), disc.get("model", ""),
                        disc.get("version", ""), conf.get("credential") or dep.get("credential") or disc.get("credential", ""),
                        conf.get("when", "") if conf.get("verdict") == "ok" else "", dep.get("rollback_due_at", "")])
