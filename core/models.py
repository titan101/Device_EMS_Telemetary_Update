from __future__ import annotations

from dataclasses import dataclass, field

PLATFORM_TEMPLATES = {"mx": "mx", "acx": "mx", "ex": "ex", "srx": "srx"}
UNSUPPORTED_PLATFORMS = {"ptx", "qfx", "unknown"}


@dataclass
class DeviceFacts:
    device: str
    hostname: str = ""
    model: str = ""
    version: str = ""
    platform: str = "unknown"   # mx | acx | ex | srx | ptx | qfx | unknown
    platform_source: str = ""   # "show version" | "config heuristic" | "operator"
    dual_re: bool = False

    @property
    def template_family(self) -> str:
        return PLATFORM_TEMPLATES.get(self.platform, "")


@dataclass
class ExistingConfig:
    """What the device has today, as parsed from `display set` lines."""
    lines: list[str] = field(default_factory=list)
    hostname: str = ""
    version: str = ""
    tacacs_servers: list[str] = field(default_factory=list)
    tacacs_group: str = ""                 # apply-group that carries tacplus-server <*>
    tacacs_source_address: str = ""
    apply_groups: list[str] = field(default_factory=list)
    authentication_order: list[str] = field(default_factory=list)
    radius_servers: list[str] = field(default_factory=list)
    login_classes: dict[str, list[str]] = field(default_factory=dict)
    login_users: dict[str, dict] = field(default_factory=dict)   # name -> {class, uid, lines}
    ntp_servers: list[str] = field(default_factory=list)
    ntp_source_address: str = ""
    syslog_hosts: dict[str, list[str]] = field(default_factory=dict)
    syslog_source_address: str = ""
    snmp_communities: dict[str, list[str]] = field(default_factory=dict)
    snmp_trap_groups: dict[str, dict] = field(default_factory=dict)  # name -> {targets, lines}
    snmp_trap_source_address: str = ""
    snmp_contact: str = ""
    snmp_location: str = ""
    prefix_lists: dict[str, list[str]] = field(default_factory=dict)
    lo0_filter: str = ""
    lo0_address: str = ""
    fxp0_master_address: str = ""
    irb_mgmt_address: str = ""

    def lines_under(self, path: str) -> list[str]:
        """Every `set <path> ...` line (and `set <path>` itself)."""
        prefix = f"set {path} "
        exact = f"set {path}"
        return [l for l in self.lines if l.startswith(prefix) or l == exact]

    def has_path(self, path: str) -> bool:
        return bool(self.lines_under(path))


@dataclass
class BuildResult:
    device: str
    platform: str
    config_lines: list[str]
    verify_commands: list[str]
    rollback_lines: list[str]
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)   # `set <path>` must be gone after the change

    @property
    def compliant(self) -> bool:
        return not self.config_lines
