from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import RunState, run_to_dict
from .storage import REPORTS_DIR, safe_change_id
from .workflow import summary_counts


def device_rows(run: RunState) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for target in run.targets:
        record = run.devices.get(target)
        if not record:
            continue
        rows.append(
            {
                "target": record.target,
                "hostname": record.hostname,
                "model": record.model,
                "version": record.version,
                "pingable": str(record.pingable),
                "driver": record.driver,
                "credential_label": record.credential_label,
                "phase": record.phase,
                "status": record.status,
                "existing_tacacs": ", ".join(record.existing.tacacs_servers),
                "existing_radius": ", ".join(record.existing.radius_servers),
                "existing_ntp": ", ".join(record.existing.ntp_servers),
                "existing_syslog": ", ".join(record.existing.syslog_hosts),
                "existing_snmp": ", ".join(record.existing.snmp_communities),
                "generated_lines": str(len(record.generated_config)),
                "deploy_result": record.deploy_result,
                "audit_result": record.audit_result,
                "error": record.error,
            }
        )
    return rows


def write_reports(run: RunState) -> dict[str, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = safe_change_id(run.change_id)
    csv_path = REPORTS_DIR / f"{stem}_device_report.csv"
    json_path = REPORTS_DIR / f"{stem}_run_state.json"
    md_path = REPORTS_DIR / f"{stem}_management_summary.md"

    rows = device_rows(run)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["target"])
        writer.writeheader()
        writer.writerows(rows)

    json_path.write_text(json.dumps(run_to_dict(run), indent=2), encoding="utf-8")

    counts = summary_counts(run)
    lines = [
        f"# EMS Telemetry Update Summary - {run.change_id}",
        "",
        f"- Devices given: {counts.get('given', 0)}",
        f"- Devices processed: {counts.get('processed', 0)}",
        f"- Discovery completed: {counts.get('discovery_completed', 0)}",
        f"- Auth failures: {counts.get('auth_failures', 0)}",
        f"- Not pingable: {counts.get('not_pingable', 0)}",
        f"- Configs built: {counts.get('built', 0)}",
        f"- Configs deployed: {counts.get('deployed', 0)}",
        f"- Failures: {counts.get('failures', 0)}",
        "",
        "## Desired State",
        "",
        f"- TACACS servers: {', '.join(run.desired.tacacs_servers) or 'none'}",
        f"- NTP servers: {', '.join(run.desired.ntp_servers) or 'none'}",
        f"- Syslog hosts: {', '.join(run.desired.syslog_hosts) or 'none'}",
        f"- SNMP communities: {', '.join(run.desired.snmp_communities) or 'none'}",
        f"- NETCONF enabled: {run.desired.enable_netconf}",
        f"- LLDP enabled: {run.desired.enable_lldp}",
        "",
        "## Device Results",
        "",
        "| Target | Hostname | Model | Version | Status | Error |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        error = row["error"].replace("|", "/")
        lines.append(
            f"| {row['target']} | {row['hostname']} | {row['model']} | {row['version']} | {row['status']} | {error} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"csv": csv_path, "json": json_path, "markdown": md_path}
