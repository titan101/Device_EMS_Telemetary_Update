"""config/desired_state.json: the standard every device is brought to.

Top-level sections apply to every platform; `platforms.<name>` deep-merges
overrides on top (a list or scalar replaces, a dict merges). `for_platform()`
returns the merged view the templates render from.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

DESIRED_FILE = "desired_state.json"
SECTIONS = ("commit", "tacacs", "radius", "login", "ntp", "syslog", "snmp")
PLACEHOLDER_RE = re.compile(r"REPLACE_WITH|<[A-Z][A-Z0-9_-]*>")

DEFAULTS: dict[str, Any] = {
    "cm_number": "",
    "commit": {"comment": "ISE_FIX_Script", "confirmed_minutes": 20, "confirm_delay_seconds": 20,
               "timeout_seconds": 180},
    "tacacs": {"servers": [], "secret": "", "port": 49, "single_connection": True, "timeout": None,
               "source_address": "auto", "apply_group": "tacplus_servers", "rotate_secret": True,
               "authentication_order": ["tacplus"],
               "authen_method": "", "aaa_lines": [],
               "accounting": {"events": ["login", "change-log", "interactive-commands"], "destination": "tacplus"}},
    "radius": {"delete": True},
    "login": {"classes": {}, "users": {}, "delete_users": [], "delete_unlisted_users": False,
              "protect_users": ["root", "remote", "remote-admin", "remote-operator", "remote-provision"]},
    "ntp": {"servers": [], "source_address": "auto", "delete_other_servers": True},
    "syslog": {"hosts": {}, "source_address": "auto", "delete_other_hosts": True},
    "snmp": {"communities": {}, "delete_other_communities": True, "trap_group": None,
             "trap_source_address": "auto", "filter_interfaces": "", "filter_duplicates": False,
             "contact": "", "location": "", "managers": [], "mrv_trap_community": "public"},
    "platforms": {},
}


class DesiredError(RuntimeError):
    pass


# A platform override of one of these replaces the whole mapping: an EX with
# `"classes": {}` means "no custom classes", not "the MX classes".
REPLACE_KEYS = {"classes", "users", "communities", "hosts", "delete_users", "protect_users", "managers"}


def _merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = {k: copy.deepcopy(v) for k, v in base.items()}
        for key, value in override.items():
            if key in out and key not in REPLACE_KEYS:
                out[key] = _merge(out[key], value)
            else:
                out[key] = copy.deepcopy(value)
        return out
    return copy.deepcopy(override)


class DesiredState:
    def __init__(self, data: dict[str, Any], source: str = ""):
        if not isinstance(data, dict):
            raise DesiredError("desired state must be a JSON object")
        self.raw = data
        self.source = source
        self.base = _merge(DEFAULTS, {k: v for k, v in data.items() if k != "platforms"})
        self.platforms = data.get("platforms") or {}
        if not isinstance(self.platforms, dict):
            raise DesiredError("'platforms' must be an object keyed by platform name")

    def for_platform(self, platform: str) -> dict[str, Any]:
        merged = copy.deepcopy(self.base)
        override = self.platforms.get(platform)
        if isinstance(override, dict):
            merged = _merge(merged, override)
        _normalise(merged)
        return merged

    @property
    def commit(self) -> dict[str, Any]:
        return self.base["commit"]

    def placeholders(self, platform: str) -> list[str]:
        """Values still carrying REPLACE_WITH / <TOKEN> -- the build refuses on these."""
        found: list[str] = []
        _walk(self.for_platform(platform), "", found)
        return found


def _normalise(d: dict[str, Any]) -> None:
    tac = d["tacacs"]
    tac["servers"] = [str(s) for s in tac.get("servers") or []]
    tac["authentication_order"] = [str(s) for s in tac.get("authentication_order") or ["tacplus"]]
    tac["aaa_lines"] = [str(s) for s in tac.get("aaa_lines") or []]
    login = d["login"]
    for name, cls in (login.get("classes") or {}).items():
        for key in ("permissions", "deny_commands", "allow_commands", "deny_configuration", "allow_configuration"):
            cls[key] = [str(x) for x in cls.get(key) or []]
        cls.setdefault("idle_timeout", None)
    for name, user in (login.get("users") or {}).items():
        user.setdefault("class", "")
        user.setdefault("uid", None)
        user.setdefault("encrypted_password", "")
        if not user["class"]:
            raise DesiredError(f"login.users.{name} needs a class")
    d["ntp"]["servers"] = [str(s) for s in d["ntp"].get("servers") or []]
    hosts = d["syslog"].get("hosts") or {}
    d["syslog"]["hosts"] = {str(h): [str(x) for x in (sel or ["any any"])] for h, sel in hosts.items()}
    snmp = d["snmp"]
    for name, com in (snmp.get("communities") or {}).items():
        com.setdefault("authorization", "read-only")
        com["clients"] = [str(c) for c in com.get("clients") or []]
    tg = snmp.get("trap_group")
    if tg:
        tg.setdefault("name", "public")
        tg.setdefault("version", "v2")
        tg["categories"] = [str(c) for c in tg.get("categories") or []]
        tg["targets"] = [str(t) for t in tg.get("targets") or []]


def _walk(node: Any, path: str, found: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and PLACEHOLDER_RE.search(key):
                found.append(f"{path}.{key}" if path else key)
            _walk(value, f"{path}.{key}" if path else str(key), found)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _walk(value, f"{path}[{i}]", found)
    elif isinstance(node, str) and PLACEHOLDER_RE.search(node):
        found.append(f"{path} = {node}")


def load_desired(path: Path) -> DesiredState:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DesiredError(f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise DesiredError(f"{path.name} is not valid JSON: {exc}") from exc
    return DesiredState(data, source=str(path))


def validate_text(text: str) -> tuple[DesiredState | None, str]:
    """For the console editor: (state, "") or (None, error)."""
    try:
        state = DesiredState(json.loads(text))
        for platform in ("mx", "ex", "srx", "mrv"):
            state.for_platform(platform)
    except (ValueError, DesiredError) as exc:
        return None, str(exc)
    return state, ""
