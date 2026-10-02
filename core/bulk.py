"""Parallel, resumable runner over many devices.

Worker threads run the per-device steps and push (device, step, record) events
onto a queue. The main thread drains that queue, prints, writes status.json and
the ledger -- so nothing shared is ever written from two threads. Ctrl-C
cancels every queued device; a device mid-session finishes its current step
and is recorded, never silently dropped.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import pipeline, status
from .pipeline import DeviceWork, RunContext

HEARTBEAT = 15
DEFAULT_WORKERS = 8
MAX_WORKERS = 10

STEP_FULL = "fix"          # discover -> build -> deploy -> confirm
STEP_REHEARSE = "rehearse"  # discover -> build (-> rehearse deploy+confirm command files)
STEP_DISCOVER = "discover"
STEP_BUILD = "build"
STEP_RECHECK = "recheck"


@dataclass
class DeviceOutcome:
    device: str
    final_step: str = ""
    final_record: dict = field(default_factory=dict)
    error: str = ""
    seconds: float = 0.0


Event = tuple[str, str, dict]
Recorder = Callable[[str, str, dict], None]


def _emit(events: queue.Queue, device: str, step: str, rec: dict) -> None:
    events.put((device, step, rec))


def _step_key(ctx: RunContext, step: str) -> str:
    return step if ctx.live else status.rehearsal_key(step)


def device_sequence(ctx: RunContext, device: str, mode: str, events: queue.Queue,
                    prior: dict | None = None) -> DeviceOutcome:
    """Worker body: runs the steps for one device, emitting a record after each."""
    started = time.monotonic()
    outcome = DeviceOutcome(device=device)
    work = DeviceWork(device=device)
    prior = prior or {}
    try:
        if mode == STEP_RECHECK:
            due = (prior.get(status.DEPLOY) or {}).get("rollback_due_at", "")
            rec = pipeline.recheck(ctx, work, due)
            _emit(events, device, _step_key(ctx, status.RECHECK), rec)
            outcome.final_step, outcome.final_record = status.RECHECK, rec
            return outcome
        rec = pipeline.discover(ctx, work)
        _emit(events, device, _step_key(ctx, status.DISCOVER), rec)
        outcome.final_step, outcome.final_record = status.DISCOVER, rec
        if rec["verdict"] != "ok" or mode == STEP_DISCOVER:
            return outcome
        rec = pipeline.build(ctx, work)
        _emit(events, device, _step_key(ctx, status.BUILD), rec)
        outcome.final_step, outcome.final_record = status.BUILD, rec
        if rec["verdict"] != "ok" or mode == STEP_BUILD or rec.get("compliant"):
            return outcome
        rec = pipeline.deploy(ctx, work)
        _emit(events, device, _step_key(ctx, status.DEPLOY), rec)
        outcome.final_step, outcome.final_record = status.DEPLOY, rec
        if rec["verdict"] != "ok":
            return outcome
        rec = pipeline.confirm(ctx, work, rollback_due_at=rec.get("rollback_due_at", ""))
        _emit(events, device, _step_key(ctx, status.CONFIRM), rec)
        outcome.final_step, outcome.final_record = status.CONFIRM, rec
    except Exception as exc:  # one device's surprise must not take the others down
        outcome.error = f"{type(exc).__name__}: {exc}"
        rec = {"step": outcome.final_step or "worker", "live": ctx.live, "verdict": "failed",
               "reason": outcome.final_step and f"crashed after {outcome.final_step}: {outcome.error}" or outcome.error,
               "when": datetime.now().isoformat(timespec="seconds"), "log": "", "credential": ""}
        _emit(events, device, _step_key(ctx, "worker"), rec)
        outcome.final_record = rec
    finally:
        outcome.seconds = time.monotonic() - started
    return outcome


def _line(device: str, step: str, rec: dict) -> str:
    tag = "OK" if rec.get("verdict") == "ok" else rec.get("verdict", "?").upper()
    extra = rec.get("reason") or ""
    if step.startswith(status.BUILD) and rec.get("verdict") == "ok":
        extra = "COMPLIANT -- nothing to send" if rec.get("compliant") else f"{rec.get('line_count', 0)} line(s) -> {rec.get('file')}"
    if step.startswith(status.DISCOVER) and rec.get("verdict") == "ok":
        extra = f"{rec.get('platform')} {rec.get('model') or ''} via {rec.get('credential')}".strip()
    if step.startswith(status.DEPLOY) and rec.get("verdict") == "ok":
        extra = f"commit confirmed {rec.get('confirmed_minutes')} -- reverts at {rec.get('rollback_due_at')} unless confirmed"
    if step.startswith(status.CONFIRM) and rec.get("verdict") == "ok":
        extra = f"confirmed via {rec.get('credential')}" + (f" -- {rec['verify']}" if rec.get("verify") else "")
    return f"[{tag}] {device} {step}: {extra}".rstrip(": ")


def run_devices(ctx: RunContext, devices: list[str], mode: str, workers: int, record: Recorder,
                prior: dict[str, dict] | None = None, out=None) -> dict[str, DeviceOutcome]:
    """Main-thread loop. `record(device, step_key, rec)` is called for every event, here only."""
    out = out or sys.stdout
    workers = max(1, min(int(workers), MAX_WORKERS))
    events: queue.Queue = queue.Queue()
    print_lock = threading.RLock()
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {pool.submit(device_sequence, ctx, d, mode, events, (prior or {}).get(d)): d for d in devices}
    pending = set(futures)
    outcomes: dict[str, DeviceOutcome] = {}
    total, finished = len(devices), 0
    phase_started = time.monotonic()
    print(f"--- {mode}: {total} device(s), up to {workers} at a time, {'LIVE' if ctx.live else 'DRY RUN'} ---",
          file=out, flush=True)

    def drain() -> None:
        while True:
            try:
                device, step, rec = events.get_nowait()
            except queue.Empty:
                return
            with print_lock:
                record(device, step, rec)
                print(_line(device, step, rec), file=out, flush=True)

    try:
        while pending:
            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            drain()
            for fut in done:
                outcome = fut.result()
                outcomes[outcome.device] = outcome
                finished += 1
                with print_lock:
                    print(f"    -- {outcome.device} done in {outcome.seconds:.0f}s ({finished}/{total} finished)",
                          file=out, flush=True)
            if not done and time.monotonic() - phase_started > HEARTBEAT and int(time.monotonic()) % HEARTBEAT == 0:
                in_session = sorted(futures[f] for f in pending if f.running())
                queued = len(pending) - len(in_session)
                with print_lock:
                    print(f"... {int(time.monotonic() - phase_started)}s: {len(in_session)} in session: "
                          f"{', '.join(in_session) or '(none)'}" + (f"; {queued} queued" if queued else ""),
                          file=out, flush=True)
        drain()
        pool.shutdown(wait=True)
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        drain()
        in_session = sorted(futures[f] for f in pending if f.running())
        queued = sorted(futures[f] for f in pending if not f.running() and not f.done())
        with print_lock:
            print(f"\nINTERRUPTED -- {len(queued)} queued device(s) never started: {', '.join(queued) or '(none)'}",
                  file=sys.stderr)
            for device in in_session:
                rec = {"step": "interrupted", "live": ctx.live, "verdict": "failed",
                       "reason": "interrupted (Ctrl-C) while a session was open -- outcome unknown, check the box by hand",
                       "when": datetime.now().isoformat(timespec="seconds"), "log": "", "credential": ""}
                record(device, _step_key(ctx, "interrupted"), rec)
                print(f"  {device} -- session was open, outcome unknown", file=sys.stderr)
        raise
    return outcomes
