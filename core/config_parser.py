"""`show configuration | display set` text -> ExistingConfig, plus platform identification.

Works on a jlogin transcript (prompts, banners and echoed commands mixed in)
and on a RANCID dump alike: only `set ...` lines are read, every regex anchors
on the full line prefix, and nothing is split by section.
"""
from __future__ import annotations

import re

from .models import DeviceFacts, ExistingConfig

_SET_RE = re.compile(r"^set (.+)$")
_VERSION_RE = re.compile(r"^set version (\S+)")
_HOSTNAME_RE = re.compile(r"^set system host-name (\S+)")
_TACPLUS_RE = re.compile(r"^set system tacplus-server (\S+)")
_TACPLUS_GROUP_RE = re.compile(r"^set groups (\S+) system tacplus-server <\*>")
_TACPLUS_SRC_RE = re.compile(r"^set (?:groups \S+ )?system tacplus-server \S+ source-address (\S+)")
_APPLY_GROUPS_RE = re.compile(r"^set system apply-groups (\S+)")
_AUTH_ORDER_RE = re.compile(r"^set system authentication-order (\S+)")
_RADIUS_RE = re.compile(r"^set (?:system|access) radius-server (\S+)")
_CLASS_RE = re.compile(r"^set system login class (\S+)")
_USER_RE = re.compile(r"^set system login user (\S+)(?: (.*))?$")
_NTP_RE = re.compile(r"^set system ntp server (\S+)")
_NTP_SRC_RE = re.compile(r"^set system ntp source-address (\S+)")
_SYSLOG_HOST_RE = re.compile(r"^set system syslog host (\S+)")
_SYSLOG_SRC_RE = re.compile(r"^set system syslog source-address (\S+)")
_COMMUNITY_RE = re.compile(r"^set snmp community (\S+)")
_TRAP_GROUP_RE = re.compile(r"^set snmp trap-group (\S+)")
_TRAP_TARGET_RE = re.compile(r"^set snmp trap-group \S+ targets (\S+)")
_TRAP_SRC_RE = re.compile(r"^set snmp trap-options source-address (\S+)")
_CONTACT_RE = re.compile(r'^set snmp contact (.+)$')
_LOCATION_RE = re.compile(r'^set snmp location (.+)$')
_PREFIX_LIST_RE = re.compile(r"^set policy-options prefix-list (\S+) (\S+)$")
_LO0_FILTER_RE = re.compile(r"^set interfaces lo0 unit 0 family inet filter input(?:-list)? (\S+)")
_LO0_ADDR_RE = re.compile(r"^set interfaces lo0 unit 0 family inet address (\S+)")
_FXP0_MASTER_RE = re.compile(r"^set groups re[01] interfaces fxp0 unit 0 family inet address (\S+) master-only")
_FXP0_PLAIN_RE = re.compile(r"^set interfaces fxp0 unit 0 family inet address (\S+)")
_IRB_ADDR_RE = re.compile(r"^set interfaces (?:irb|vlan)\.?\S* unit \d+ family inet address (\S+)")

_SHOW_VERSION_HOST_RE = re.compile(r"^Hostname:\s+(\S+)", re.M)
_SHOW_VERSION_MODEL_RE = re.compile(r"^Model:\s+(\S+)", re.M)
_SHOW_VERSION_JUNOS_RE = re.compile(r"^Junos:\s+(\S+)", re.M)
_SHOW_VERSION_OLD_RE = re.compile(r"^JUNOS Base OS boot \[(\S+)\]", re.M)


def set_lines(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("set ") and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def _first(pattern: re.Pattern, lines: list[str]) -> str:
    for line in lines:
        m = pattern.match(line)
        if m:
            return m.group(1).strip('"')
    return ""


def _all(pattern: re.Pattern, lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        m = pattern.match(line)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def _grouped(pattern: re.Pattern, lines: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for line in lines:
        m = pattern.match(line)
        if m:
            groups.setdefault(m.group(1), []).append(line)
    return groups


def _users(lines: list[str]) -> dict[str, dict]:
    users: dict[str, dict] = {}
    for line in lines:
        m = _USER_RE.match(line)
        if not m:
            continue
        name, rest = m.group(1), m.group(2) or ""
        rec = users.setdefault(name, {"class": "", "uid": "", "lines": [], "has_password": False})
        rec["lines"].append(line)
        if rest.startswith("class "):
            rec["class"] = rest.split(" ", 1)[1]
        elif rest.startswith("uid "):
            rec["uid"] = rest.split(" ", 1)[1]
        elif rest.startswith("authentication "):
            rec["has_password"] = True
    return users


def _trap_groups(lines: list[str]) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for line in lines:
        m = _TRAP_GROUP_RE.match(line)
        if not m:
            continue
        rec = groups.setdefault(m.group(1), {"targets": [], "lines": []})
        rec["lines"].append(line)
        t = _TRAP_TARGET_RE.match(line)
        if t and t.group(1) not in rec["targets"]:
            rec["targets"].append(t.group(1))
    return groups


def _prefix_lists(lines: list[str]) -> dict[str, list[str]]:
    lists: dict[str, list[str]] = {}
    for line in lines:
        m = _PREFIX_LIST_RE.match(line)
        if m and m.group(2) != "apply-path":
            lists.setdefault(m.group(1), []).append(m.group(2))
    return lists


def parse_config(text: str) -> ExistingConfig:
    lines = set_lines(text)
    cfg = ExistingConfig(lines=lines)
    cfg.hostname = _first(_HOSTNAME_RE, lines)
    cfg.version = _first(_VERSION_RE, lines)
    cfg.tacacs_servers = _all(_TACPLUS_RE, lines)
    cfg.tacacs_group = _first(_TACPLUS_GROUP_RE, lines)
    cfg.tacacs_source_address = _first(_TACPLUS_SRC_RE, lines)
    cfg.apply_groups = _all(_APPLY_GROUPS_RE, lines)
    cfg.authentication_order = _all(_AUTH_ORDER_RE, lines)
    cfg.radius_servers = _all(_RADIUS_RE, lines)
    cfg.login_classes = _grouped(_CLASS_RE, lines)
    cfg.login_users = _users(lines)
    cfg.ntp_servers = _all(_NTP_RE, lines)
    cfg.ntp_source_address = _first(_NTP_SRC_RE, lines)
    cfg.syslog_hosts = _grouped(_SYSLOG_HOST_RE, lines)
    cfg.syslog_source_address = _first(_SYSLOG_SRC_RE, lines)
    cfg.snmp_communities = _grouped(_COMMUNITY_RE, lines)
    cfg.snmp_trap_groups = _trap_groups(lines)
    cfg.snmp_trap_source_address = _first(_TRAP_SRC_RE, lines)
    cfg.snmp_contact = _first(_CONTACT_RE, lines)
    cfg.snmp_location = _first(_LOCATION_RE, lines)
    cfg.prefix_lists = _prefix_lists(lines)
    cfg.lo0_filter = _first(_LO0_FILTER_RE, lines)
    cfg.lo0_address = _first(_LO0_ADDR_RE, lines).split("/", 1)[0]
    cfg.fxp0_master_address = (_first(_FXP0_MASTER_RE, lines) or _first(_FXP0_PLAIN_RE, lines)).split("/", 1)[0]
    cfg.irb_mgmt_address = _first(_IRB_ADDR_RE, lines).split("/", 1)[0]
    return cfg


def classify_platform(model: str) -> str:
    """Model string from `show version` (mx480, ex3400-24t, srx345, acx5448, ptx1000)."""
    key = (model or "").strip().lower()
    for family in ("mx", "acx", "ex", "srx", "ptx", "qfx"):
        if key.startswith(family):
            return family
    return "unknown"


def platform_from_config(lines: list[str]) -> str:
    """Best effort for a RANCID dump (no `show version`): shape of the management plane."""
    text = "\n".join(lines)
    if "set security zones" in text or "set security policies" in text:
        return "srx"
    if re.search(r"^set (?:groups re[01] )?interfaces fxp0 ", text, re.M) or "set groups re0 " in text:
        return "mx"
    if re.search(r"^set interfaces (?:me0|vme) ", text, re.M) or "set system services web-management" in text \
            or re.search(r"^set interfaces vlan unit", text, re.M) or "set virtual-chassis" in text:
        return "ex"
    return "unknown"


def facts_from_show_version(device: str, text: str) -> DeviceFacts:
    facts = DeviceFacts(device=device)
    m = _SHOW_VERSION_HOST_RE.search(text)
    facts.hostname = m.group(1) if m else ""
    m = _SHOW_VERSION_MODEL_RE.search(text)
    facts.model = m.group(1) if m else ""
    m = _SHOW_VERSION_JUNOS_RE.search(text) or _SHOW_VERSION_OLD_RE.search(text)
    facts.version = m.group(1) if m else ""
    facts.dual_re = "{master}" in text or "{backup}" in text
    if facts.model:
        facts.platform = classify_platform(facts.model)
        facts.platform_source = "show version"
    return facts


def facts_from_config(device: str, cfg: ExistingConfig, platform_hint: str = "") -> DeviceFacts:
    facts = DeviceFacts(device=device, hostname=cfg.hostname, version=cfg.version)
    if platform_hint:
        facts.platform, facts.platform_source = platform_hint.lower(), "operator"
    else:
        facts.platform, facts.platform_source = platform_from_config(cfg.lines), "config heuristic"
    facts.dual_re = any(l.startswith("set groups re1 ") for l in cfg.lines)
    return facts
