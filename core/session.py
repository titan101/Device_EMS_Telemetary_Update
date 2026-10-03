"""The only code in this project that can touch a device.

A session is `jlogin -x <command file> <device>` (optionally `-f <cloginrc>` for
a static local credential). The command file is built here from a fixed set
of shapes -- there is no free-form path to a device. jlogin's exit code is
not trusted (RANCID login scripts exit 0 after "Error: Couldn't login"), so
every session gets a verdict read from the transcript, and OK needs positive
evidence of what the action was meant to do.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .credentials import Credential, write_temp_cloginrc
from .platforms import CONFIRMED, Profile, profile as platform_profile

SNAPSHOT = "snapshot"       # show commands only
SHOWCOMPARE = "showcompare"  # load config, show | compare, rollback 0
COMMIT = "commit"           # load config, commit confirmed N
CONFIRM = "confirm"         # show commands, then an empty commit inside the window
ROLLBACK = "rollback"       # rollback 1 + commit
ACTIONS = (SNAPSHOT, SHOWCOMPARE, COMMIT, CONFIRM, ROLLBACK)
COMMITTING_ACTIONS = (COMMIT, CONFIRM, ROLLBACK)

CONFIG_VERBS = ("set ", "delete ", "activate ", "deactivate ")
PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_-]*>|REPLACE_WITH")
DEFAULT_TIMEOUT = 180
DEFAULT_CONFIRMED_MINUTES = 20
DEFAULT_COMMENT = "ISE_FIX_Script"

OK = "ok"
TIMEOUT = "timeout"
DNS = "dns"
UNREACHABLE = "unreachable"
AUTH = "auth"
NO_SESSION = "no-session"
REJECTED = "rejected"
FAILED = "failed"
RETRY_NEXT_CREDENTIAL = (AUTH, NO_SESSION)

_TRANSCRIPT_PATTERNS = [
    (DNS, re.compile(r"could not resolve hostname|couldn't resolve|name or service not known|"
                     r"nodename nor servname|no address associated|unknown host|"
                     r"temporary failure in name resolution|host lookup failed", re.I)),
    (UNREACHABLE, re.compile(r"connection refused|connection timed out|no route to host|"
                             r"network is unreachable|connect to host \S+ port \d+|couldn't connect|"
                             r"connection closed by remote host|connection reset by peer", re.I)),
    (AUTH, re.compile(r"permission denied|check your password|authentication failed|"
                      r"access denied|too many authentication failures|invalid password|"
                      r"error: password incorrect", re.I)),
    (NO_SESSION, re.compile(r"error: couldn't login|error: eof received|error: timeout reached|"
                            r"timed out waiting|error: unknown host", re.I)),
    # "warning: statement not found" (a delete of something absent) is deliberately NOT here:
    # Junos commits straight through it.
    (REJECTED, re.compile(r"^error: |configuration check-out failed|commit failed|syntax error|"
                          r"unknown command|is not valid|missing argument|"
                          r"configuration database locked|users currently editing", re.I | re.M)),
]
_PROMPT_RE = re.compile(r"^\S+@\S+[>#]", re.M)
_EDIT_RE = re.compile(r"^\[edit", re.M)
_COMMIT_OK_RE = re.compile(r"commit complete", re.I)
_CONFIRMED_RE = re.compile(r"commit confirmed will be automatically rolled back in (\d+) minutes", re.I)


class SessionError(Exception):
    pass


def _safe_comment(comment: str) -> str:
    cleaned = "".join(ch for ch in (comment or "").strip() if ch.isalnum() or ch in "-_.")
    return cleaned or DEFAULT_COMMENT


def validate_config_lines(lines: list[str], name: str = "fix file") -> list[str]:
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(CONFIG_VERBS):
            raise SessionError(f"{name}: {raw!r} is not a set/delete/activate/deactivate line -- "
                               "refusing to send anything")
        hit = PLACEHOLDER_RE.search(line)
        if hit:
            raise SessionError(f"{name}: placeholder {hit.group(0)} still in {line!r} -- fill it in first")
        out.append(line)
    if not out:
        raise SessionError(f"{name}: no config lines to apply")
    return out


def build_command_file(action: str, config_lines: list[str] | None = None,
                       show_commands: list[str] | None = None, commit_comment: str = DEFAULT_COMMENT,
                       confirmed_minutes: int = DEFAULT_CONFIRMED_MINUTES) -> str:
    if action not in ACTIONS:
        raise SessionError(f"unknown action {action!r}")
    comment = _safe_comment(commit_comment)
    shows = list(show_commands or [])
    if action == SNAPSHOT:
        if not shows:
            raise SessionError("snapshot needs at least one show command")
        return "\n".join(shows) + "\n"
    if action == CONFIRM:
        # Shows first (they are the verification), then the empty commit that
        # makes the pending 'commit confirmed' permanent.
        return "\n".join([*shows, "configure exclusive", f"commit comment {comment} and-quit"]) + "\n"
    if action == ROLLBACK:
        return "\n".join(["configure exclusive", "rollback 1", "show | compare",
                          f"commit comment {comment}_ROLLBACK and-quit"]) + "\n"
    if not config_lines:
        raise SessionError(f"{action} needs config lines")
    body = ["configure exclusive", *config_lines, "show | compare"]
    if action == SHOWCOMPARE:
        body += ["rollback 0", "exit"]
    else:
        body += [f"commit confirmed {int(confirmed_minutes)} comment {comment}_PENDING", "exit"]
    return "\n".join(body) + "\n"


def classify(output: str, exit_code: int | None, timed_out: bool, action: str,
             timeout: int, prof: Profile | None = None) -> tuple[str, str]:
    if timed_out:
        return TIMEOUT, f"no answer within {timeout}s"
    if prof is not None and not prof.is_junos:
        return _classify_cli(output, exit_code, action, prof)
    for verdict, pattern in _TRANSCRIPT_PATTERNS:
        m = pattern.search(output)
        if m:
            line = next((l.strip() for l in output.splitlines() if m.group(0) in l), m.group(0))
            return verdict, line[:160]
    if exit_code not in (0, None):
        return FAILED, f"jlogin exit {exit_code}"
    if not output.strip():
        return NO_SESSION, "jlogin returned nothing -- the session never reached the device"
    if action in COMMITTING_ACTIONS and not _COMMIT_OK_RE.search(output):
        return FAILED, "no 'commit complete' in the transcript -- the commit did not go through"
    if action == COMMIT and not _CONFIRMED_RE.search(output):
        return FAILED, "commit went through but Junos never armed the rollback timer -- check the box"
    if action in (SHOWCOMPARE, SNAPSHOT) and not _PROMPT_RE.search(output):
        return NO_SESSION, "no Junos prompt in the transcript -- the CLI was never reached"
    return OK, ""


def _classify_cli(output: str, exit_code: int | None, action: str, prof: Profile) -> tuple[str, str]:
    """IOS-like boxes (MRV): no commit message to look for -- OK means the enable prompt was
    reached and the box printed no `%` error after any of our lines."""
    for verdict, pattern in _TRANSCRIPT_PATTERNS[:4]:   # dns / unreachable / auth / no-session
        m = pattern.search(output)
        if m:
            line = next((l.strip() for l in output.splitlines() if m.group(0) in l), m.group(0))
            return verdict, line[:160]
    if prof.error_re is not None:
        m = prof.error_re.search(output)
        if m:
            line = next((l.strip() for l in output.splitlines() if m.group(0).strip() in l), m.group(0))
            return REJECTED, line[:160]
    if exit_code not in (0, None):
        return FAILED, f"{prof.login_binary} exit {exit_code}"
    if not output.strip():
        return NO_SESSION, f"{prof.login_binary} returned nothing -- the session never reached the device"
    if not prof.prompt_re.search(output):
        return NO_SESSION, "no CLI prompt in the transcript -- the device was never reached"
    if action in (COMMIT, CONFIRM, ROLLBACK) and not re.search(r"^[\w.-]+#", output, re.M):
        return FAILED, "the enable prompt was never reached -- nothing was configured"
    return OK, ""


def advice(verdict: str, device: str, action: str, timeout: int) -> str:
    changes = action in COMMITTING_ACTIONS
    if verdict == DNS:
        return f"{device} does not resolve on this host -- use the FQDN or the management address. Nothing was sent."
    if verdict == UNREACHABLE:
        return f"{device} refused or never answered -- down, an ACL, or the wrong address. Nothing was sent."
    if verdict == AUTH:
        return "every credential on the ladder was refused. Nothing was sent."
    if verdict == NO_SESSION:
        return "jlogin never reached the Junos CLI -- read the transcript. Nothing was sent."
    if verdict == TIMEOUT and changes:
        return (f"the session hung past {timeout}s. The commit may have gone through -- a 'commit confirmed' "
                "reverts itself; check the box before retrying.")
    if verdict == TIMEOUT:
        return f"the session hung past {timeout}s; nothing was committed. Retry once the device answers."
    if verdict == REJECTED and changes:
        return "the device rejected the configuration -- Junos aborted the whole commit. Fix the named line and retry."
    if verdict == REJECTED:
        return "the device rejected the candidate; it was rolled back, nothing was committed."
    if verdict == FAILED:
        return "see the transcript; the device record was not advanced."
    return ""


def preflight(device: str) -> str | None:
    if os.environ.get("EMS_SKIP_DNS_CHECK"):
        return None
    try:
        socket.getaddrinfo(device, None)
    except socket.gaierror as exc:
        return f"{device} does not resolve ({exc.strerror or exc})"
    except OSError as exc:
        return f"{device}: name lookup failed ({exc})"
    return None


def jlogin_binary() -> str:
    return os.environ.get("JLOGIN_BIN", "jlogin")


def jlogin_available() -> bool:
    return shutil.which(jlogin_binary()) is not None


def cloginrc_path() -> Path:
    override = os.environ.get("CLOGINRC_PATH")
    return Path(override) if override else Path.home() / ".cloginrc"


@dataclass
class JloginHealth:
    binary_found: bool
    binary_path: str
    cloginrc_found: bool
    cloginrc_permissive: bool
    message: str

    @property
    def ok(self) -> bool:
        return self.binary_found and self.cloginrc_found and not self.cloginrc_permissive


def check_jlogin_health() -> JloginHealth:
    path = shutil.which(jlogin_binary()) or ""
    rc = cloginrc_path()
    found = rc.exists()
    permissive = False
    if found and os.name != "nt":
        permissive = bool(rc.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
    if not path:
        msg = f"{jlogin_binary()} not found on PATH -- this host can generate and rehearse, not deploy"
    elif not found:
        msg = f"{rc} is missing -- jlogin has no default credentials here"
    elif permissive:
        msg = f"{rc} is readable by group/other -- chmod 600 it"
    else:
        msg = f"jlogin at {path}, credentials in {rc}"
    return JloginHealth(bool(path), path, found, permissive, msg)


@dataclass
class SessionResult:
    action: str
    device: str
    command_path: Path
    log_path: Path | None = None
    exit_code: int | None = None
    timed_out: bool = False
    output: str = ""
    dry_run: bool = False
    verdict: str = OK
    reason: str = ""
    advice: str = ""
    credential: str = ""
    attempts: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict == OK

    @property
    def has_diff(self) -> bool:
        return any(line.strip().startswith(("+", "-")) for line in self.output.splitlines())

    @property
    def confirmed_minutes(self) -> int | None:
        m = _CONFIRMED_RE.search(self.output)
        return int(m.group(1)) if m else None


def stage(log_dir: Path, device: str, action: str, command_text: str,
          timestamp: str | None = None) -> tuple[Path, str]:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{stamp}_{device}_{action}"
    command_path = log_dir / f"{base}.cmd"
    command_path.write_text(command_text, encoding="utf-8")
    return command_path, base


def login_binary_for(prof: Profile | None) -> str:
    """jlogin for Junos (JLOGIN_BIN override), the profile's script otherwise (CLOGIN_BIN override)."""
    if prof is None or prof.is_junos:
        return jlogin_binary()
    return os.environ.get("CLOGIN_BIN", os.environ.get("JLOGIN_BIN_ALL", prof.login_binary))


def _argv(command_path: Path, device: str, cloginrc: Path | None, prof: Profile | None = None) -> list[str]:
    argv = [login_binary_for(prof), *(prof.login_args if prof is not None else ())]
    if cloginrc is not None:
        argv += ["-f", str(cloginrc)]
    return argv + ["-x", str(command_path), device]


def rehearse(device: str, command_path: Path, log_dir: Path, base: str, timeout: int = DEFAULT_TIMEOUT,
             ladder: list[str] | None = None, prof: Profile | None = None) -> SessionResult:
    log_path = log_dir / f"{base}.log"
    would_run = " ".join(_argv(command_path, device, None, prof))
    body = (f"# DRY RUN -- no session was opened, {device} was not contacted.\n"
            f"# Would have run: {would_run}\n"
            f"# Credential ladder: {', '.join(ladder or ['jlogin-default'])}\n"
            f"# Timeout would be: {timeout}s\n"
            f"# Generated: {datetime.now().isoformat(timespec='seconds')}\n\n"
            "# --- the exact commands that would have been sent ---\n"
            + command_path.read_text(encoding="utf-8"))
    log_path.write_text(body, encoding="utf-8")
    return SessionResult(action=base.rsplit("_", 1)[-1], device=device, command_path=command_path,
                         log_path=log_path, exit_code=0, output=body, dry_run=True,
                         credential=(ladder or ["jlogin-default"])[0])


def _as_text(data: object) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return str(data or "")


def _kill_session(proc: subprocess.Popen) -> None:
    # jlogin is expect spawning ssh: kill the whole group or the real session
    # keeps 'configure exclusive' on the box after we gave up on it.
    if hasattr(os, "killpg") and hasattr(signal, "SIGKILL"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    proc.kill()


def _run_process(argv: list[str], timeout: int) -> tuple[str, int | None, bool]:
    kwargs = {"start_new_session": True} if os.name != "nt" else {}
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_session(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        # No second communicate() after the kill -- a grandchild holding the
        # pipe would block it forever.
        return _as_text(exc.stdout) + _as_text(exc.stderr), None, True
    return (out or "") + (err or ""), proc.returncode, False


def _attempt(device: str, command_path: Path, log_path: Path, action: str, timeout: int,
             cred: Credential, cred_dir: Path, prof: Profile | None = None) -> SessionResult:
    cloginrc = None if cred.is_default else write_temp_cloginrc(cred_dir, cred)
    started = datetime.now().isoformat(timespec="seconds")
    try:
        argv = _argv(command_path, device, cloginrc, prof)
        output, exit_code, timed_out = _run_process(argv, timeout)
    finally:
        if cloginrc is not None:
            try:
                cloginrc.unlink()
            except OSError:
                pass
    verdict, reason = classify(output, exit_code, timed_out, action, timeout, prof)
    shown = [a if a != str(cloginrc) else "<cloginrc>" for a in argv]
    result_line = f"TIMEOUT after {timeout}s" if timed_out else f"exit {exit_code}"
    header = (f"# {' '.join(shown)}\n# Credential: {cred.label}\n# Started: {started}\n"
              f"# Result: {result_line}\n# Verdict: {verdict}" + (f" -- {reason}" if reason else "") + "\n\n")
    log_path.write_text(header + output, encoding="utf-8")
    return SessionResult(action=action, device=device, command_path=command_path, log_path=log_path,
                         exit_code=exit_code, timed_out=timed_out, output=output, verdict=verdict,
                         reason=reason, credential=cred.label)


def run(device: str, command_path: Path, log_dir: Path, base: str, ladder: list[Credential],
        timeout: int = DEFAULT_TIMEOUT, cred_dir: Path | None = None, prof: Profile | None = None) -> SessionResult:
    """Try each credential in order until one reaches the device.

    Moves to the next credential only on auth / no-session. Anything else --
    the box answered (ok/rejected/failed), it's unreachable, or a commit
    timed out -- ends the ladder, because retrying could re-send a commit.
    """
    binary = login_binary_for(prof)
    if shutil.which(binary) is None:
        raise SessionError(f"{binary} not found on this host -- device access needs the box "
                           "that has the RANCID login scripts. Build and rehearse work anywhere.")
    if not ladder:
        raise SessionError("empty credential ladder")
    action = base.rsplit("_", 1)[-1]
    first_log = log_dir / f"{base}.log"
    blocked = preflight(device)
    if blocked:
        first_log.write_text(f"# {binary} -x {command_path.name} {device}\n# Result: not run -- {blocked}\n"
                             f"# Verdict: {DNS} -- {blocked}\n# No session was opened.\n", encoding="utf-8")
        return SessionResult(action=action, device=device, command_path=command_path, log_path=first_log,
                             verdict=DNS, reason=blocked, advice=advice(DNS, device, action, timeout))
    cred_dir = cred_dir or log_dir
    attempts: list[dict] = []
    result: SessionResult | None = None
    for index, cred in enumerate(ladder):
        log_path = first_log if index == 0 else log_dir / f"{base}__{index}_{cred.label}.log"
        result = _attempt(device, command_path, log_path, action, timeout, cred, cred_dir, prof)
        attempts.append({"credential": cred.label, "verdict": result.verdict, "reason": result.reason,
                         "log": log_path.name})
        if result.verdict not in RETRY_NEXT_CREDENTIAL:
            break
        if action in COMMITTING_ACTIONS and result.timed_out:
            break
    assert result is not None
    result.attempts = attempts
    result.advice = advice(result.verdict, device, action, timeout)
    return result
