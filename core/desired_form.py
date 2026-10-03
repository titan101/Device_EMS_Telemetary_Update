"""Desired-state form <-> JSON.

The console edits one platform at a time. The form shows the EFFECTIVE values for
that platform (base merged with its override); on save, every section key that
differs from the base is written into platforms.<name>, everything equal is dropped
from the override. MX is the base itself.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from .desired import DEFAULTS, DesiredState, REPLACE_KEYS, _merge

BASE_PLATFORM = "mx"
PLATFORMS = ("mx", "ex", "srx", "acx", "mrv")
SECRET_UNCHANGED = "__unchanged__"


def _lines(text: str) -> list[str]:
    return [l.strip() for l in (text or "").splitlines() if l.strip()]


def _csv(text: str) -> list[str]:
    return [x.strip() for x in (text or "").replace("\n", ",").split(",") if x.strip()]


def _int_or_none(value: str) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


# --------------------------------------------------------------------------- desired -> form
def users_text(users: dict) -> str:
    rows = []
    for name, u in (users or {}).items():
        rows.append(", ".join([name, str(u.get("class", "")), str(u.get("uid") or ""), str(u.get("encrypted_password") or "")]).rstrip(", "))
    return "\n".join(rows)


def communities_text(communities: dict) -> str:
    rows = []
    for name, c in (communities or {}).items():
        rows.append(", ".join([name, c.get("authorization", "read-only"), " ".join(c.get("clients") or [])]).rstrip(", "))
    return "\n".join(rows)


def syslog_text(hosts: dict) -> str:
    return "\n".join(f"{host} {'; '.join(sel)}" for host, sel in (hosts or {}).items())


def classes_rows(classes: dict) -> list[dict]:
    rows = []
    for name, c in (classes or {}).items():
        rows.append({"name": name, "idle_timeout": c.get("idle_timeout") or "",
                     "permissions": "\n".join(c.get("permissions") or []),
                     "deny_commands": "\n".join(c.get("deny_commands") or []),
                     "allow_commands": "\n".join(c.get("allow_commands") or []),
                     "deny_configuration": "\n".join(c.get("deny_configuration") or []),
                     "allow_configuration": "\n".join(c.get("allow_configuration") or [])})
    return rows


# --------------------------------------------------------------------------- form -> section dicts
def parse_users(text: str) -> dict:
    users: dict[str, dict] = {}
    for line in _lines(text):
        parts = [p.strip() for p in line.split(",")]
        if not parts[0] or len(parts) < 2 or not parts[1]:
            raise ValueError(f"user line needs at least 'name, class': {line!r}")
        users[parts[0]] = {"class": parts[1], "uid": _int_or_none(parts[2]) if len(parts) > 2 else None,
                           "encrypted_password": parts[3] if len(parts) > 3 else ""}
    return users


def parse_communities(text: str) -> dict:
    out: dict[str, dict] = {}
    for line in _lines(text):
        parts = [p.strip() for p in line.split(",")]
        auth = parts[1] if len(parts) > 1 and parts[1] else "read-only"
        if auth not in ("read-only", "read-write"):
            raise ValueError(f"community {parts[0]}: authorization must be read-only or read-write")
        out[parts[0]] = {"authorization": auth, "clients": parts[2].split() if len(parts) > 2 else []}
    return out


def parse_syslog(text: str) -> dict:
    out: dict[str, list[str]] = {}
    for line in _lines(text):
        host, _, rest = line.partition(" ")
        selectors = [s.strip() for s in rest.split(";") if s.strip()] or ["any any"]
        out[host.strip()] = selectors
    return out


def parse_classes(form) -> dict:
    names = form.getlist("class_name")
    out: dict[str, dict] = {}
    for i, name in enumerate(names):
        name = name.strip()
        if not name:
            continue

        def col(key: str) -> str:
            values = form.getlist(key)
            return values[i] if i < len(values) else ""

        out[name] = {"idle_timeout": _int_or_none(col("class_idle_timeout")),
                     "permissions": _lines(col("class_permissions")),
                     "deny_commands": _lines(col("class_deny_commands")),
                     "allow_commands": _lines(col("class_allow_commands")),
                     "deny_configuration": _lines(col("class_deny_configuration")),
                     "allow_configuration": _lines(col("class_allow_configuration"))}
    return out


def _source(value: str) -> str:
    value = (value or "").strip()
    return value if value else "auto"


def form_to_sections(form, current: dict) -> dict:
    """The effective desired dict for one platform, from the submitted form."""
    d = copy.deepcopy(current)
    d["commit"] = {"comment": form.get("commit_comment", "ISE_FIX_Script").strip() or "ISE_FIX_Script",
                   "confirmed_minutes": _int_or_none(form.get("confirmed_minutes")) or 20,
                   "confirm_delay_seconds": _int_or_none(form.get("confirm_delay_seconds")) if form.get("confirm_delay_seconds", "").strip().isdigit() else 20,
                   "timeout_seconds": _int_or_none(form.get("timeout_seconds")) or 180}
    secret = form.get("tacacs_secret", "")
    if secret == "" or secret == SECRET_UNCHANGED:
        secret = current["tacacs"].get("secret", "")
    accounting = None
    if form.get("accounting_enabled"):
        accounting = {"events": _lines(form.get("accounting_events", "")) or ["login", "change-log", "interactive-commands"],
                      "destination": form.get("accounting_destination", "tacplus").strip() or "tacplus"}
    d["tacacs"] = {**current["tacacs"], "servers": _lines(form.get("tacacs_servers", "")), "secret": secret,
                   "port": _int_or_none(form.get("tacacs_port")) or 49,
                   "single_connection": bool(form.get("single_connection")),
                   "timeout": _int_or_none(form.get("tacacs_timeout")),
                   "source_address": _source(form.get("tacacs_source", "auto")),
                   "apply_group": form.get("apply_group", "").strip() if form.get("use_apply_group") else "",
                   "rotate_secret": bool(form.get("rotate_secret")),
                   "authentication_order": _csv(form.get("authentication_order", "tacplus")) or ["tacplus"],
                   "accounting": accounting}
    d["radius"] = {"delete": bool(form.get("radius_delete"))}
    d["login"] = {"classes": parse_classes(form), "users": parse_users(form.get("users", "")),
                  "delete_users": _lines(form.get("delete_users", "")),
                  "delete_unlisted_users": bool(form.get("delete_unlisted_users")),
                  "protect_users": _lines(form.get("protect_users", "")) or DEFAULTS["login"]["protect_users"]}
    d["ntp"] = {"servers": _lines(form.get("ntp_servers", "")), "source_address": _source(form.get("ntp_source", "auto")),
                "delete_other_servers": bool(form.get("ntp_delete_others"))}
    d["syslog"] = {"hosts": parse_syslog(form.get("syslog_hosts", "")), "source_address": _source(form.get("syslog_source", "auto")),
                   "delete_other_hosts": bool(form.get("syslog_delete_others"))}
    trap_targets = _lines(form.get("trap_targets", ""))
    d["snmp"] = {**current["snmp"], "communities": parse_communities(form.get("communities", "")),
                 "delete_other_communities": bool(form.get("snmp_delete_others")),
                 "trap_group": ({"name": form.get("trap_group_name", "public").strip() or "public",
                                 "version": form.get("trap_version", "v2").strip() or "v2",
                                 "categories": _lines(form.get("trap_categories", "")),
                                 "targets": trap_targets} if trap_targets else None),
                 "trap_source_address": _source(form.get("trap_source", "auto")),
                 "filter_interfaces": form.get("filter_interfaces", "").strip(),
                 "filter_duplicates": bool(form.get("filter_duplicates")),
                 "contact": form.get("snmp_contact", "").strip(), "location": form.get("snmp_location", "").strip(),
                 "managers": _lines(form.get("snmp_managers", ""))}
    return d


# --------------------------------------------------------------------------- write back
def apply_platform(state: DesiredState, platform: str, effective: dict) -> dict:
    """New raw JSON: MX edits the base; any other platform stores only what differs."""
    raw = copy.deepcopy(state.raw)
    raw.pop("platforms", None)
    sections = ("commit", "tacacs", "radius", "login", "ntp", "syslog", "snmp")
    if platform == BASE_PLATFORM:
        for key in sections:
            raw[key] = effective[key]
        new_state = DesiredState({**raw, "platforms": state.platforms})
        return {**raw, "platforms": _prune_overrides(new_state)}
    base = state.for_platform(BASE_PLATFORM)
    override: dict[str, Any] = {}
    for key in sections:
        diff = _section_diff(base.get(key), effective.get(key))
        if diff is not None:
            override[key] = diff
    platforms = copy.deepcopy(state.platforms)
    if override:
        platforms[platform] = override
    else:
        platforms.pop(platform, None)
    return {**raw, "platforms": platforms}


def _section_diff(base: Any, new: Any) -> Any:
    if base == new:
        return None
    if isinstance(base, dict) and isinstance(new, dict):
        out: dict[str, Any] = {}
        for key in new:
            if key in REPLACE_KEYS or not isinstance(new[key], dict):
                if base.get(key) != new[key]:
                    out[key] = new[key]
            else:
                sub = _section_diff(base.get(key), new[key])
                if sub is not None:
                    out[key] = sub
        for key in base:
            if key not in new:
                out[key] = None
        return out or None
    return new


def _prune_overrides(state: DesiredState) -> dict:
    """After a base edit, drop override values that now equal the base."""
    base = state.for_platform(BASE_PLATFORM)
    pruned: dict[str, Any] = {}
    for platform, override in state.platforms.items():
        if not isinstance(override, dict):
            continue
        effective = state.for_platform(platform)
        new_override: dict[str, Any] = {}
        for key in override:
            diff = _section_diff(base.get(key), effective.get(key))
            if diff is not None:
                new_override[key] = diff
        if new_override:
            pruned[platform] = new_override
    return pruned


def dumps(raw: dict) -> str:
    return json.dumps(raw, indent=2) + "\n"


def _walk_secret(raw: dict, fn) -> None:
    for section in [raw] + [o for o in (raw.get("platforms") or {}).values() if isinstance(o, dict)]:
        tac = section.get("tacacs")
        if isinstance(tac, dict) and "secret" in tac:
            tac["secret"] = fn(tac["secret"])


def mask_json(raw: dict) -> str:
    """The raw JSON for the Advanced editor, with every TACACS secret replaced by a marker."""
    out = copy.deepcopy(raw)
    _walk_secret(out, lambda v: SECRET_UNCHANGED if v and "REPLACE_WITH" not in str(v) else v)
    return dumps(out)


def unmask_json(text: str, current: dict) -> str:
    """Put the real secrets back where the Advanced editor left the marker."""
    data = json.loads(text)
    secrets: list[str] = []
    _walk_secret(copy.deepcopy(current), lambda v: secrets.append(v) or v)
    base_secret = (current.get("tacacs") or {}).get("secret", "")
    _walk_secret(data, lambda v: base_secret if v == SECRET_UNCHANGED else v)
    return dumps(data)
