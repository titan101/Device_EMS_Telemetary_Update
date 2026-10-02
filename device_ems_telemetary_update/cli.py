from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .credential_store import CredentialStoreError, load_credentials, save_credentials
from .models import CredentialProfile, DesiredState
from .reachability import parse_targets
from .reporting import write_reports
from .storage import load_run, save_run
from .template_engine import available_junos_templates
from .workflow import (
    audit_devices,
    build_configs,
    deploy_configs,
    ensure_device_records,
    run_discovery,
    run_rancid_discovery,
    summary_counts,
)


def _resolve_passphrase(args: argparse.Namespace) -> str:
    if args.passphrase_env:
        value = os.environ.get(args.passphrase_env)
        if not value:
            raise SystemExit(f"Environment variable {args.passphrase_env} is not set.")
        return value
    return getpass.getpass("Vault master passphrase: ")


def _print_device_table(run) -> None:
    if not run.targets:
        print("No targets on this run.")
        return
    header = f"{'target':<22}{'model':<14}{'type':<6}{'status':<30}{'error'}"
    print(header)
    print("-" * len(header))
    for target in run.targets:
        record = run.devices.get(target)
        if not record:
            continue
        print(f"{record.target:<22}{record.model:<14}{record.device_type:<6}{record.status:<30}{record.error[:60]}")


def cmd_templates(args: argparse.Namespace) -> None:
    for spec in available_junos_templates():
        platforms = ",".join(spec.platforms)
        device_types = ",".join(spec.device_types) or "any"
        print(f"{spec.template_id:18} platforms={platforms:14} device_types={device_types:10} {spec.label}")


def cmd_creds_add(args: argparse.Namespace) -> None:
    passphrase = _resolve_passphrase(args)
    password = getpass.getpass(f"Password for {args.username}: ")
    try:
        credentials = load_credentials(passphrase)
    except CredentialStoreError as exc:
        raise SystemExit(str(exc)) from exc
    credentials = [c for c in credentials if c.label != args.label]
    credentials.append(
        CredentialProfile(
            label=args.label,
            username=args.username,
            password=password,
            role=args.role,
            enabled=not args.disabled,
        )
    )
    save_credentials(credentials, passphrase)
    print(f"Saved credential profile '{args.label}'.")


def cmd_creds_list(args: argparse.Namespace) -> None:
    passphrase = _resolve_passphrase(args)
    try:
        credentials = load_credentials(passphrase)
    except CredentialStoreError as exc:
        raise SystemExit(str(exc)) from exc
    if not credentials:
        print("No credential profiles stored.")
        return
    for credential in credentials:
        print(f"{credential.label:20} user={credential.username:15} role={credential.role:10} enabled={credential.enabled}")


def cmd_creds_remove(args: argparse.Namespace) -> None:
    passphrase = _resolve_passphrase(args)
    try:
        credentials = load_credentials(passphrase)
    except CredentialStoreError as exc:
        raise SystemExit(str(exc)) from exc
    remaining = [c for c in credentials if c.label != args.label]
    if len(remaining) == len(credentials):
        raise SystemExit(f"No credential profile named '{args.label}'.")
    save_credentials(remaining, passphrase)
    print(f"Removed credential profile '{args.label}'.")


def cmd_targets(args: argparse.Namespace) -> None:
    run = load_run(args.change_id)
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    run.change_id = args.change_id
    run.targets = parse_targets(raw)
    if args.notes is not None:
        run.notes = args.notes
    ensure_device_records(run)
    save_run(run)
    print(f"Saved {len(run.targets)} target(s) to change {run.change_id}.")


def cmd_desired_show(args: argparse.Namespace) -> None:
    run = load_run(args.change_id)
    print(json.dumps(asdict(run.desired), indent=2))


def cmd_desired_set(args: argparse.Namespace) -> None:
    run = load_run(args.change_id)
    overrides = json.loads(Path(args.file).read_text(encoding="utf-8"))
    current = asdict(run.desired)
    current.update(overrides)
    run.desired = DesiredState(**current)
    ensure_device_records(run)
    save_run(run)
    print(f"Desired state updated for change {run.change_id}.")


def cmd_discover(args: argparse.Namespace) -> None:
    run = load_run(args.change_id)
    passphrase = _resolve_passphrase(args)
    credentials = load_credentials(passphrase)
    selected = args.targets.split(",") if args.targets else None
    run = run_discovery(run, credentials, selected)
    _print_device_table(run)


def cmd_discover_rancid(args: argparse.Namespace) -> None:
    run = load_run(args.change_id)
    run.change_id = args.change_id
    run = run_rancid_discovery(run, Path(args.folder), platform=args.platform, pattern=args.glob)
    _print_device_table(run)


def cmd_build(args: argparse.Namespace) -> None:
    run = load_run(args.change_id)
    selected = args.targets.split(",") if args.targets else None
    run = build_configs(run, selected)
    _print_device_table(run)


def cmd_deploy(args: argparse.Namespace) -> None:
    run = load_run(args.change_id)
    selected = args.targets.split(",") if args.targets else None
    dry_run = not args.live
    if not dry_run and args.confirm != run.change_id:
        raise SystemExit("Live deploy requires --confirm to exactly match the change id (safety gate).")
    passphrase = _resolve_passphrase(args)
    credentials = load_credentials(passphrase)
    run = deploy_configs(run, credentials, selected, dry_run=dry_run)
    _print_device_table(run)


def cmd_audit(args: argparse.Namespace) -> None:
    run = load_run(args.change_id)
    selected = args.targets.split(",") if args.targets else None
    passphrase = _resolve_passphrase(args)
    credentials = load_credentials(passphrase)
    run = audit_devices(run, credentials, selected, confirm_commit=args.confirm_commit, audit_only=not args.all_credentials)
    _print_device_table(run)


def cmd_report(args: argparse.Namespace) -> None:
    run = load_run(args.change_id)
    for name, path in write_reports(run).items():
        print(f"{name}: {path}")


def cmd_status(args: argparse.Namespace) -> None:
    run = load_run(args.change_id)
    counts = summary_counts(run)
    print(f"Change: {run.change_id}  Updated: {run.updated_at}")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    _print_device_table(run)


def _add_passphrase_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--passphrase-env",
        help="Env var holding the vault master passphrase. Prompts interactively if omitted.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="device-ems-cli", description="Headless CLI for Device EMS Telemetry Update.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("templates", help="List available Junos change templates and the platforms each one supports.")
    p.set_defaults(func=cmd_templates)

    p = sub.add_parser("creds-add", help="Add or replace a credential profile in the encrypted vault.")
    p.add_argument("--label", required=True)
    p.add_argument("--username", required=True)
    p.add_argument("--role", choices=["primary", "secondary", "audit"], default="secondary")
    p.add_argument("--disabled", action="store_true")
    _add_passphrase_arg(p)
    p.set_defaults(func=cmd_creds_add)

    p = sub.add_parser("creds-list", help="List credential profile labels in the vault.")
    _add_passphrase_arg(p)
    p.set_defaults(func=cmd_creds_list)

    p = sub.add_parser("creds-remove", help="Remove a credential profile from the vault.")
    p.add_argument("--label", required=True)
    _add_passphrase_arg(p)
    p.set_defaults(func=cmd_creds_remove)

    p = sub.add_parser("targets", help="Set the device target list for a change.")
    p.add_argument("change_id")
    p.add_argument("--file", required=True, help="Path to a text file of targets, one per line ('-' for stdin).")
    p.add_argument("--notes", default=None)
    p.set_defaults(func=cmd_targets)

    p = sub.add_parser("desired-show", help="Print the current desired state for a change as JSON.")
    p.add_argument("change_id")
    p.set_defaults(func=cmd_desired_show)

    p = sub.add_parser("desired-set", help="Apply desired-state overrides from a JSON file.")
    p.add_argument("change_id")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_desired_set)

    p = sub.add_parser("discover", help="Phase 1: ping and discover selected devices.")
    p.add_argument("change_id")
    p.add_argument("--targets", help="Comma-separated subset of targets. Defaults to all.")
    _add_passphrase_arg(p)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser(
        "discover-rancid",
        help="Phase 1 (offline): parse RANCID-style Junos 'display set' config dumps from a folder instead of live SSH.",
    )
    p.add_argument("change_id")
    p.add_argument("--folder", required=True, help="Directory with one config text file per router, named after the router hostname.")
    p.add_argument("--platform", default="mx", choices=["mx", "ex", "ptx"], help="Platform to assume for template selection.")
    p.add_argument("--glob", default="*", help="Filename glob to match within --folder.")
    p.set_defaults(func=cmd_discover_rancid)

    p = sub.add_parser("build", help="Phase 2: render candidate configs for selected devices.")
    p.add_argument("change_id")
    p.add_argument("--targets")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("deploy", help="Phase 3: deploy candidate configs (dry-run by default).")
    p.add_argument("change_id")
    p.add_argument("--targets")
    p.add_argument("--live", action="store_true", help="Open real device sessions and commit-confirm. Omit for a dry run.")
    p.add_argument("--confirm", help="Must exactly match change_id to arm a live deploy.")
    _add_passphrase_arg(p)
    p.set_defaults(func=cmd_deploy)

    p = sub.add_parser(
        "audit",
        help="Phase 4: log back in to confirm devices are reachable, optionally confirming the pending commit.",
    )
    p.add_argument("change_id")
    p.add_argument("--targets")
    p.add_argument("--confirm-commit", action="store_true")
    p.add_argument("--all-credentials", action="store_true", help="Try every enabled credential, not just audit-role ones.")
    _add_passphrase_arg(p)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("report", help="Phase 5: write CSV/JSON/Markdown reports for a change.")
    p.add_argument("change_id")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("status", help="Print summary counts and per-device status for a change.")
    p.add_argument("change_id")
    p.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
