"""Credential ladder for jlogin sessions.

jlogin reads ~/.cloginrc (the operator's own TACACS/ISE account). Devices
whose TACACS is already broken only answer to local accounts, so a run can
also carry a list of static local users that are tried in order. A static
credential is handed to jlogin through a throw-away cloginrc file (-f) with
mode 0600, never on the command line where ps would show it.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

CREDENTIALS_FILE = "credentials.json"
JLOGIN_DEFAULT = "jlogin-default"


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credential:
    label: str
    username: str = ""
    password: str = ""

    @property
    def is_default(self) -> bool:
        return self.label == JLOGIN_DEFAULT


@dataclass
class CredentialSet:
    """Ladders for the two kinds of session.

    discover: order tried for discovery and deploy (default jlogin first, then
    static local users). confirm: order tried for the second login that proves
    the new AAA works -- by default ONLY the jlogin default account, so a box
    whose ISE login fails rolls itself back instead of being confirmed through
    a local account.
    """
    discover: list[Credential] = field(default_factory=lambda: [Credential(JLOGIN_DEFAULT)])
    confirm: list[Credential] = field(default_factory=lambda: [Credential(JLOGIN_DEFAULT)])

    def labels(self, ladder: str) -> list[str]:
        return [c.label for c in getattr(self, ladder)]

    def by_label(self, label: str) -> Credential | None:
        for cred in self.discover + self.confirm:
            if cred.label == label:
                return cred
        return None


def _parse_users(raw: object) -> dict[str, Credential]:
    users: dict[str, Credential] = {}
    if not isinstance(raw, list):
        return users
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("username") or "").strip()
        username = str(item.get("username") or "").strip()
        password = str(item.get("password") or "")
        if not label or not username:
            continue
        users[label] = Credential(label=label, username=username, password=password)
    return users


def _ladder(names: object, users: dict[str, Credential], fallback: list[str]) -> list[Credential]:
    chosen = names if isinstance(names, list) and names else fallback
    ladder: list[Credential] = []
    for name in chosen:
        name = str(name)
        if name == JLOGIN_DEFAULT:
            ladder.append(Credential(JLOGIN_DEFAULT))
        elif name in users:
            ladder.append(users[name])
        else:
            raise CredentialError(f"credential ladder names {name!r} but no local user has that label")
    return ladder


def load_credentials(path: Path) -> CredentialSet:
    """Missing file -> jlogin default only. A present file must parse."""
    if not path.exists():
        return CredentialSet()
    _warn_if_permissive(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CredentialError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise CredentialError(f"{path.name} must hold a JSON object")
    users = _parse_users(data.get("local_users"))
    default_first = [JLOGIN_DEFAULT] if data.get("jlogin_default", True) else []
    discover = _ladder(data.get("discover_order"), users, default_first + list(users))
    confirm = _ladder(data.get("confirm_order"), users, [JLOGIN_DEFAULT])
    if not discover:
        raise CredentialError(f"{path.name}: the discover ladder is empty")
    return CredentialSet(discover=discover, confirm=confirm)


def _warn_if_permissive(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"WARNING: {path} is readable by group/other -- chmod 600 it", flush=True)


def write_temp_cloginrc(directory: Path, cred: Credential) -> Path:
    """One-shot cloginrc for a static credential; caller deletes it after the session."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f".cloginrc_{cred.label}_{os.getpid()}"
    body = (f"add user * {{{cred.username}}}\n"
            f"add password * {{{cred.password}}} {{{cred.password}}}\n"
            "add method * {ssh}\n"
            "add autoenable * {1}\n")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def public_summary(creds: CredentialSet) -> dict:
    return {
        "discover": [{"label": c.label, "username": c.username or "(from ~/.cloginrc)"} for c in creds.discover],
        "confirm": [{"label": c.label, "username": c.username or "(from ~/.cloginrc)"} for c in creds.confirm],
    }
