from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from device_ems_telemetary_update.credential_store import (
    CredentialStoreError,
    load_credentials,
    save_credentials,
)
from device_ems_telemetary_update.models import CredentialProfile, DesiredState, DeviceRecord, RunState
from device_ems_telemetary_update.reachability import parse_targets
from device_ems_telemetary_update.reporting import device_rows, write_reports
from device_ems_telemetary_update.storage import load_run, safe_change_id, save_run
from device_ems_telemetary_update.template_engine import available_junos_templates
from device_ems_telemetary_update.workflow import (
    audit_devices,
    build_configs,
    deploy_configs,
    ensure_device_records,
    run_discovery,
    summary_counts,
)


st.set_page_config(
    page_title="Device EMS Telemetry Update",
    page_icon="NET",
    layout="wide",
    initial_sidebar_state="expanded",
)


def inject_css() -> None:
    st.markdown(
        """
        <style>
        :root {
            --ops-bg: #0b0f14;
            --ops-panel: #111820;
            --ops-panel-2: #151f2a;
            --ops-field: #0c1219;
            --ops-ink: #eef4f8;
            --ops-muted: #95a7b7;
            --ops-line: #263544;
            --ops-accent: #35c2a2;
            --ops-accent-2: #67a7ff;
        }
        .stApp { background: linear-gradient(180deg, rgba(53, 194, 162, 0.08), transparent 260px), var(--ops-bg); color: var(--ops-ink); }
        .block-container { padding-top: 1.2rem; max-width: 1500px; }
        h1, h2, h3 { color: var(--ops-ink); letter-spacing: 0; }
        [data-testid="stHeader"] { background: rgba(11, 15, 20, 0.9); }
        [data-testid="stSidebar"] { background: var(--ops-panel); border-right: 1px solid var(--ops-line); }
        div[data-testid="stMetric"] {
            border: 1px solid var(--ops-line);
            border-radius: 8px;
            padding: 0.8rem 0.9rem;
            background: var(--ops-panel);
        }
        div[data-testid="stMetric"] label { color: var(--ops-muted); }
        div[data-testid="stMetricValue"] { color: var(--ops-accent); }
        div[data-testid="stTabs"] button { color: var(--ops-muted); }
        div[data-testid="stTabs"] button[aria-selected="true"] { color: var(--ops-ink); border-bottom-color: var(--ops-accent); }
        .stTextInput input, .stTextArea textarea, .stNumberInput input, .stSelectbox div[data-baseweb="select"] {
            background: var(--ops-field);
            border-color: var(--ops-line);
            color: var(--ops-ink);
        }
        .stDataFrame, div[data-testid="stExpander"] {
            border: 1px solid var(--ops-line);
            border-radius: 8px;
            overflow: hidden;
        }
        .phase-title {
            border-left: 4px solid var(--ops-accent);
            padding-left: 0.75rem;
            font-weight: 700;
            margin: 0.5rem 0 1rem 0;
            color: var(--ops-ink);
        }
        .small-muted { color: var(--ops-muted); font-size: 0.9rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def as_lines(values: list[str]) -> str:
    return "\n".join(values)


def lines_from_text(raw: str) -> list[str]:
    return parse_targets(raw)


def init_session() -> None:
    if "run" not in st.session_state:
        st.session_state.run = RunState(change_id="CHG000000")
    if "credentials" not in st.session_state:
        st.session_state.credentials = []
    if "vault_unlocked" not in st.session_state:
        st.session_state.vault_unlocked = False


def current_run() -> RunState:
    run = st.session_state.run
    ensure_device_records(run)
    return run


def save_current_run() -> None:
    save_run(current_run())


def device_selection(run: RunState, key: str) -> list[str]:
    choices = list(run.targets)
    return st.multiselect("Devices", choices, default=choices, key=key)


def metrics_row(run: RunState) -> None:
    counts = summary_counts(run)
    cols = st.columns(8)
    metrics = [
        ("Given", counts.get("given", 0)),
        ("Processed", counts.get("processed", 0)),
        ("Not Pingable", counts.get("not_pingable", 0)),
        ("Auth Fail", counts.get("auth_failures", 0)),
        ("Discovery", counts.get("discovery_completed", 0)),
        ("Loggable", counts.get("loggable", 0)),
        ("Deployed", counts.get("deployed", 0)),
        ("Failures", counts.get("failures", 0)),
    ]
    for col, (label, value) in zip(cols, metrics):
        col.metric(label, value)


def credentials_tab() -> None:
    st.markdown('<div class="phase-title">Credential Vault</div>', unsafe_allow_html=True)
    master = st.text_input("Master passphrase", type="password", key="master_passphrase")
    col_a, col_b = st.columns([1, 1])
    with col_a:
        if st.button("Unlock vault", use_container_width=True):
            try:
                st.session_state.credentials = load_credentials(master)
                st.session_state.vault_unlocked = True
                st.success("Vault unlocked.")
            except CredentialStoreError as exc:
                st.error(str(exc))
    with col_b:
        if st.button("Lock session", use_container_width=True):
            st.session_state.credentials = []
            st.session_state.vault_unlocked = False
            st.success("Session credentials cleared.")

    credentials: list[CredentialProfile] = st.session_state.credentials
    if credentials:
        public_rows = [
            {
                "label": c.label,
                "username": c.username,
                "role": c.role,
                "enabled": c.enabled,
            }
            for c in credentials
        ]
        st.dataframe(pd.DataFrame(public_rows), use_container_width=True, hide_index=True)

    with st.form("add_credential_form", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns([1.1, 1.1, 1.2, 0.8])
        label = c1.text_input("Label", placeholder="primary-admin")
        username = c2.text_input("Username")
        password = c3.text_input("Password", type="password")
        role = c4.selectbox("Role", ["primary", "secondary", "audit"])
        enabled = st.checkbox("Enabled", value=True)
        submitted = st.form_submit_button("Add or replace credential")
        if submitted:
            if not master:
                st.error("Enter the master passphrase before saving.")
            elif not label or not username or not password:
                st.error("Label, username, and password are required.")
            else:
                credentials = [credential for credential in credentials if credential.label != label]
                credentials.append(CredentialProfile(label=label, username=username, password=password, role=role, enabled=enabled))
                save_credentials(credentials, master)
                st.session_state.credentials = credentials
                st.session_state.vault_unlocked = True
                st.success(f"Saved credential profile {label}.")

    if credentials:
        remove_label = st.selectbox("Remove profile", [""] + [credential.label for credential in credentials])
        if remove_label and st.button("Remove selected profile"):
            credentials = [credential for credential in credentials if credential.label != remove_label]
            save_credentials(credentials, master)
            st.session_state.credentials = credentials
            st.success(f"Removed {remove_label}.")


def targets_tab(run: RunState) -> None:
    st.markdown('<div class="phase-title">Targets And Change</div>', unsafe_allow_html=True)
    col_a, col_b = st.columns([1, 2])
    with col_a:
        change_id = st.text_input("Change management number", value=run.change_id)
        if st.button("Load or create change", use_container_width=True):
            st.session_state.run = load_run(change_id)
            st.success(f"Loaded {change_id}.")
            st.rerun()
    with col_b:
        target_text = st.text_area("Device names or IPs", value=as_lines(run.targets), height=180)
    notes = st.text_area("Run notes", value=run.notes, height=90)
    parsed = lines_from_text(target_text)
    st.caption(f"{len(parsed)} unique targets parsed.")
    if st.button("Save target list", type="primary"):
        run.change_id = change_id.strip() or run.change_id
        run.targets = parsed
        run.notes = notes
        ensure_device_records(run)
        save_current_run()
        st.success("Targets saved.")


def desired_state_tab(run: RunState) -> None:
    st.markdown('<div class="phase-title">Desired EMS And Telemetry State</div>', unsafe_allow_html=True)
    desired = run.desired
    template_specs = available_junos_templates()
    template_label_to_id = {f"{spec.label} ({spec.template_id})": spec.template_id for spec in template_specs}
    selected_ids = set(desired.selected_templates)
    selected_labels = [
        label for label, template_id in template_label_to_id.items() if template_id in selected_ids
    ] or list(template_label_to_id)
    chosen_template_labels = st.multiselect(
        "Jinja change templates",
        list(template_label_to_id),
        default=selected_labels,
    )
    selected_templates = [template_label_to_id[label] for label in chosen_template_labels]
    with st.expander("Available Jinja templates", expanded=False):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "id": spec.template_id,
                        "label": spec.label,
                        "platforms": ", ".join(spec.platforms),
                        "device_types": ", ".join(spec.device_types) or "any",
                        "path": spec.path,
                        "description": spec.description,
                    }
                    for spec in template_specs
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )

    col_a, col_b, col_c = st.columns(3)
    tacacs_servers = col_a.text_area("TACACS server IPs", value=as_lines(desired.tacacs_servers), height=130)
    ntp_servers = col_b.text_area("NTP server IPs", value=as_lines(desired.ntp_servers), height=130)
    syslog_hosts = col_c.text_area("Syslog host IPs", value=as_lines(desired.syslog_hosts), height=130)

    col_d, col_e, col_f = st.columns(3)
    snmp_communities = col_d.text_area("SNMP communities", value=as_lines(desired.snmp_communities), height=110)
    snmp_clients = col_e.text_area("SNMP allowed clients", value=as_lines(desired.snmp_clients), height=110)
    tacacs_secret = col_f.text_input("TACACS shared secret", value=desired.tacacs_secret, type="password")
    discovery_snmp_communities = st.text_area(
        "SNMP discovery communities",
        value=as_lines(desired.discovery_snmp_communities),
        height=80,
    )
    legacy_login_users = st.text_area(
        "Legacy login users to delete",
        value=as_lines(desired.login_users_to_delete),
        height=80,
    )

    c1, c2, c3, c4 = st.columns(4)
    enable_netconf = c1.checkbox("Enable NETCONF SSH", value=desired.enable_netconf)
    enable_lldp = c2.checkbox("Enable LLDP all interfaces", value=desired.enable_lldp)
    auth_order = c3.checkbox("Auth order TACACS then local", value=desired.auth_order_tacacs_then_local)
    ntp_prefer_first = c4.checkbox("Prefer first NTP server", value=desired.ntp_prefer_first)

    clean_cols = st.columns(6)
    cleanup_old_tacacs = clean_cols[0].checkbox("Delete old TACACS", value=desired.cleanup_old_tacacs)
    cleanup_old_radius = clean_cols[1].checkbox("Delete old RADIUS", value=desired.cleanup_old_radius)
    cleanup_old_login_users = clean_cols[2].checkbox("Delete listed users", value=desired.cleanup_old_login_users)
    cleanup_old_ntp = clean_cols[3].checkbox("Delete old NTP", value=desired.cleanup_old_ntp)
    cleanup_old_syslog = clean_cols[4].checkbox("Delete old syslog", value=desired.cleanup_old_syslog)
    cleanup_old_snmp = clean_cols[5].checkbox("Delete old SNMP", value=desired.cleanup_old_snmp)

    t1, t2, t3, t4 = st.columns(4)
    tacacs_timeout = int(t1.number_input("TACACS timeout", min_value=1, max_value=60, value=int(desired.tacacs_timeout)))
    commit_confirm = int(t2.number_input("Commit confirmed minutes", min_value=1, max_value=120, value=int(desired.commit_confirm_minutes)))
    audit_wait = int(t3.number_input("Audit wait seconds", min_value=0, max_value=600, value=int(desired.audit_wait_seconds)))
    max_workers = int(t4.number_input("Parallel workers", min_value=1, max_value=64, value=int(desired.max_workers)))

    snmp_authorization = st.selectbox(
        "SNMP authorization",
        ["read-only", "read-write"],
        index=0 if desired.snmp_authorization == "read-only" else 1,
    )
    syslog_any_severity = st.selectbox(
        "Syslog any severity",
        ["any", "notice", "info", "warning", "error", "critical"],
        index=["any", "notice", "info", "warning", "error", "critical"].index(desired.syslog_any_severity)
        if desired.syslog_any_severity in ["any", "notice", "info", "warning", "error", "critical"]
        else 0,
    )

    if st.button("Save desired state", type="primary"):
        run.desired = DesiredState(
            selected_templates=selected_templates,
            discovery_snmp_communities=lines_from_text(discovery_snmp_communities),
            tacacs_servers=lines_from_text(tacacs_servers),
            tacacs_secret=tacacs_secret,
            tacacs_timeout=tacacs_timeout,
            auth_order_tacacs_then_local=auth_order,
            login_users_to_delete=lines_from_text(legacy_login_users),
            ntp_servers=lines_from_text(ntp_servers),
            ntp_prefer_first=ntp_prefer_first,
            syslog_hosts=lines_from_text(syslog_hosts),
            syslog_any_severity=syslog_any_severity,
            snmp_communities=lines_from_text(snmp_communities),
            snmp_clients=lines_from_text(snmp_clients),
            snmp_authorization=snmp_authorization,
            enable_netconf=enable_netconf,
            enable_lldp=enable_lldp,
            cleanup_old_tacacs=cleanup_old_tacacs,
            cleanup_old_radius=cleanup_old_radius,
            cleanup_old_login_users=cleanup_old_login_users,
            cleanup_old_ntp=cleanup_old_ntp,
            cleanup_old_syslog=cleanup_old_syslog,
            cleanup_old_snmp=cleanup_old_snmp,
            commit_confirm_minutes=commit_confirm,
            audit_wait_seconds=audit_wait,
            max_workers=max_workers,
        )
        save_current_run()
        st.success("Desired state saved.")


def discovery_tab(run: RunState) -> None:
    st.markdown('<div class="phase-title">Phase 1 Discovery</div>', unsafe_allow_html=True)
    selected = device_selection(run, "discovery_devices")
    if st.button("Ping and discover selected devices", type="primary"):
        if not st.session_state.credentials and not run.desired.discovery_snmp_communities:
            st.error("Unlock or add credentials first, or add SNMP discovery communities in Desired State.")
        else:
            with st.spinner("Running reachability and discovery..."):
                st.session_state.run = run_discovery(run, st.session_state.credentials, selected)
            st.success("Discovery complete.")
            st.rerun()


def build_tab(run: RunState) -> None:
    st.markdown('<div class="phase-title">Phase 2 Build And Review</div>', unsafe_allow_html=True)
    selected = device_selection(run, "build_devices")
    if st.button("Build configs for selected devices", type="primary"):
        with st.spinner("Building candidate configs..."):
            st.session_state.run = build_configs(run, selected)
        st.success("Build complete.")
        st.rerun()

    for target in selected or run.targets:
        record = run.devices.get(target)
        if not record:
            continue
        with st.expander(f"{target} - {record.status}", expanded=False):
            st.write(
                {
                    "hostname": record.hostname,
                    "model": record.model,
                    "device_type": record.device_type,
                    "version": record.version,
                    "existing_tacacs": record.existing.tacacs_servers,
                    "existing_radius": record.existing.radius_servers,
                    "existing_login_users": record.existing.login_users,
                    "existing_ntp": record.existing.ntp_servers,
                    "existing_syslog": record.existing.syslog_hosts,
                    "existing_snmp": record.existing.snmp_communities,
                    "selected_templates": run.desired.selected_templates,
                    "fix_file": record.fix_file_path,
                }
            )
            st.code("\n".join(record.generated_config) or "No generated config yet.", language="text")


def deploy_tab(run: RunState) -> None:
    st.markdown('<div class="phase-title">Phase 3 Deploy</div>', unsafe_allow_html=True)
    selected = device_selection(run, "deploy_devices")
    dry_run = st.checkbox("Dry run mode", value=True)
    reviewed = st.checkbox("Approval 1: generated configs have been reviewed")
    change_ok = st.checkbox("Approval 2: this change window is active")
    typed_change = st.text_input("Type change number to arm live deploy")
    live_armed = (not dry_run) and reviewed and change_ok and typed_change.strip() == run.change_id

    if dry_run:
        st.info("Dry run will mark selected devices reviewed without opening deployment sessions.")
    elif not live_armed:
        st.warning("Live deployment is locked.")

    deploy_label = "Run dry-run deploy" if dry_run else "Run live commit-confirmed deploy"
    if st.button(deploy_label, type="primary", disabled=(not dry_run and not live_armed)):
        if not st.session_state.credentials:
            st.error("Unlock or add credentials first.")
        else:
            with st.spinner("Deploy phase running..."):
                st.session_state.run = deploy_configs(run, st.session_state.credentials, selected, dry_run=dry_run)
            st.success("Deploy phase complete.")
            st.rerun()

    for target in selected or run.targets:
        record = run.devices.get(target)
        if not record:
            continue
        with st.expander(f"{target} - deploy result", expanded=False):
            st.write(record.deploy_result or record.status)
            if record.candidate_diff:
                st.code(record.candidate_diff, language="diff")
            if record.error:
                st.error(record.error)


def audit_tab(run: RunState) -> None:
    st.markdown('<div class="phase-title">Phase 4 Audit</div>', unsafe_allow_html=True)
    selected = device_selection(run, "audit_devices")
    audit_only = st.checkbox("Use audit-role credentials only when available", value=True)
    confirm_commit = st.checkbox("Confirm pending commit after audit login succeeds", value=False)
    if st.button("Run audit login", type="primary"):
        if not st.session_state.credentials:
            st.error("Unlock or add credentials first.")
        else:
            with st.spinner("Auditing device logins..."):
                st.session_state.run = audit_devices(
                    run,
                    st.session_state.credentials,
                    selected,
                    confirm_commit=confirm_commit,
                    audit_only=audit_only,
                )
            st.success("Audit complete.")
            st.rerun()


def reports_tab(run: RunState) -> None:
    st.markdown('<div class="phase-title">Phase 5 Documentation</div>', unsafe_allow_html=True)
    rows = device_rows(run)
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    if st.button("Generate report files", type="primary"):
        paths = write_reports(run)
        st.success("Reports generated.")
        for name, path in paths.items():
            st.write(f"{name}: `{path}`")
    report_stem = safe_change_id(run.change_id)
    paths = {
        "CSV": Path("data/reports") / f"{report_stem}_device_report.csv",
        "Markdown": Path("data/reports") / f"{report_stem}_management_summary.md",
        "JSON": Path("data/reports") / f"{report_stem}_run_state.json",
    }
    for label, relative_path in paths.items():
        path = Path(__file__).resolve().parent / relative_path
        if path.exists():
            st.download_button(
                f"Download {label}",
                data=path.read_bytes(),
                file_name=path.name,
                mime="text/plain",
            )


def device_table(run: RunState) -> None:
    rows = []
    for target in run.targets:
        record: DeviceRecord = run.devices.get(target, DeviceRecord(target=target))
        rows.append(
            {
                "target": target,
                "ping": record.pingable,
                "hostname": record.hostname,
                "model": record.model,
                "device_type": record.device_type,
                "version": record.version,
                "driver": record.driver,
                "status": record.status,
                "error": record.error,
            }
        )
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def logs_tab(run: RunState) -> None:
    st.markdown('<div class="phase-title">Console Logs</div>', unsafe_allow_html=True)
    all_lines = list(run.logs)
    for target in run.targets:
        record = run.devices.get(target)
        if not record:
            continue
        all_lines.extend(f"{target} | {line}" for line in record.logs)
    st.text_area("Run console", value="\n".join(all_lines[-1000:]), height=520)
    rows = []
    for target in run.targets:
        record = run.devices.get(target)
        if not record:
            continue
        rows.append(
            {
                "target": target,
                "status": record.status,
                "driver": record.driver,
                "model": record.model,
                "device_type": record.device_type,
                "fix_file": record.fix_file_path,
                "last_error": record.error,
                "log_entries": len(record.logs),
            }
        )
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def main() -> None:
    inject_css()
    init_session()
    run = current_run()

    st.title("Device EMS Telemetry Update")
    st.caption(f"Change: {run.change_id} | Last updated: {run.updated_at}")
    metrics_row(run)
    device_table(run)

    tabs = st.tabs(
        [
            "Credentials",
            "Targets",
            "Desired State",
            "Phase 1 Discovery",
            "Phase 2 Build",
            "Phase 3 Deploy",
            "Phase 4 Audit",
            "Phase 5 Reports",
            "Logs",
        ]
    )
    with tabs[0]:
        credentials_tab()
    with tabs[1]:
        targets_tab(run)
    with tabs[2]:
        desired_state_tab(run)
    with tabs[3]:
        discovery_tab(run)
    with tabs[4]:
        build_tab(run)
    with tabs[5]:
        deploy_tab(run)
    with tabs[6]:
        audit_tab(run)
    with tabs[7]:
        reports_tab(run)
    with tabs[8]:
        logs_tab(run)


if __name__ == "__main__":
    main()
