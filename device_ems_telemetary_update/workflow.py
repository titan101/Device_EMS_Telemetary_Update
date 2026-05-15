from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from .adapters import JunosAdapter
from .models import CredentialProfile, DeviceRecord, RunState
from .reachability import ping_many
from .storage import save_run
from .template_engine import render_junos_config


STATUS_GROUPS = {
    "given": {"new", "pingable", "not_pingable", "discovery_complete", "auth_failed", "build_complete", "dry_run", "deployed_confirm_pending", "deploy_failed", "audit_passed", "audit_passed_commit_confirmed", "audit_failed"},
    "not_pingable": {"not_pingable"},
    "auth_failures": {"auth_failed"},
    "discovery_completed": {"discovery_complete", "build_complete", "dry_run", "deployed_confirm_pending", "audit_passed", "audit_passed_commit_confirmed"},
    "loggable": {"discovery_complete", "build_complete", "dry_run", "deployed_confirm_pending", "audit_passed", "audit_passed_commit_confirmed"},
    "built": {"build_complete", "dry_run", "deployed_confirm_pending", "audit_passed", "audit_passed_commit_confirmed"},
    "deployed": {"deployed_confirm_pending", "audit_passed", "audit_passed_commit_confirmed"},
    "failures": {"not_pingable", "auth_failed", "deploy_failed", "audit_failed", "build_required"},
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
        future_map = {executor.submit(adapter.discover, target, credentials): target for target in reachable_targets}
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
    save_run(run)
    return run


def build_configs(run: RunState, selected: list[str] | None = None) -> RunState:
    ensure_device_records(run)
    targets = selected_or_all(run, selected)
    for target in targets:
        record = run.devices[target]
        if record.status not in {"discovery_complete", "build_complete", "dry_run", "deployed_confirm_pending", "audit_passed", "audit_passed_commit_confirmed"}:
            record.status = "build_required"
            record.error = "Discovery must complete before build."
            record.log("Build skipped because discovery is incomplete.")
            continue
        try:
            if record.vendor == "junos":
                record.generated_config = render_junos_config(record, run.desired)
                record.phase = "build"
                record.status = "build_complete"
                record.error = ""
                record.log(f"Generated {len(record.generated_config)} Junos set/delete lines.")
            else:
                raise ValueError(f"Unsupported vendor: {record.vendor}")
        except Exception as exc:
            record.status = "build_failed"
            record.error = str(exc)
            record.log(f"Build failed: {exc}")
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
