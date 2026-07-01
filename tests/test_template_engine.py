from device_ems_telemetary_update.adapters.junos import _parse_existing
from device_ems_telemetary_update.adapters.junos import parse_model_from_sysdescr, parse_version_from_sysdescr
from device_ems_telemetary_update.device_types import device_type_keys
from device_ems_telemetary_update.models import DesiredState, DeviceRecord, ExistingConfig, RunState
from device_ems_telemetary_update.reachability import parse_targets
from device_ems_telemetary_update.template_engine import (
    available_junos_templates,
    render_junos_config,
    selected_junos_templates,
)
from device_ems_telemetary_update.workflow import build_configs, write_fix_file


def test_parse_targets_deduplicates_common_input_forms():
    raw = "mx1\nmx2, 10.0.0.1\nmx1 # duplicate\n\n"
    assert parse_targets(raw) == ["mx1", "mx2", "10.0.0.1"]


def test_junos_template_deletes_legacy_and_sets_desired_state():
    record = DeviceRecord(
        target="mx1",
        model="MX480",
        existing=ExistingConfig(
            tacacs_servers=["192.0.2.10"],
            radius_servers=["192.0.2.20"],
            radius_delete_lines=["delete system radius-server 192.0.2.20"],
            login_users=["oldadmin"],
            login_user_delete_lines=["delete system login user oldadmin"],
            ntp_servers=["192.0.2.30"],
            syslog_hosts=["192.0.2.40"],
            snmp_communities=["oldpublic"],
        ),
    )
    desired = DesiredState(
        tacacs_servers=["198.51.100.10"],
        tacacs_secret="shared",
        login_users_to_delete=["oldadmin"],
        ntp_servers=["198.51.100.20"],
        syslog_hosts=["198.51.100.30"],
        snmp_communities=["netops-ro"],
        snmp_clients=["198.51.100.0/24"],
        cleanup_old_tacacs=True,
        cleanup_old_radius=True,
        cleanup_old_login_users=True,
        cleanup_old_ntp=True,
        cleanup_old_syslog=True,
        cleanup_old_snmp=True,
    )
    lines = render_junos_config(record, desired)
    assert "delete system tacplus-server 192.0.2.10" in lines
    assert "delete system radius-server 192.0.2.20" in lines
    assert "delete system login user oldadmin" in lines
    assert "set system tacplus-server 198.51.100.10 timeout 5" in lines
    assert 'set system tacplus-server 198.51.100.10 secret "shared"' in lines
    assert "set system services netconf ssh" in lines
    assert "set protocols lldp interface all" in lines


def test_ntp_template_keeps_servers_on_separate_lines_when_prefer_first():
    record = DeviceRecord(target="mx1", model="MX480")
    desired = DesiredState(
        selected_templates=["ntp_servers"],
        ntp_servers=["198.51.100.20", "198.51.100.21"],
    )

    lines = render_junos_config(record, desired)

    assert "set system ntp server 198.51.100.20 prefer" in lines
    assert "set system ntp server 198.51.100.21" in lines


def test_ptx_platform_is_hard_blocked_pending_validation():
    record = DeviceRecord(target="ptx1", model="PTX10008")
    desired = DesiredState()

    assert selected_junos_templates(desired, record) == []
    assert render_junos_config(record, desired) == []


def test_build_configs_flags_unsupported_platform_instead_of_silently_skipping(tmp_path, monkeypatch):
    from device_ems_telemetary_update import workflow

    monkeypatch.setattr(workflow, "FIX_FILES_DIR", tmp_path)
    monkeypatch.setattr(workflow, "save_run", lambda run: None)

    run = RunState(change_id="CHG999", targets=["ptx1"])
    run.devices["ptx1"] = DeviceRecord(target="ptx1", model="PTX10008", status="discovery_complete")

    build_configs(run)

    record = run.devices["ptx1"]
    assert record.status == "build_unsupported_platform"
    assert "ptx" in record.error.lower()
    assert record.generated_config == []
    assert record.fix_file_path == ""


def test_template_selection_limits_rendered_domains():
    record = DeviceRecord(
        target="ex1",
        model="EX3400",
        existing=ExistingConfig(
            tacacs_servers=["192.0.2.10"],
            snmp_communities=["oldpublic"],
        ),
    )
    desired = DesiredState(
        selected_templates=["snmp_communities"],
        tacacs_servers=["198.51.100.10"],
        snmp_communities=["netops-ro"],
        cleanup_old_tacacs=True,
        cleanup_old_snmp=True,
    )

    lines = render_junos_config(record, desired)

    assert "delete snmp community oldpublic" in lines
    assert "set snmp community netops-ro authorization read-only" in lines
    assert "delete system tacplus-server 192.0.2.10" not in lines
    assert "set system tacplus-server 198.51.100.10 timeout 5" not in lines


def test_available_junos_templates_are_auto_discovered():
    template_ids = {template.template_id for template in available_junos_templates()}
    assert {"tacacs_users", "snmp_communities", "ntp_servers", "syslog_hosts", "netconf_lldp"} <= template_ids


def test_parse_existing_collects_login_users_for_cleanup_review():
    existing = _parse_existing(
        {
            "login": "\n".join(
                [
                    "set system login user oldadmin uid 2001",
                    "set system login user oldadmin class super-user",
                    "set system login user noc class read-only",
                ]
            )
        }
    )

    assert existing.login_users == ["oldadmin", "noc"]
    assert existing.login_user_delete_lines == [
        "delete system login user oldadmin",
        "delete system login user noc",
    ]


def test_device_type_keys_include_exact_family_and_platform():
    assert device_type_keys("EX3400-24P") == ["ex340024p", "ex", "junos"]
    assert device_type_keys("MX480") == ["mx480", "mx", "junos"]


def test_snmp_sysdescr_parsers_extract_model_and_version():
    sys_descr = "Juniper Networks, Inc. ex3400-24p internet router, kernel JUNOS 21.4R3-S5.4"
    assert parse_model_from_sysdescr(sys_descr) == "EX3400-24P"
    assert parse_version_from_sysdescr(sys_descr) == "21.4R3-S5.4"


def test_write_fix_file_creates_reviewable_per_device_artifact(tmp_path, monkeypatch):
    from device_ems_telemetary_update import workflow

    monkeypatch.setattr(workflow, "FIX_FILES_DIR", tmp_path)
    record = DeviceRecord(
        target="192.0.2.10",
        hostname="edge-1",
        model="EX3400",
        device_type="ex",
        generated_config=["delete snmp community public", "set snmp community netops-ro authorization read-only"],
    )

    relative = write_fix_file("CHG123", record)
    path = tmp_path.parent / relative

    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "# Target: 192.0.2.10" in content
    assert "delete snmp community public" in content
