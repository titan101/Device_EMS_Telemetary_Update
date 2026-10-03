"""Per-run device status: runs/<run>/status.json.

Shape: {"devices": {host: {step: record}}}. Written after EVERY device step,
by re-reading the file and overlaying only this process's records (two runs on
the same folder never erase each other), tmp + replace so a kill mid-write
can't leave half a file. A failed write is reported, never raised -- the run
output is the record of last resort.

Rehearsed (non-live) steps land under "<step>_rehearsal" so a dry run can
never overwrite a real record.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

STATUS_FILE = "status.json"
RETRY_DELAY = 0.2

# Step keys, in pipeline order.
DISCOVER = "discover"
BUILD = "build"
DEPLOY = "deploy"
CONFIRM = "confirm"
RECHECK = "recheck"
STEPS = (DISCOVER, BUILD, DEPLOY, CONFIRM, RECHECK)

# Device-level outcome (derived from the step records by `device_state`).
ST_NEW = "new"
ST_DISCOVERED = "discovered"
ST_BUILT = "built"
ST_PENDING_CONFIRM = "pending_confirm"
ST_CONFIRMED = "confirmed"
ST_ROLLBACK_PENDING = "rollback_pending"
ST_ROLLED_BACK = "rolled_back"
ST_ROLLBACK_UNKNOWN = "rollback_unknown"
ST_FAILED = "failed"
ST_UNSUPPORTED = "unsupported"
ST_REHEARSED = "rehearsed"
ST_REHEARSAL_FAILED = "rehearsal_failed"
ST_COMPLIANT = "compliant"
BAD_STATES = (ST_FAILED, ST_UNSUPPORTED, ST_ROLLBACK_UNKNOWN, ST_REHEARSAL_FAILED)


def rehearsal_key(step: str) -> str:
    return f"{step}_rehearsal"


def load_status(folder: Path) -> dict:
    try:
        data = json.loads((folder / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"devices": {}}
    if not isinstance(data, dict) or not isinstance(data.get("devices"), dict):
        return {"devices": {}}
    devices: dict[str, dict] = {}
    for device, records in data["devices"].items():
        if isinstance(records, dict):
            devices[device] = {k: r for k, r in records.items() if isinstance(r, dict)}
    return {"devices": devices}


def save_status(folder: Path, run_records: dict[str, dict]) -> str:
    """Merge this run's {device: {step: record}} into the file. Returns "" or the error."""
    path = folder / STATUS_FILE
    tmp = path.with_name(path.name + ".tmp")
    error = ""
    for attempt in range(3):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            merged = load_status(folder)
            for device, records in run_records.items():
                merged["devices"].setdefault(device, {}).update(records)
            tmp.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
            return ""
        except OSError as exc:
            error = str(exc)
            if attempt < 2:
                time.sleep(RETRY_DELAY)
    return error


def _rec(records: dict, step: str) -> dict | None:
    return records.get(step) or records.get(rehearsal_key(step))


def device_state(records: dict | None) -> tuple[str, str]:
    """(state, detail) for the board -- derived, never stored, so it can't drift."""
    if not records:
        return ST_NEW, "not started"
    live = {k: v for k, v in records.items() if not k.endswith("_rehearsal")}
    if not live:
        rehearsed = {k[: -len("_rehearsal")]: v for k, v in records.items() if k.endswith("_rehearsal")}
        state, detail = _state_from(rehearsed)
        if state in BAD_STATES:
            return ST_REHEARSAL_FAILED, f"rehearsal -- {detail}"
        return ST_REHEARSED, f"rehearsal -- {detail}"
    return _state_from(live)


def _state_from(live: dict) -> tuple[str, str]:
    recheck, confirm = live.get(RECHECK), live.get(CONFIRM)
    deploy, build, discover = live.get(DEPLOY), live.get(BUILD), live.get(DISCOVER)
    crashed = live.get("worker") or live.get("interrupted")
    if crashed and not (recheck or confirm):
        return ST_FAILED, crashed.get("reason", "worker failed")
    if recheck:
        return _recheck_state(recheck)
    if confirm:
        state, detail = _confirm_state(confirm)
        got_in = (discover or {}).get("credential", "")
        if got_in and got_in not in ("jlogin-default", "rancid"):
            detail += f" -- TACACS was down on this box, got in as {got_in}"
        return state, detail
    if deploy:
        if deploy.get("verdict") == "ok":
            return ST_PENDING_CONFIRM, f"commit confirmed, rollback due {deploy.get('rollback_due_at', '?')}"
        return ST_FAILED, f"deploy: {deploy.get('reason') or deploy.get('verdict')}"
    if build:
        if build.get("verdict") == "ok" and build.get("compliant"):
            return ST_COMPLIANT, "already matches the standard -- nothing sent"
        if build.get("verdict") == "ok":
            return ST_BUILT, f"{build.get('line_count', 0)} config line(s) ready"
        if build.get("verdict") == "unsupported":
            return ST_UNSUPPORTED, build.get("reason", "platform has no template")
        return ST_FAILED, f"build: {build.get('reason') or build.get('verdict')}"
    if discover:
        if discover.get("verdict") == "ok":
            return ST_DISCOVERED, f"{discover.get('platform', '?')} via {discover.get('credential', '?')}"
        return ST_FAILED, f"discover: {discover.get('reason') or discover.get('verdict')}"
    return ST_NEW, "not started"


def _confirm_state(confirm: dict) -> tuple[str, str]:
    verdict = confirm.get("verdict")
    if verdict == "ok":
        return ST_CONFIRMED, f"confirmed via {confirm.get('credential', '?')}"
    return ST_ROLLBACK_PENDING, (f"confirm failed ({confirm.get('reason') or verdict}) -- device "
                                 f"reverts at {confirm.get('rollback_due_at', '?')}")


def _recheck_state(recheck: dict) -> tuple[str, str]:
    verdict = recheck.get("verdict")
    if verdict == "ok":
        return ST_ROLLED_BACK, "rolled back -- old config verified on the box, fix by hand"
    if verdict == "too-early":
        return ST_ROLLBACK_PENDING, f"confirm failed -- {recheck.get('reason', 'rollback timer still running')}"
    if verdict == "still-applied":
        return ST_FAILED, "NEW config still on the box after the timer -- confirm it or roll back by hand"
    return ST_ROLLBACK_UNKNOWN, f"could not verify the rollback ({recheck.get('reason') or verdict})"


def done_live(records: dict | None, step: str) -> bool:
    """True when a LIVE run of `step` already came back ok for this device."""
    rec = (records or {}).get(step)
    return bool(rec and rec.get("live") and rec.get("verdict") == "ok")
