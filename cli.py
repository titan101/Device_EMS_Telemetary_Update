#!/usr/bin/env python3
"""Device EMS update -- command line.

    cli.py check                         jlogin, credentials and desired state all in order?
    cli.py new  RUN --file targets.txt   create a run (or --targets a,b,c)
    cli.py rehearse RUN --rancid-folder /mnt/.../configs   build from RANCID, no device contact
    cli.py fix  RUN --live --yes         the ISE fix on every device of the run (resumable)
    cli.py recheck RUN --live            after the timer: did the failed ones roll back?
    cli.py status RUN                    device board
    cli.py mop RUN                       MOP markdown + html
    cli.py preview --config-file X       the fix file one config dump would get

Nothing reaches a device without --live, and --live without --yes only prints the plan.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from core import builder, bulk, config_parser, credentials, desired, errorlog, ledger, mop, runs, session, status
from core import templates as tpl
from core.pipeline import RunContext

ROOT = Path(__file__).resolve().parent
RUNS_ROOT = ROOT / "runs"
CONFIG_DIR = ROOT / "config"
LEDGER_PATH = ROOT / "ledger.db"


# --------------------------------------------------------------------------- helpers
DESIRED_DEFAULT = "desired_state.default.json"


def _desired_path() -> Path:
    return CONFIG_DIR / desired.DESIRED_FILE


def ensure_default_config() -> bool:
    """First start: copy the shipped fleet standard to desired_state.json (not in git).
    Returns True when the copy was made."""
    target, default = _desired_path(), CONFIG_DIR / DESIRED_DEFAULT
    if target.exists() or not default.exists():
        return False
    shutil.copyfile(default, target)
    return True


def _load_desired(path: Path | None = None) -> desired.DesiredState:
    path = path or _desired_path()
    if path == _desired_path() and ensure_default_config():
        print(f"Created {path.name} from the shipped fleet standard -- fill in tacacs.secret and the SNMP "
              "community names (cli.py check lists what is left).", file=sys.stderr)
    if not path.exists():
        sys.exit(f"ERROR: {path} is missing and no {DESIRED_DEFAULT} to start from")
    try:
        return desired.load_desired(path)
    except desired.DesiredError as exc:
        sys.exit(f"ERROR: {exc}")


def _load_creds(path: Path | None = None) -> credentials.CredentialSet:
    try:
        return credentials.load_credentials(path or CONFIG_DIR / credentials.CREDENTIALS_FILE)
    except credentials.CredentialError as exc:
        sys.exit(f"ERROR: {exc}")


def _context(args: argparse.Namespace, run_id: str, run_dir: Path, meta: dict, live: bool) -> RunContext:
    des = _load_desired(Path(args.desired) if getattr(args, "desired", None) else None)
    commit = des.commit
    rancid_folder = Path(args.rancid_folder) if getattr(args, "rancid_folder", None) else None
    _, _, _, hints = runs.load_run(RUNS_ROOT, run_id)
    creds = _load_creds(Path(args.credentials) if getattr(args, "credentials", None) else None)
    return RunContext(
        run_id=run_id, run_dir=run_dir, desired=des, creds=creds, live=live, rancid_folder=rancid_folder,
        platform_hints=hints, cm_number=meta.get("cm_number", ""),
        confirmed_minutes=int(getattr(args, "confirmed_minutes", None) or commit["confirmed_minutes"]),
        confirm_delay=int(getattr(args, "delay", None) if getattr(args, "delay", None) is not None
                          else commit["confirm_delay_seconds"]),
        timeout=int(getattr(args, "timeout", None) or commit["timeout_seconds"]),
        comment=commit["comment"],
    )


def _recorder(run_dir: Path, run_id: str):
    """Main-thread writer: status.json (merge-on-save) + the fleet ledger. Never raises."""
    conn = ledger.connect(LEDGER_PATH)
    run_records: dict[str, dict] = {}
    warned = {"status": False}

    def record(device: str, step_key: str, rec: dict) -> None:
        run_records.setdefault(device, {})[step_key] = rec
        error = status.save_status(run_dir, run_records)
        if error and not warned["status"]:
            warned["status"] = True
            print(f"WARNING: could not write {status.STATUS_FILE} ({error}) -- the board will lag; this output is the record",
                  file=sys.stderr, flush=True)
        try:
            merged = status.load_status(run_dir)["devices"].get(device) or run_records[device]
            state, detail = status.device_state(merged)
            disc = merged.get(status.DISCOVER) or merged.get(status.rehearsal_key(status.DISCOVER)) or {}
            ledger.record_device(conn, device, state, detail, run_id, datetime.now().isoformat(timespec="seconds"),
                                 platform=disc.get("platform", ""), model=disc.get("model", ""),
                                 version=disc.get("version", ""), credential=rec.get("credential", ""))
        except Exception as exc:  # noqa: BLE001 -- the ledger is a convenience view
            errorlog.record(ROOT, f"ledger update {run_id} {device}", exc)

    return record


def _select(args: argparse.Namespace, devices: list[str]) -> list[str]:
    wanted = getattr(args, "devices", None)
    if not wanted:
        return devices
    names = [d.strip() for d in wanted.split(",") if d.strip()]
    unknown = [d for d in names if d not in devices]
    if unknown:
        sys.exit(f"ERROR: not in this run's targets: {', '.join(unknown)}")
    return [d for d in devices if d in names]


def _plan(ctx: RunContext, selected: list[str], skipped: dict[str, str], mode: str, workers: int) -> None:
    print(f"Run: {ctx.run_id}   Mode: {mode}   {'LIVE -- *** THIS CHANGES DEVICES ***' if ctx.live else 'DRY RUN -- no device will be contacted'}")
    print(f"Desired state: {ctx.desired.source}   commit confirmed {ctx.confirmed_minutes} min, comment {ctx.comment}, "
          f"confirm after {ctx.confirm_delay}s, session timeout {ctx.timeout}s")
    print(f"Credential ladder (discover/deploy): {', '.join(ctx.creds.labels('discover'))}")
    print(f"Credential ladder (confirm):         {', '.join(ctx.creds.labels('confirm'))}")
    if ctx.rancid_folder:
        print(f"RANCID folder: {ctx.rancid_folder}")
    print(f"Devices: {len(selected)} selected, {len(skipped)} skipped, up to {workers} at a time")
    for device, why in skipped.items():
        print(f"  SKIPPED {device} -- {why}")
    for device in selected:
        print(f"  {device}" + (f" ({ctx.platform_hints[device]})" if device in ctx.platform_hints else ""))


# --------------------------------------------------------------------------- commands
def cmd_check(args: argparse.Namespace) -> None:
    health = session.check_jlogin_health()
    print(f"jlogin:        {'OK' if health.ok else 'PROBLEM'} -- {health.message}")
    try:
        creds = credentials.load_credentials(CONFIG_DIR / credentials.CREDENTIALS_FILE)
        print(f"credentials:   discover ladder {creds.labels('discover')}, confirm ladder {creds.labels('confirm')}")
    except credentials.CredentialError as exc:
        print(f"credentials:   PROBLEM -- {exc}")
    path = _desired_path()
    if ensure_default_config():
        print(f"desired state: created {path.name} from the shipped fleet standard")
    if not path.exists():
        print(f"desired state: MISSING -- no {path.name} and no {DESIRED_DEFAULT} to start from")
        return
    try:
        des = desired.load_desired(path)
    except desired.DesiredError as exc:
        print(f"desired state: PROBLEM -- {exc}")
        return
    holes: list[str] = []
    for platform in tpl.TEMPLATE_FOR:
        d = des.for_platform(platform)
        holes += [h for h in des.placeholders(platform) if h not in holes]
        print(f"desired state: {platform:4} -> {len(d['tacacs']['servers'])} tacacs, {len(d['login']['classes'])} classes, "
              f"{len(d['login']['users'])} users, {len(d['ntp']['servers'])} ntp, {len(d['syslog']['hosts'])} syslog, "
              f"{len(d['snmp']['communities'])} communities")
    if holes:
        print(f"\nTO FILL in {path} ({len(holes)}):")
        for hole in holes:
            print(f"  - {hole}")
    else:
        print("\ndesired state: complete -- nothing left to fill")


def cmd_new(args: argparse.Namespace) -> None:
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.targets:
        text = "\n".join(args.targets.split(","))
    else:
        sys.exit("ERROR: give --file targets.txt or --targets a,b,c")
    try:
        devices, hints = runs.parse_targets(text)
        run_id = runs.safe_run_id(args.run)
        run_dir = runs.create_run(RUNS_ROOT, run_id, devices, hints, cm_number=args.cm or "", note=args.note or "")
    except runs.RunError as exc:
        sys.exit(f"ERROR: {exc}")
    conn = ledger.connect(LEDGER_PATH)
    ledger.record_run(conn, run_id, datetime.now().isoformat(timespec="seconds"), args.cm or "", args.note or "")
    print(f"Created run {run_id} with {len(devices)} device(s) at {run_dir}")
    print(f"Next: cli.py rehearse {run_id} --rancid-folder <folder>   or   cli.py fix {run_id} --live --yes")


def _run_mode(args: argparse.Namespace, mode: str, live: bool) -> None:
    try:
        run_dir, meta, devices, _ = runs.load_run(RUNS_ROOT, args.run)
    except runs.RunError as exc:
        sys.exit(f"ERROR: {exc}")
    ctx = _context(args, run_dir.name, run_dir, meta, live)
    if live and not session.jlogin_available():
        sys.exit(f"ERROR: {session.jlogin_binary()} not found on this host -- live runs need the box with jlogin")
    if not live and ctx.rancid_folder is None and mode != bulk.STEP_RECHECK:
        sys.exit("ERROR: a dry run reads the before-picture from RANCID -- pass --rancid-folder (or --live)")
    data = status.load_status(run_dir)
    selected, skipped = [], {}
    for device in _select(args, devices):
        recs = data["devices"].get(device) or {}
        why = _skip_reason(recs, mode, live, getattr(args, "redo", False))
        if why:
            skipped[device] = why
        else:
            selected.append(device)
    workers = getattr(args, "workers", None) or bulk.DEFAULT_WORKERS
    _plan(ctx, selected, skipped, mode, workers)
    if not selected:
        print("Nothing to do.")
        return
    if live and not getattr(args, "yes", False):
        print("\nPreview only -- nothing was sent. Re-run with --yes to open the sessions.")
        return
    sys.stdout.reconfigure(line_buffering=True)
    print()
    outcomes = bulk.run_devices(ctx, selected, mode, workers, _recorder(run_dir, run_dir.name), prior=data["devices"])
    _summary(run_dir, selected, skipped, live)


def _timer_running(recs: dict) -> str:
    """The rollback-due time if a live commit-confirmed is still counting down, else ''."""
    due = (recs.get(status.DEPLOY) or {}).get("rollback_due_at", "")
    try:
        return due if due and datetime.fromisoformat(due) > datetime.now() else ""
    except ValueError:
        return ""


def _skip_reason(recs: dict, mode: str, live: bool, redo: bool) -> str:
    state, _ = status.device_state(recs)
    if mode == bulk.STEP_RECHECK:
        if state not in (status.ST_ROLLBACK_PENDING, status.ST_ROLLBACK_UNKNOWN):
            return f"state is {state}, no rollback to check"
        due = _timer_running(recs)
        return f"rollback timer still running, due {due} -- recheck after that" if due else ""
    if not live:
        return ""
    if state == status.ST_ROLLBACK_PENDING and _timer_running(recs):
        # The failed change is still live on the box until the timer expires;
        # discovering it now would read the pending config as the real one.
        return f"rollback pending until {_timer_running(recs)} -- let the box revert, then recheck"
    if redo:
        return ""
    if status.done_live(recs, status.CONFIRM):
        return f"already confirmed live at {recs[status.CONFIRM].get('when')} -- pass --redo to repeat"
    build = recs.get(status.BUILD) or {}
    if build.get("live") and build.get("verdict") == "ok" and build.get("compliant"):
        return f"compliant at {build.get('when')} -- pass --redo to re-check"
    return ""


def _summary(run_dir: Path, selected: list[str], skipped: dict, live: bool) -> None:
    rows = [r for r in runs.device_states(run_dir, selected)]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    print("\n=== Summary: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) +
          (f", {len(skipped)} skipped" if skipped else "") + f" -- {len(selected)} device(s) ===")
    pending = [r for r in rows if r["state"] in (status.ST_ROLLBACK_PENDING,)]
    if pending:
        print("\nROLLBACK PENDING -- these boxes revert themselves when their timer expires; "
              f"then run: cli.py recheck {run_dir.name} --live")
        for row in pending:
            print(f"  {row['device']} -- {row['detail']}")
    bad = [r for r in rows if r["state"] in (status.ST_FAILED, status.ST_UNSUPPORTED, status.ST_ROLLBACK_UNKNOWN)]
    for row in bad:
        print(f"  {row['state'].upper()} {row['device']} -- {row['detail']}")
    print(f"Per-device record: {run_dir / status.STATUS_FILE}")
    if bad or pending:
        sys.exit(1)


def cmd_fix(args: argparse.Namespace) -> None:
    _run_mode(args, bulk.STEP_FULL, live=bool(args.live))


def cmd_rehearse(args: argparse.Namespace) -> None:
    _run_mode(args, bulk.STEP_FULL, live=False)


def cmd_discover(args: argparse.Namespace) -> None:
    _run_mode(args, bulk.STEP_DISCOVER, live=bool(args.live))


def cmd_build(args: argparse.Namespace) -> None:
    _run_mode(args, bulk.STEP_BUILD, live=bool(args.live))


def cmd_recheck(args: argparse.Namespace) -> None:
    if args.wait:
        _wait_for_timers(args.run)
    _run_mode(args, bulk.STEP_RECHECK, live=bool(args.live))


def _wait_for_timers(run: str) -> None:
    run_dir, _, devices, _ = runs.load_run(RUNS_ROOT, run)
    due = []
    for row in runs.device_states(run_dir, devices):
        if row["state"] == status.ST_ROLLBACK_PENDING:
            try:
                due.append(datetime.fromisoformat(row["records"][status.DEPLOY]["rollback_due_at"]))
            except (KeyError, ValueError):
                continue
    if not due:
        return
    remaining = (max(due) - datetime.now()).total_seconds() + 60
    if remaining > 0:
        print(f"Waiting {int(remaining)}s for the last rollback timer ({max(due):%H:%M}) ...", flush=True)
        time.sleep(remaining)


def cmd_status(args: argparse.Namespace) -> None:
    try:
        run_dir, meta, devices, _ = runs.load_run(RUNS_ROOT, args.run)
    except runs.RunError as exc:
        sys.exit(f"ERROR: {exc}")
    rows = runs.device_states(run_dir, devices)
    if args.json:
        print(json.dumps([{k: v for k, v in r.items() if k != "records"} for r in rows], indent=2))
        return
    if args.csv:
        path = Path(args.csv)
        runs.export_csv(run_dir, devices, path)
        print(f"Wrote {path}")
        return
    print(f"Run {run_dir.name}  CM {meta.get('cm_number') or '-'}  {len(devices)} device(s)")
    width = max((len(r["device"]) for r in rows), default=10)
    for row in rows:
        print(f"  {row['device']:{width}}  {row['state']:17}  {row['detail']}")
    counts = runs.state_counts(run_dir, devices)
    print("  " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))


def cmd_ledger(args: argparse.Namespace) -> None:
    conn = ledger.connect(LEDGER_PATH)
    rows = ledger.list_devices(conn, state=args.state or "", run_id=args.run or "")
    if args.export:
        import csv
        path = Path(args.export)
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["device", "platform", "model", "version", "state", "detail", "run_id", "credential", "updated_at"])
            for r in rows:
                w.writerow([r["device"], r["platform"], r["model"], r["version"], r["state"], r["detail"], r["run_id"],
                            r["credential"], r["updated_at"]])
        print(f"Wrote {len(rows)} row(s) to {path}")
        return
    counts = ledger.state_counts(conn)
    print("Fleet: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "empty"))
    for r in rows:
        print(f"  {r['device']:28} {r['platform'] or '?':4} {r['state']:17} {r['run_id']:22} {r['updated_at']}  {r['detail']}")


def cmd_mop(args: argparse.Namespace) -> None:
    try:
        run_dir, meta, devices, _ = runs.load_run(RUNS_ROOT, args.run)
    except runs.RunError as exc:
        sys.exit(f"ERROR: {exc}")
    des = _load_desired()
    md, html_path = mop.write_mop(run_dir, run_dir.name, meta, runs.device_states(run_dir, devices),
                                  des.commit["confirmed_minutes"], des.commit["comment"])
    print(f"Wrote {md}\nWrote {html_path}")


def cmd_templates(args: argparse.Namespace) -> None:
    for item in tpl.list_templates():
        print(f"{item['path']}  <- {', '.join(item['platforms']) or '(unused)'}")
        if args.show:
            print(item["text"])


def cmd_preview(args: argparse.Namespace) -> None:
    path = Path(args.config_file)
    text = path.read_text(encoding="utf-8", errors="replace")
    cfg = config_parser.parse_config(text)
    device = args.device or cfg.hostname or path.stem
    facts = config_parser.facts_from_show_version(device, text)
    if facts.platform == "unknown":
        facts = config_parser.facts_from_config(device, cfg, platform_hint=args.platform or "")
    elif args.platform:
        facts.platform, facts.platform_source = args.platform, "operator"
    facts.hostname = facts.hostname or cfg.hostname
    facts.version = facts.version or cfg.version
    des = _load_desired(Path(args.desired) if args.desired else None)
    try:
        result = builder.build(facts, cfg, des, protected_extra=[c.username for c in _load_creds().discover if c.username])
    except builder.BuildError as exc:
        sys.exit(f"ERROR: {exc}")
    print(builder.render_file(result, facts, "preview", "", f"file {path}"), end="")
    if result.compliant:
        print("# (compliant)", file=sys.stderr)


def cmd_hash_password(args: argparse.Namespace) -> None:
    import getpass
    password = args.password or getpass.getpass("Password to hash: ")
    try:
        from passlib.hash import sha512_crypt  # type: ignore[import-not-found]
        print(sha512_crypt.using(rounds=5000).hash(password))
        return
    except ImportError:
        pass
    if shutil.which("openssl"):
        out = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n", text=True,
                             capture_output=True)
        if out.returncode == 0 and out.stdout.strip():
            print(out.stdout.strip())
            return
    sys.exit("ERROR: no hasher available -- pip install passlib, or run on a host with openssl, "
             "or copy the encrypted-password line from a box where the user was set by hand")


# --------------------------------------------------------------------------- parser
def _add_run_args(p: argparse.ArgumentParser, live_flag: bool = True) -> None:
    p.add_argument("run")
    p.add_argument("--devices", help="comma-separated subset of the run's targets")
    p.add_argument("--workers", type=int, help=f"devices at a time (default {bulk.DEFAULT_WORKERS}, max {bulk.MAX_WORKERS})")
    p.add_argument("--rancid-folder", help="before-picture for a dry run (one display-set dump per device)")
    p.add_argument("--desired", help="alternative desired_state.json")
    p.add_argument("--credentials", help="alternative credentials.json")
    p.add_argument("--confirmed-minutes", type=int, help="commit confirmed timer (default from desired state)")
    p.add_argument("--delay", type=int, help="seconds between deploy and the confirm login")
    p.add_argument("--timeout", type=int, help="per-session timeout in seconds")
    p.add_argument("--redo", action="store_true", help="repeat devices already confirmed/compliant")
    if live_flag:
        p.add_argument("--live", action="store_true", help="open real jlogin sessions (default: rehearse)")
        p.add_argument("--yes", action="store_true", help="with --live: actually run instead of printing the plan")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="jlogin, credentials and desired state in order?").set_defaults(func=cmd_check)

    s = sub.add_parser("new", help="create a run from a target list")
    s.add_argument("run", help="run name -- the CM number is a good one")
    s.add_argument("--file", help="targets file: one device per line, optional ,platform")
    s.add_argument("--targets", help="comma-separated devices")
    s.add_argument("--cm", help="change-management number")
    s.add_argument("--note")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("fix", help="discover -> build -> commit confirmed -> second login + confirm")
    _add_run_args(s)
    s.set_defaults(func=cmd_fix)

    s = sub.add_parser("rehearse", help="same as fix without --live: RANCID before-picture, files only")
    _add_run_args(s, live_flag=False)
    s.set_defaults(func=cmd_rehearse)

    s = sub.add_parser("discover", help="only the read-only discovery session")
    _add_run_args(s)
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("build", help="discover + build the fix files, no deploy")
    _add_run_args(s)
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("recheck", help="after the timer: verify the rollback on devices whose confirm failed")
    _add_run_args(s)
    s.add_argument("--wait", action="store_true", help="sleep until the last rollback timer has expired")
    s.set_defaults(func=cmd_recheck)

    s = sub.add_parser("status", help="device board for a run")
    s.add_argument("run")
    s.add_argument("--json", action="store_true")
    s.add_argument("--csv", help="write the board to this CSV file")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("ledger", help="fleet ledger across runs")
    s.add_argument("--state")
    s.add_argument("--run")
    s.add_argument("--export", help="CSV path")
    s.set_defaults(func=cmd_ledger)

    s = sub.add_parser("mop", help="write MOP_<run>.md/.html")
    s.add_argument("run")
    s.set_defaults(func=cmd_mop)

    s = sub.add_parser("templates", help="list (and --show) the platform templates")
    s.add_argument("--show", action="store_true")
    s.set_defaults(func=cmd_templates)

    s = sub.add_parser("preview", help="render the fix file for one config dump, no run, no device")
    s.add_argument("--config-file", required=True)
    s.add_argument("--platform", choices=sorted(tpl.TEMPLATE_FOR))
    s.add_argument("--device")
    s.add_argument("--desired")
    s.set_defaults(func=cmd_preview)

    s = sub.add_parser("hash-password", help="sha512-crypt hash for login.users[].encrypted_password")
    s.add_argument("--password", help="(prompted if omitted)")
    s.set_defaults(func=cmd_hash_password)
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    context = " ".join(sys.argv[1:]) or args.command
    try:
        args.func(args)
    except SystemExit as exc:
        if exc.code not in (0, None):
            errorlog.record(ROOT, context, message=f"exited {exc.code}", level="FAILED")
        raise
    except KeyboardInterrupt:
        errorlog.record(ROOT, context, message="interrupted by operator", level="FAILED")
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 -- last resort, recorded then re-raised as an exit
        path = errorlog.record(ROOT, context, exc=exc)
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        if path:
            print(f"Full traceback recorded in {path}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
