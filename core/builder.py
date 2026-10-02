"""Render the platform template, drop what the box already has, and write the fix file
with its verify commands and an exact restore-style rollback.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import datetime
from pathlib import Path

from . import templates as tpl
from .models import BuildResult, DeviceFacts, ExistingConfig

SECTION_MARKER = "# --- "
VERIFY_HEADER = "# --- Verify ---"
ROLLBACK_HEADER = "# --- Rollback (restores the pre-change lines -- the commit-confirmed timer is the first line of defence) ---"
PURPOSE = "EMS standard -- TACACS+ to ISE, login classes/users, accounting, NTP, syslog, SNMP"

# Managed objects: a set/delete line belongs to the first path pattern it matches.
# Exact-state compare, rollback and verification all work per object.
_OBJECT_RES = [re.compile(p) for p in (
    r"^(groups \S+ system tacplus-server)",
    r"^(system tacplus-server \S+)",
    r"^(system apply-groups \S+)",
    r"^(system authentication-order)",
    r"^(system accounting)",
    r"^(system radius-server \S+)",
    r"^(access radius-server \S+)",
    r"^(system login class \S+)",
    r"^(system login user \S+)",
    r"^(system ntp server \S+)",
    r"^(system ntp source-address)",
    r"^(system syslog host \S+)",
    r"^(system syslog source-address)",
    r"^(snmp community \S+)",
    r"^(snmp trap-group \S+)",
    r"^(snmp trap-options source-address)",
    r"^(snmp filter-interfaces)",
    r"^(snmp filter-duplicates)",
    r"^(snmp contact)",
    r"^(snmp location)",
)]
_SECRET_RE = re.compile(r"( secret )(.*)$")


class BuildError(RuntimeError):
    pass


def split_verb(line: str) -> tuple[str, str]:
    verb, _, rest = line.strip().partition(" ")
    return verb, rest.strip()


def object_path(line: str) -> str:
    _, path = split_verb(line)
    for pattern in _OBJECT_RES:
        m = pattern.match(path)
        if m:
            return m.group(1)
    return path


def mask_secret(line: str) -> str:
    return _SECRET_RE.sub(r"\1<secret>", line)


def resolve_sources(platform: str, existing: ExistingConfig, d: dict) -> tuple[dict[str, str], list[str]]:
    """'auto' -> the device's current value, else its management address; a literal IP; or '' (omit)."""
    notes: list[str] = []
    current_any = existing.tacacs_source_address or existing.ntp_source_address or existing.syslog_source_address
    if platform in ("mx", "acx"):
        fallback = existing.fxp0_master_address or existing.irb_mgmt_address or existing.lo0_address
    else:
        fallback = existing.lo0_address or existing.irb_mgmt_address or existing.fxp0_master_address

    def pick(label: str, setting: object, current: str) -> str:
        if setting in ("", None, "none", False):
            return ""
        if str(setting) != "auto":
            return str(setting)
        value = current or current_any or fallback
        if not value:
            notes.append(f"{label} source-address omitted: the box shows no management address to use")
        return value

    src = {
        "tacacs": pick("tacacs", d["tacacs"].get("source_address"), existing.tacacs_source_address),
        "ntp": pick("ntp", d["ntp"].get("source_address"), existing.ntp_source_address),
        "syslog": pick("syslog", d["syslog"].get("source_address"), existing.syslog_source_address),
        "snmp_trap": pick("snmp trap-options", d["snmp"].get("trap_source_address"),
                          existing.snmp_trap_source_address),
    }
    return src, notes


def users_to_delete(existing: ExistingConfig, d: dict, protected_extra: list[str]) -> list[str]:
    login = d["login"]
    protected = set(login.get("protect_users") or []) | set(login.get("users") or {}) | set(protected_extra)
    wanted = set(login.get("delete_users") or [])
    out: list[str] = []
    for name in existing.login_users:
        if name in protected:
            continue
        if name in wanted or login.get("delete_unlisted_users"):
            out.append(name)
    return out


def render(platform: str, existing: ExistingConfig, d: dict, protected_extra: list[str]) -> tuple[list[str], list[str]]:
    path = tpl.template_path(platform)
    if not path:
        raise BuildError(f"no template for platform {platform!r} -- supported: {', '.join(tpl.TEMPLATE_FOR)}")
    src, notes = resolve_sources(platform, existing, d)
    deletions = users_to_delete(existing, d, protected_extra)

    def uid_for(name: str) -> str:
        uid = (d["login"]["users"].get(name) or {}).get("uid")
        if uid:
            return str(uid)
        return str((existing.login_users.get(name) or {}).get("uid") or "")

    text = tpl.environment().get_template(path).render(
        d=d, existing=existing, src=src, users_to_delete=deletions, uid_for=uid_for)
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    return lines, notes


def _norm(lines: list[str], mask: bool) -> list[str]:
    out = [mask_secret(l) if mask else l for l in lines]
    return sorted(set(out))


def minimise(rendered: list[str], existing: ExistingConfig, rotate_secret: bool = True) -> list[str]:
    """Drop every object the box already has exactly, every set it already has,
    every delete of something that isn't there. Order of the rest is kept."""
    mask = not rotate_secret
    objects: dict[str, dict] = {}
    for line in rendered:
        verb, path = split_verb(line)
        obj = object_path(line)
        rec = objects.setdefault(obj, {"delete_self": False, "lines": []})
        if verb == "delete" and path == obj:
            rec["delete_self"] = True
        rec["lines"].append(line)
    out: list[str] = []
    for obj, rec in objects.items():
        have = existing.lines_under(obj)
        sets = [l for l in rec["lines"] if l.startswith("set ")]
        if rec["delete_self"]:
            if not have and not sets:
                continue
            if have and _norm(sets, mask) == _norm(have, mask):
                continue
            # Deleting an absent object makes Junos print "statement not found";
            # harmless, but noise in the diff -- send only the sets then.
            out.extend(l for l in rec["lines"] if have or not l.startswith("delete "))
            continue
        have_norm = set(_norm(have, mask))
        for line in rec["lines"]:
            verb, path = split_verb(line)
            if verb == "set" and (mask_secret(line) if mask else line) in have_norm:
                continue
            if verb == "delete" and not existing.has_path(path):
                continue
            out.append(line)
    return out


def touched_objects(config_lines: list[str]) -> list[str]:
    seen: list[str] = []
    for line in config_lines:
        obj = object_path(line)
        if obj not in seen:
            seen.append(obj)
    return seen


def rollback_lines(config_lines: list[str], existing: ExistingConfig) -> list[str]:
    """Restore, not invert: every touched object is deleted and its pre-change lines put back."""
    deletes: list[str] = []
    restores: list[str] = []
    for obj in touched_objects(config_lines):
        deletes.append(f"delete {obj}")
        restores.extend(existing.lines_under(obj))
    return deletes + restores


def forbidden_paths(config_lines: list[str]) -> list[str]:
    """Objects deleted and never re-set: after the change no `set <obj>` may remain."""
    deleted = [object_path(l) for l in config_lines if l.startswith("delete ") and split_verb(l)[1] == object_path(l)]
    reset = {object_path(l) for l in config_lines if l.startswith("set ")}
    return [obj for obj in deleted if obj not in reset]


_STANZA_FOR = (
    ("groups ", lambda obj: " ".join(obj.split()[:2])),
    ("system tacplus-server", lambda obj: "system tacplus-server"),
    ("system login", lambda obj: "system login"),
    ("system ntp", lambda obj: "system ntp"),
    ("system syslog", lambda obj: "system syslog"),
    ("snmp", lambda obj: "snmp"),
)


def verify_commands(config_lines: list[str], d: dict) -> list[str]:
    """One `show configuration <stanza> | display set` per touched stanza, after `show system commit`."""
    stanzas: list[str] = []
    for obj in touched_objects(config_lines):
        head = next((fn(obj) for prefix, fn in _STANZA_FOR if obj.startswith(prefix)), obj)
        if head not in stanzas:
            stanzas.append(head)
    return ["show system commit | no-more"] + [f"show configuration {s} | display set | no-more" for s in stanzas]


def _manager_coverage(existing: ExistingConfig, d: dict) -> list[str]:
    warnings: list[str] = []
    if not existing.lo0_filter:
        return warnings
    nets = []
    for name, prefixes in existing.prefix_lists.items():
        for p in prefixes:
            try:
                nets.append((name, ipaddress.ip_network(p, strict=False)))
            except ValueError:
                continue
    for manager in d["snmp"].get("managers") or []:
        try:
            ip = ipaddress.ip_address(manager)
        except ValueError:
            continue
        covered = [name for name, net in nets if ip in net]
        if not covered:
            warnings.append(f"SNMP manager {manager} is not in any prefix-list while lo0 carries filter "
                            f"{existing.lo0_filter} -- polling from it may be dropped until the filter is updated")
    return warnings


def build(facts: DeviceFacts, existing: ExistingConfig, desired, protected_extra: list[str] | None = None) -> BuildResult:
    platform = facts.platform
    if not facts.template_family:
        raise BuildError(f"{facts.device}: platform {platform!r} ({facts.model or 'model unknown'}) has no "
                         "validated template -- give --platform, or add one under templates/")
    d = desired.for_platform(platform)
    holes = desired.placeholders(platform)
    if holes:
        raise BuildError("desired_state.json still has placeholders: " + "; ".join(holes[:5]))
    if not d["tacacs"]["servers"] or not d["tacacs"]["secret"]:
        raise BuildError("desired_state.json: tacacs.servers and tacacs.secret are required")
    rendered, notes = render(platform, existing, d, protected_extra or [])
    config = minimise(rendered, existing, rotate_secret=bool(d["tacacs"].get("rotate_secret", True)))
    warnings = _manager_coverage(existing, d)
    if platform in tpl.UNVALIDATED:
        warnings.insert(0, f"{platform.upper()} template is UNVALIDATED: {tpl.UNVALIDATED[platform]}")
    if facts.platform_source == "config heuristic":
        notes.append(f"platform {platform} guessed from the config shape, not from `show version`")
    return BuildResult(device=facts.device, platform=platform, config_lines=config,
                       verify_commands=verify_commands(config, d),
                       rollback_lines=rollback_lines(config, existing), notes=notes, warnings=warnings,
                       forbidden_paths=forbidden_paths(config))


def render_file(result: BuildResult, facts: DeviceFacts, run_id: str, cm_number: str,
                before_source: str) -> str:
    parts = [
        f"# Device: {facts.device}" + (f" ({facts.hostname})" if facts.hostname and facts.hostname != facts.device else ""),
        f"# Platform: {result.platform} ({facts.model or 'model unknown'}, Junos {facts.version or '?'}) -- "
        f"template {tpl.template_path(result.platform)}",
        f"# Run: {run_id}" + (f"   CM: {cm_number}" if cm_number else ""),
        f"# Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"# Purpose: {PURPOSE}",
        f"# Before-picture: {before_source}",
    ]
    parts += [f"# NOTE: {n}" for n in result.notes]
    parts += [f"# WARNING: {w}" for w in result.warnings]
    parts.append("")
    if result.compliant:
        parts += ["# COMPLIANT -- the box already matches the standard, nothing to send.", ""]
    else:
        parts += result.config_lines
    parts += ["", VERIFY_HEADER, *result.verify_commands]
    if result.rollback_lines:
        parts += ["", ROLLBACK_HEADER, *result.rollback_lines]
    return "\n".join(parts) + "\n"


def config_lines_from_file(path: Path) -> list[str]:
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith(SECTION_MARKER):
            break
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def show_commands_from_file(path: Path) -> list[str]:
    cmds: list[str] = []
    in_verify = False
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith(SECTION_MARKER):
            in_verify = line == VERIFY_HEADER
            continue
        if in_verify and line.startswith("show ") and line not in cmds:
            cmds.append(line)
    return cmds


def rollback_lines_from_file(path: Path) -> list[str]:
    lines: list[str] = []
    in_rollback = False
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith(SECTION_MARKER):
            in_rollback = line.startswith("# --- Rollback")
            continue
        if in_rollback and line and not line.startswith("#"):
            lines.append(line)
    return lines


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
