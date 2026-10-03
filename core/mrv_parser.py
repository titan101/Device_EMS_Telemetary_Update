"""MRV OptiSwitch (MasterOS) running-config -> ExistingConfig.

IOS-like text: top-level statements, one-space-indented block children, `!`
separators. Only the management-plane blocks are read. Works on a clogin
transcript (banners, prompts, `show version` output mixed in) as well as a
RANCID file (`!RANCID-CONTENT-TYPE: mrv` header, `!Image:` lines).
"""
from __future__ import annotations

import re

from .models import DeviceFacts, ExistingConfig

_HOSTNAME_RE = re.compile(r"^hostname (\S+)", re.M)
_VERSION_RE = re.compile(r"^! ?version (\S+)", re.M)
_IMAGE_MODEL_RE = re.compile(r"MRV OptiSwitch[- ]?(\S+)", re.I)
_IMAGE_OS_RE = re.compile(r"MasterOS version:\s*(\S+)", re.I)
_TACACS_HOST_RE = re.compile(r"^tacacs-server host (\S+)(?: key (\S+))?", re.M)
_RSYSLOG_RE = re.compile(r"^rsyslog(?: log)? (\S+)", re.M)
_USERNAME_RE = re.compile(r"^username (\S+)", re.M)
_BLOCK_HEADERS = ("aaa", "ntp", "snmp")
_IFACE_RE = re.compile(r"^interface (out-of-band \S+|vlan \S+)")
_IP_RE = re.compile(r"^\s+ip (\d+\.\d+\.\d+\.\d+)(?:/\d+)?")


def config_lines(text: str) -> list[str]:
    """The running-config portion: from the first `!`/statement line to the trailing prompt,
    transcript noise (prompts, echoed commands, `!Image:` headers) removed."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if re.match(r"^\S+[>#]\s", line) or re.match(r"^\S+[>#]$", line):
            continue  # prompt / echoed command
        if line.startswith(("spawn ", "Password", "Last login", "ATTENTION", "!RANCID", "!Image")):
            continue
        out.append(line)
    return out


def blocks(lines: list[str]) -> dict[str, list[str]]:
    """Top-level block header -> its children (stripped). Later blocks with the same header merge."""
    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        if line.startswith((" ", "\t")):
            if current is not None:
                found.setdefault(current, []).append(line.strip())
            continue
        current = line.strip() if line.strip() and not line.startswith("!") else None
        if current is not None:
            found.setdefault(current, [])
    return found


def parse_config(text: str) -> ExistingConfig:
    lines = config_lines(text)
    cfg = ExistingConfig(lines=lines)
    body = "\n".join(lines)
    cfg.hostname = _first(_HOSTNAME_RE, body)
    cfg.version = _first(_VERSION_RE, body)
    cfg.blocks = blocks(lines)
    for m in _TACACS_HOST_RE.finditer(body):
        if m.group(1) not in cfg.tacacs_servers:
            cfg.tacacs_servers.append(m.group(1))
    cfg.authentication_order = [l for l in cfg.blocks.get("aaa", []) if l.startswith("authentication login")]
    for m in _USERNAME_RE.finditer(body):
        cfg.login_users.setdefault(m.group(1), {"class": "", "uid": "", "lines": [], "has_password": True})
        cfg.login_users[m.group(1)]["lines"].append(m.group(0))
    cfg.ntp_servers = [l.split()[1] for l in cfg.blocks.get("ntp", []) if l.startswith("server ") and len(l.split()) > 1]
    for m in _RSYSLOG_RE.finditer(body):
        cfg.syslog_hosts.setdefault(m.group(1), []).append(m.group(0))
    _snmp(cfg)
    _management(cfg, lines)
    return cfg


def _snmp(cfg: ExistingConfig) -> None:
    targets: list[str] = []
    trap_lines: list[str] = []
    for child in cfg.blocks.get("snmp", []):
        parts = child.split()
        if not parts:
            continue
        if parts[0] == "community" and len(parts) > 1:
            # MRV: `community <index> <read-only|read-write> default <community-string>`
            name = parts[-1] if len(parts) >= 5 else parts[1]
            cfg.snmp_communities.setdefault(name, []).append(child)
            cfg.snmp_community_ids[name] = parts[1]
        elif parts[0] == "trapsess" and len(parts) > 1:
            targets.append(parts[1])
            trap_lines.append(child)
        elif parts[0] == "source" and len(parts) > 2 and parts[1] == "ip":
            cfg.snmp_trap_source_address = parts[2]
        elif parts[0] == "contact":
            cfg.snmp_contact = child[len("contact "):].strip()
        elif parts[0] == "location":
            cfg.snmp_location = child[len("location "):].strip()
    if targets or trap_lines:
        cfg.snmp_trap_groups["trapsess"] = {"targets": targets, "lines": trap_lines}


def _management(cfg: ExistingConfig, lines: list[str]) -> None:
    current = ""
    for line in lines:
        m = _IFACE_RE.match(line)
        if m:
            current = m.group(1)
            continue
        if not line.startswith((" ", "\t")):
            current = ""
            continue
        ip = _IP_RE.match(line)
        if ip and current:
            if current.startswith("out-of-band"):
                cfg.fxp0_master_address = cfg.fxp0_master_address or ip.group(1)
            else:
                cfg.irb_mgmt_address = cfg.irb_mgmt_address or ip.group(1)


def _first(pattern: re.Pattern, text: str) -> str:
    m = pattern.search(text)
    return m.group(1) if m else ""


def facts(device: str, transcript: str, cfg: ExistingConfig) -> DeviceFacts:
    f = DeviceFacts(device=device, hostname=cfg.hostname, platform="mrv", platform_source="operator")
    m = _IMAGE_MODEL_RE.search(transcript)
    f.model = f"OptiSwitch {m.group(1)}" if m else "MRV OptiSwitch"
    m = _IMAGE_OS_RE.search(transcript)
    f.version = m.group(1) if m else cfg.version
    return f


def is_mrv_text(text: str) -> bool:
    return bool(_TACACS_HOST_RE.search(text) or "!RANCID-CONTENT-TYPE: mrv" in text
                or re.search(r"^tacacs-server authen-method|^rsyslog |^no cli-paging", text, re.M))
