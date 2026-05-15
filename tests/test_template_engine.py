from device_ems_telemetary_update.models import DesiredState, DeviceRecord, ExistingConfig
from device_ems_telemetary_update.reachability import parse_targets
from device_ems_telemetary_update.template_engine import render_junos_config


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
            ntp_servers=["192.0.2.30"],
            syslog_hosts=["192.0.2.40"],
            snmp_communities=["oldpublic"],
        ),
    )
    desired = DesiredState(
        tacacs_servers=["198.51.100.10"],
        tacacs_secret="shared",
        ntp_servers=["198.51.100.20"],
        syslog_hosts=["198.51.100.30"],
        snmp_communities=["netops-ro"],
        snmp_clients=["198.51.100.0/24"],
        cleanup_old_tacacs=True,
        cleanup_old_radius=True,
        cleanup_old_ntp=True,
        cleanup_old_syslog=True,
        cleanup_old_snmp=True,
    )
    lines = render_junos_config(record, desired)
    assert "delete system tacplus-server 192.0.2.10" in lines
    assert "delete system radius-server 192.0.2.20" in lines
    assert "set system tacplus-server 198.51.100.10 timeout 5" in lines
    assert 'set system tacplus-server 198.51.100.10 secret "shared"' in lines
    assert "set system services netconf ssh" in lines
    assert "set protocols lldp interface all" in lines
