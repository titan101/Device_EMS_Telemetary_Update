from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from .adapters import JunosAdapter
from .device_types import classify_junos_platform
from .models import CredentialProfile, DeviceRecord, RunState
from .reachability import ping_many
from .storage import FIX_FILES_DIR, safe_change_id, save_run
from .template_engine import render_junos_config, selected_junos_templates


STATUS_GROUPS = {
    "given": {"new", "pingable", "not_pingable", "discovery_complete", "auth_failed", "build_complete", "dry_run", "deployed_confirm_pending", "deploy_failed", "audit_passed", "audit_passed_commit_confirmed", "audit_failed"},
    "not_pingable": {"not_pingable"},
    "auth_failures": {"auth_failed"},
    "discovery_completed": {"discovery_complete", "build_complete", "dry_run", "deployed_confirm_pending", "audit_passed", "audit_passed_commit_confirmed"},
    "loggable": {"discovery_complete", "build_complete", "dry_run", "deployed_confirm_pending", "audit_passed", "audit_passed_commit_confirmed"},
    "built": {"build_complete", "dry_run", "deployed_confirm_pending", "audit_passed", "audit_passed_commit_confirmed"},
    "deployed": {"deployed_confirm_pending", "audit_passed", "audit_passed_commit_confirmed"},
    "failures": {"not_pingable", "auth_failed", "deploy_failed", "audit_failed", "build_required", "build_unsupported_platform"},
}


def get_adapter(vendor: str):
    if vendor.lower() == "junos":
        return JunosAdapter()
    raise ValueError(f"Unsupported vendor: {vendor}")


def ensure_device_records(run: RunState) -> None:
    for target in run.targets:
        run.devices.setdefault(target, DeviceRecord(target=target))
    for target in list(run.devices):
        if target not in run.targets:
            del run.devices[target]


def selected_or_all(run: RunState, selected: list[str] | None) -> list[str]:
    ensure_device_records(run)
    if selected:
        return [target for target in selected if target in run.devices]
    return list(run.targets)


def run_discovery(run: RunState, credentials: list[CredentialProfile], selected: list[str] | None = None) -> RunState:
    ensure_device_records(run)
    targets = selected_or_all(run, selected)
    if not targets:
        return run
    max_workers = max(1, run.desired.max_workers)
    run.log(f"Discovery starting for {len(targets)} target(s) with {max_workers} worker thread(s).")
    ping_results = ping_many(targets, timeout_ms=1200, max_workers=max_workers)
    for target, (reachable, latency, output) in ping_results.items():
        record = run.devices[target]
        record.pingable = reachable
        record.ping_ms = latency
        record.phase = "reachability"
        record.status = "pingable" if reachable else "not_pingable"
        record.error = "" if reachable else "Ping failed."
        record.log("Ping passed." if reachable else f"Ping failed. {output[:220]}")
    reachable_targets = [target for target in targets if run.devices[target].pingable]
    adapter = JunosAdapter()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(adapter.discover, target, credentials, run.desired): target for target in reachable_targets}
        for future in as_completed(future_map):
            target = future_map[future]
            try:
                record = future.result()
            except Exception as exc:
                record = run.devices[target]
                record.phase = "discovery"
                record.status = "discovery_failed"
                record.error = str(exc)
                record.log(f"Unhandled discovery failure: {exc}")
            record.pingable = run.devices[target].pingable
            record.ping_ms = run.devices[target].ping_ms
            run.devices[target] = record
            run.log(f"Discovery finished for {target}: {record.status} model={record.model or 'unknown'} driver={record.driver or 'none'}.")
    run.log("Discovery phase complete.")
    save_run(run)
    return run


def build_configs(run: RunState, selected: list[str] | None = None) -> RunState:
    ensure_device_records(run)
    targets = selected_or_all(run, selected)
    run.log(f"Build starting for {len(targets)} target(s).")
    for target in targets:
        record = run.devices[target]
        if record.status not in {"discovery_complete", "build_complete", "dry_run", "deployed_confirm_pending", "audit_passed", "audit_passed_commit_confirmed"}:
            record.status = "build_required"
            record.error = "Discovery must complete before build."
            record.log("Build skipped because discovery is incomplete.")
            continue
        try:
            if record.vendor == "junos":
                if not selected_junos_templates(run.desired, record):
                    platform = classify_junos_platform(record.model)
                    record.fix_file_path = ""
                    record.phase = "build"
                    record.status = "build_unsupported_platform"
                    record.error = (
                        f"No change templates support platform '{platform}' (model '{record.model or 'unknown'}'). "
                        "This platform has not been validated against a real config sample yet."
                    )
                    record.log(record.error)
                    run.log(f"Build skipped for {target}: {record.error}")
                    continue
                record.generated_config = render_junos_config(record, run.desired)
                record.fix_file_path = write_fix_file(run.change_id, record)
                record.phase = "build"
                record.status = "build_complete"
                record.error = ""
                record.log(f"Generated {len(record.generated_config)} Junos set/delete lines in {record.fix_file_path}.")
                run.log(f"Build finished for {target}: {len(record.generated_config)} lines -> {record.fix_file_path}.")
            else:
                raise ValueError(f"Unsupported vendor: {record.vendor}")
        except Exception as exc:
            record.status = "build_failed"
            record.error = str(exc)
            record.log(f"Build failed: {exc}")
            run.log(f"Build failed for {target}: {exc}")
    run.log("Build phase complete.")
    save_run(run)
    return run


def deploy_configs(
    run: RunState,
    credentials: list[CredentialProfile],
    selected: list[str] | None = None,
    dry_run: bool = True,
) -> RunState:
    ensure_device_records(run)
    run.dry_run = dry_run
    targets = selected_or_all(run, selected)
    max_workers = max(1, run.desired.max_workers)
    run.log(
        f"{'Dry-run' if dry_run else 'Live'} deploy starting for {len(targets)} target(s) with {max_workers} worker thread(s)."
    )
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {}
        for target in targets:
            record = run.devices[target]
            adapter = get_adapter(record.vendor)
            future = executor.submit(adapter.deploy, record, credentials, run.desired, run.change_id, dry_run)
            future_map[future] = target
        for future in as_completed(future_map):
            target = future_map[future]
            run.devices[target] = future.result()
            record = run.devices[target]
            run.log(f"Deploy finished for {target}: {record.status} {record.deploy_result or record.error}")
    run.log("Deploy phase complete.")
    save_run(run)
    return run


def audit_devices(
    run: RunState,
    credentials: list[CredentialProfile],
    selected: list[str] | None = None,
    confirm_commit: bool = False,
    audit_only: bool = True,
) -> RunState:
    ensure_device_records(run)
    targets = selected_or_all(run, selected)
    max_workers = max(1, run.desired.max_workers)
    run.log(f"Audit starting for {len(targets)} target(s) with {max_workers} worker thread(s).")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {}
        for target in targets:
            record = run.devices[target]
            adapter = get_adapter(record.vendor)
            future = executor.submit(adapter.audit, record, credentials, run.desired, run.change_id, confirm_commit, audit_only)
            future_map[future] = target
        for future in as_completed(future_map):
            target = future_map[future]
            run.devices[target] = future.result()
            record = run.devices[target]
            run.log(f"Audit finished for {target}: {record.status} {record.audit_result or record.error}")
    run.log("Audit phase complete.")
    save_run(run)
    return run


def summary_counts(run: RunState) -> dict[str, int]:
    ensure_device_records(run)
    statuses = [record.status for record in run.devices.values()]
    counts: dict[str, int] = {
        "given": len(run.targets),
        "processed": sum(1 for status in statuses if status != "new"),
    }
    for name, grouped_statuses in STATUS_GROUPS.items():
        if name == "given":
            continue
        counts[name] = sum(1 for status in statuses if status in grouped_statuses)
    return counts


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned or "device"


def write_fix_file(change_id: str, record: DeviceRecord) -> str:
    change_dir = FIX_FILES_DIR / safe_change_id(change_id)
    change_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_slug(record.hostname or record.target)}.set"
    path = change_dir / filename
    header = [
        f"# Change: {change_id}",
        f"# Target: {record.target}",
        f"# Hostname: {record.hostname or 'unknown'}",
        f"# Model: {record.model or 'unknown'}",
        f"# Device type: {record.device_type or 'unknown'}",
        "# Review this file before live deployment.",
        "",
    ]
    path.write_text("\n".join(header + record.generated_config) + "\n", encoding="utf-8")
    return str(path.relative_to(FIX_FILES_DIR.parent)).replace("\\", "/")
