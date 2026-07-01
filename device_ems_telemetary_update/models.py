from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CredentialProfile:
    label: str
    username: str
    password: str
    role: str = "secondary"
    enabled: bool = True


@dataclass
class ExistingConfig:
    tacacs_servers: list[str] = field(default_factory=list)
    radius_servers: list[str] = field(default_factory=list)
    radius_delete_lines: list[str] = field(default_factory=list)
    login_users: list[str] = field(default_factory=list)
    login_user_delete_lines: list[str] = field(default_factory=list)
    ntp_servers: list[str] = field(default_factory=list)
    syslog_hosts: list[str] = field(default_factory=list)
    snmp_communities: list[str] = field(default_factory=list)
    netconf_enabled: bool = False
    lldp_enabled: bool = False
    raw_sections: dict[str, str] = field(default_factory=dict)


@dataclass
class DesiredState:
    selected_templates: list[str] = field(
        default_factory=lambda: [
            "tacacs_users",
            "snmp_communities",
            "ntp_servers",
            "syslog_hosts",
            "netconf_lldp",
        ]
    )
    discovery_snmp_communities: list[str] = field(default_factory=list)
    tacacs_servers: list[str] = field(default_factory=list)
    tacacs_secret: str = ""
    tacacs_timeout: int = 5
    auth_order_tacacs_then_local: bool = True
    login_users_to_delete: list[str] = field(default_factory=list)
    ntp_servers: list[str] = field(default_factory=list)
    ntp_prefer_first: bool = True
    syslog_hosts: list[str] = field(default_factory=list)
    syslog_any_severity: str = "any"
    snmp_communities: list[str] = field(default_factory=list)
    snmp_clients: list[str] = field(default_factory=list)
    snmp_authorization: str = "read-only"
    enable_netconf: bool = True
    enable_lldp: bool = True
    cleanup_old_tacacs: bool = True
    cleanup_old_radius: bool = True
    cleanup_old_login_users: bool = False
    cleanup_old_ntp: bool = False
    cleanup_old_syslog: bool = False
    cleanup_old_snmp: bool = False
    commit_confirm_minutes: int = 30
    audit_wait_seconds: int = 30
    max_workers: int = 8


@dataclass
class DeviceRecord:
    target: str
    vendor: str = "junos"
    address: str = ""
    pingable: bool = False
    ping_ms: float | None = None
    phase: str = "intake"
    status: str = "new"
    hostname: str = ""
    model: str = ""
    device_type: str = ""
    version: str = ""
    serial_number: str = ""
    driver: str = ""
    credential_label: str = ""
    auth_attempts: list[str] = field(default_factory=list)
    existing: ExistingConfig = field(default_factory=ExistingConfig)
    generated_config: list[str] = field(default_factory=list)
    fix_file_path: str = ""
    candidate_diff: str = ""
    deploy_result: str = ""
    audit_result: str = ""
    error: str = ""
    logs: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now)

    def log(self, message: str) -> None:
        self.logs.append(f"{utc_now()} {message}")
        self.updated_at = utc_now()


@dataclass
class RunState:
    change_id: str
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    targets: list[str] = field(default_factory=list)
    desired: DesiredState = field(default_factory=DesiredState)
    devices: dict[str, DeviceRecord] = field(default_factory=dict)
    notes: str = ""
    dry_run: bool = True
    logs: list[str] = field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = utc_now()

    def log(self, message: str) -> None:
        self.logs.append(f"{utc_now()} {message}")
        self.touch()


def _dataclass_from_dict(cls: type, data: dict[str, Any]):
    field_types = {field.name: field.type for field in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    clean = {}
    for name in cls.__dataclass_fields__:  # type: ignore[attr-defined]
        if name in data:
            clean[name] = data[name]
    if cls is DeviceRecord and isinstance(clean.get("existing"), dict):
        clean["existing"] = ExistingConfig(**clean["existing"])
    if cls is RunState:
        if isinstance(clean.get("desired"), dict):
            clean["desired"] = DesiredState(**clean["desired"])
        devices = {}
        for target, record in clean.get("devices", {}).items():
            if isinstance(record, DeviceRecord):
                devices[target] = record
            else:
                devices[target] = _dataclass_from_dict(DeviceRecord, record)
        clean["devices"] = devices
    return cls(**clean)


def run_to_dict(run: RunState) -> dict[str, Any]:
    return asdict(run)


def run_from_dict(data: dict[str, Any]) -> RunState:
    return _dataclass_from_dict(RunState, data)


def credential_to_public_dict(profile: CredentialProfile) -> dict[str, Any]:
    return {
        "label": profile.label,
        "username": profile.username,
        "role": profile.role,
        "enabled": profile.enabled,
        "password": "***" if profile.password else "",
    }
