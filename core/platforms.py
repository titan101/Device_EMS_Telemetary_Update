"""Per-platform profile: which RANCID login script reaches the box, how a command
file is wrapped, what the transcript must show, and which commit model applies.

    confirmed  Junos: commit confirmed N, a second login confirms; the box reverts itself.
    staged     MRV:   changes apply at once but are NOT saved; the second login verifies,
                      removes the old servers and saves. A failed confirm is undone by an
                      explicit rollback session (startup config was never touched).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

JUNOS = "junos"
MRV = "mrv"
ADVA = "adva"

CONFIRMED = "confirmed"
STAGED = "staged"


@dataclass(frozen=True)
class Profile:
    platform: str
    family: str
    login_binary: str
    login_args: tuple[str, ...] = ()
    commit_model: str = CONFIRMED
    template: str = ""
    discover_commands: tuple[str, ...] = ()
    running_config_command: str = ""
    supported: bool = True
    note: str = ""
    prompt_re: re.Pattern = field(default=re.compile(r"^\S+@\S+[>#]", re.M))
    error_re: re.Pattern | None = None

    @property
    def is_junos(self) -> bool:
        return self.family == JUNOS


_JUNOS_DISCOVER = (
    "show version | no-more",
    "show configuration system | display set | no-more",
    "show configuration groups | display set | no-more",
    "show configuration snmp | display set | no-more",
    "show configuration interfaces lo0 | display set | no-more",
    "show configuration interfaces fxp0 | display set | no-more",
    "show configuration policy-options | display set | match prefix-list | no-more",
)
_MRV_DISCOVER = ("no cli-paging", "enable", "show version", "show running-config")
_MRV_PROMPT = re.compile(r"^[A-Za-z0-9][\w.-]*(?:\(config[^)]*\))?[>#](?:\s|$)", re.M)   # prompt alone or with the echoed command
_MRV_ERROR = re.compile(r"^\s*%\s*(unknown command|invalid|incomplete|ambiguous|error)|^error:", re.I | re.M)

PROFILES: dict[str, Profile] = {
    "mx": Profile("mx", JUNOS, "jlogin", template="junos/mx/ems_fix.set.j2", discover_commands=_JUNOS_DISCOVER),
    "acx": Profile("acx", JUNOS, "jlogin", template="junos/mx/ems_fix.set.j2", discover_commands=_JUNOS_DISCOVER),
    "ex": Profile("ex", JUNOS, "jlogin", template="junos/ex/ems_fix.set.j2", discover_commands=_JUNOS_DISCOVER),
    "srx": Profile("srx", JUNOS, "jlogin", template="junos/srx/ems_fix.set.j2", discover_commands=_JUNOS_DISCOVER,
                   note="no real SRX configuration has been reviewed yet -- rehearse and read the diff first"),
    "mrv": Profile("mrv", MRV, "clogin", commit_model=STAGED, template="mrv/optiswitch/ems_fix.cli.j2",
                   discover_commands=_MRV_DISCOVER, running_config_command="show running-config",
                   prompt_re=_MRV_PROMPT, error_re=_MRV_ERROR,
                   note="MRV has no commit-confirmed: changes are applied unsaved, the ISE login verifies, "
                        "then the old servers are removed and the config is saved"),
    "adva": Profile("adva", ADVA, "clogin", login_args=("-noenable",), commit_model=STAGED, supported=False,
                    note="ADVA FSP150-XG480: reachable with clogin -noenable / jlogin, but no save or rollback "
                         "command has been confirmed on a real box -- not built yet"),
}

NON_JUNOS = tuple(p for p, prof in PROFILES.items() if not prof.is_junos)


def profile(platform: str) -> Profile | None:
    return PROFILES.get((platform or "").lower())


def supported_platforms() -> list[str]:
    return [p for p, prof in PROFILES.items() if prof.supported and prof.template]


# --------------------------------------------------------------------------- MRV command files
MRV_EXEC_PREFIX = ("no cli-paging", "enable")
MRV_SKIP_IN_VERIFY = {"no cli-paging", "enable", "configure terminal", "configure", "end", "exit",
                      "write memory", "write me"}


def mrv_config_session(config_lines: list[str], save: bool) -> str:
    """Exec prefix, config mode, the lines (block children keep their one-space indent),
    back to exec, optional save. Mirrors the production MRV push scripts."""
    body = [*MRV_EXEC_PREFIX, "configure terminal", *config_lines, "end"]
    if save:
        body.append("write memory")
    body.append("exit")
    return "\n".join(body) + "\n"


def mrv_show_session(show_commands: list[str]) -> str:
    return "\n".join([*MRV_EXEC_PREFIX, *show_commands, "exit"]) + "\n"


def mrv_confirm_session(show_commands: list[str], finalize_lines: list[str]) -> str:
    """Verify first (the shows), then the finalising config and the save."""
    body = [*MRV_EXEC_PREFIX, *show_commands]
    if finalize_lines:
        body += ["configure terminal", *finalize_lines, "end"]
    body.append("write memory")
    if finalize_lines:
        body += list(show_commands)      # verification reads the LAST dump: the removals must show there
    body.append("exit")
    return "\n".join(body) + "\n"


_CLI_FORBIDDEN_FIRST = {"reload", "erase", "format", "copy", "boot", "write", "end", "configure", "enable",
                        "no cli-paging", "quit", "logout"}
_CLI_BAD_CHARS = set(";|&<>`$\\")


def validate_cli_lines(lines: list[str]) -> str:
    """'' when every line is a plain config statement; else the first offending line."""
    for raw in lines:
        line = raw.strip()
        if not line or any(ch in _CLI_BAD_CHARS for ch in line) or any(ord(ch) < 32 for ch in line):
            return raw
        first = line.split()[0].lower()
        if first in _CLI_FORBIDDEN_FIRST or line.lower() in _CLI_FORBIDDEN_FIRST:
            return raw
    return ""
