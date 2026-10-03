"""The per-device sequence: discover -> build -> deploy -> confirm (-> recheck).

Each step returns a status record (a plain dict) and never raises past the
step boundary; the caller decides whether the next step runs. Worker threads
call these; nothing here writes shared state -- records go back to the main
thread, which persists them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import builder, config_parser, mrv_builder, mrv_parser, platforms, rancid, session, status, verify
from .credentials import Credential, CredentialSet
from .desired import DesiredState
from .models import BuildResult, DeviceFacts, ExistingConfig
from .platforms import STAGED, Profile

DISCOVER_COMMANDS = [
    "show version | no-more",
    "show configuration system | display set | no-more",
    "show configuration groups | display set | no-more",
    "show configuration snmp | display set | no-more",
    "show configuration interfaces lo0 | display set | no-more",
    "show configuration interfaces fxp0 | display set | no-more",
    "show configuration policy-options | display set | match prefix-list | no-more",
]
FIX_FILE = "01_{device}_ems_fix.txt"
BEFORE_FILE = "before_{device}.txt"


@dataclass
class RunContext:
    run_id: str
    run_dir: Path
    desired: DesiredState
    creds: CredentialSet
    live: bool = False
    rancid_folder: Path | None = None
    platform_hints: dict[str, str] = field(default_factory=dict)
    cm_number: str = ""
    confirmed_minutes: int = session.DEFAULT_CONFIRMED_MINUTES
    confirm_delay: int = 20
    timeout: int = session.DEFAULT_TIMEOUT
    comment: str = session.DEFAULT_COMMENT

    def device_dir(self, device: str) -> Path:
        return self.run_dir / "devices" / device

    def log_dir(self, device: str) -> Path:
        return self.device_dir(device) / "logs"

    def fix_path(self, device: str) -> Path:
        return self.device_dir(device) / FIX_FILE.format(device=device)

    def before_path(self, device: str) -> Path:
        return self.device_dir(device) / BEFORE_FILE.format(device=device)


@dataclass
class DeviceWork:
    """Everything one device's steps pass to each other inside a worker."""
    device: str
    facts: DeviceFacts | None = None
    existing: ExistingConfig | None = None
    build: BuildResult | None = None
    credential: str = ""
    before_source: str = ""
    profile: Profile | None = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _profile_for(ctx: RunContext, device: str) -> Profile:
    """Junos (jlogin) unless the target list says otherwise -- MRV/ADVA need their own login script
    BEFORE the first session, so they must be declared."""
    return platforms.profile(ctx.platform_hints.get(device, "")) or platforms.profile("mx")


def _base_record(step: str, ctx: RunContext, result: session.SessionResult | None = None,
                 prof: Profile | None = None) -> dict:
    rec = {"step": step, "live": ctx.live, "when": _now(), "verdict": "ok", "reason": "", "log": "",
           "credential": ""}
    if prof is not None and prof.commit_model == STAGED:
        rec["staged"] = True
    if result is not None:
        rec.update(verdict=result.verdict, reason=result.reason, credential=result.credential,
                   log=result.log_path.name if result.log_path else "", attempts=result.attempts,
                   advice=result.advice)
    return rec


def _run_session(ctx: RunContext, device: str, action: str, text: str, ladder: list[Credential],
                 prof: Profile | None = None) -> session.SessionResult:
    log_dir = ctx.log_dir(device)
    command_path, base = session.stage(log_dir, device, action, text)
    if not ctx.live:
        return session.rehearse(device, command_path, log_dir, base, timeout=ctx.timeout,
                                ladder=[c.label for c in ladder], prof=prof)
    return session.run(device, command_path, log_dir, base, ladder, timeout=ctx.timeout,
                       cred_dir=ctx.run_dir / ".creds", prof=prof)


# --------------------------------------------------------------------------- discover
def discover(ctx: RunContext, work: DeviceWork) -> dict:
    hint = ctx.platform_hints.get(work.device, "")
    prof = _profile_for(ctx, work.device)
    work.profile = prof
    if not prof.supported:
        rec = _base_record(status.DISCOVER, ctx, prof=prof)
        rec.update(verdict="unsupported", reason=f"{prof.platform}: {prof.note}", platform=prof.platform)
        return rec
    if ctx.live:
        return _discover_live_cli(ctx, work, prof) if not prof.is_junos else _discover_live(ctx, work, hint)
    if ctx.rancid_folder is None:
        rec = _base_record(status.DISCOVER, ctx, prof=prof)
        rec.update(verdict="failed", reason="dry run with no --rancid-folder: nothing to read the before-picture from")
        return rec
    return _discover_rancid(ctx, work, hint)


def _discover_live_cli(ctx: RunContext, work: DeviceWork, prof: Profile) -> dict:
    """IOS-like boxes (MRV): exec prefix, show version, show running-config."""
    text = platforms.mrv_show_session(["show version", prof.running_config_command])
    result = _run_session(ctx, work.device, session.SNAPSHOT, text, ctx.creds.discover, prof)
    rec = _base_record(status.DISCOVER, ctx, result, prof)
    if not result.ok:
        return rec
    cfg = mrv_parser.parse_config(result.output)
    if not cfg.lines:
        rec.update(verdict="failed", reason="session reached the box but no running-config came back -- read the log")
        return rec
    facts = mrv_parser.facts(work.device, result.output, cfg)
    ctx.before_path(work.device).write_text(result.output, encoding="utf-8")
    work.facts, work.existing, work.credential = facts, cfg, result.credential
    work.before_source = f"live discovery via {result.credential} ({rec['log']})"
    rec.update(platform=facts.platform, model=facts.model, version=facts.version, hostname=facts.hostname,
               platform_source=facts.platform_source, before=ctx.before_path(work.device).name,
               tacacs_servers=cfg.tacacs_servers, login_users=sorted(cfg.login_users))
    return rec


def _discover_live(ctx: RunContext, work: DeviceWork, hint: str) -> dict:
    text = session.build_command_file(session.SNAPSHOT, show_commands=DISCOVER_COMMANDS)
    result = _run_session(ctx, work.device, session.SNAPSHOT, text, ctx.creds.discover)
    rec = _base_record(status.DISCOVER, ctx, result)
    if not result.ok:
        return rec
    facts = config_parser.facts_from_show_version(work.device, result.output)
    cfg = config_parser.parse_config(result.output)
    if not cfg.lines:
        rec.update(verdict="failed", reason="session reached the box but returned no `set` lines -- read the log")
        return rec
    if hint:
        facts.platform, facts.platform_source = hint, "operator"
    elif facts.platform == "unknown":
        facts.platform, facts.platform_source = config_parser.platform_from_config(cfg.lines), "config heuristic"
    facts.hostname = facts.hostname or cfg.hostname
    facts.version = facts.version or cfg.version
    ctx.before_path(work.device).write_text(result.output, encoding="utf-8")
    work.facts, work.existing, work.credential = facts, cfg, result.credential
    work.before_source = f"live discovery via {result.credential} ({rec['log']})"
    rec.update(platform=facts.platform, model=facts.model, version=facts.version, hostname=facts.hostname,
               platform_source=facts.platform_source, before=ctx.before_path(work.device).name,
               tacacs_servers=cfg.tacacs_servers, login_users=sorted(cfg.login_users))
    return rec


def _discover_rancid(ctx: RunContext, work: DeviceWork, hint: str) -> dict:
    rec = _base_record(status.DISCOVER, ctx)
    rec["credential"] = "rancid"
    try:
        path, text = rancid.read_config(ctx.rancid_folder, work.device)
    except rancid.RancidError as exc:
        rec.update(verdict="failed", reason=str(exc))
        return rec
    if hint == "mrv" or (not hint and mrv_parser.is_mrv_text(text)):
        cfg = mrv_parser.parse_config(text)
        facts = mrv_parser.facts(work.device, text, cfg)
        work.profile = platforms.profile("mrv")
        if not cfg.lines:
            rec.update(verdict="failed", reason=f"{path.name} holds no MRV running-config")
            return rec
    else:
        cfg = config_parser.parse_config(text)
        if not cfg.lines:
            rec.update(verdict="failed", reason=f"{path.name} holds no `set` lines")
            return rec
        facts = config_parser.facts_from_config(work.device, cfg, platform_hint=hint)
    ctx.device_dir(work.device).mkdir(parents=True, exist_ok=True)
    ctx.before_path(work.device).write_text(text, encoding="utf-8")
    work.facts, work.existing, work.before_source = facts, cfg, f"RANCID {path}"
    rec.update(platform=facts.platform, model=facts.model, version=facts.version, hostname=facts.hostname,
               platform_source=facts.platform_source, before=ctx.before_path(work.device).name,
               tacacs_servers=cfg.tacacs_servers, login_users=sorted(cfg.login_users), rancid=str(path))
    return rec


# --------------------------------------------------------------------------- build
def build(ctx: RunContext, work: DeviceWork) -> dict:
    rec = _base_record(status.BUILD, ctx)
    if work.facts is None or work.existing is None:
        rec.update(verdict="failed", reason="no before-picture -- discovery did not complete")
        return rec
    protected = [c.username for c in ctx.creds.discover + ctx.creds.confirm if c.username]
    try:
        if work.facts.platform == "mrv":
            result = mrv_builder.build(work.facts, work.existing, ctx.desired)
        else:
            result = builder.build(work.facts, work.existing, ctx.desired, protected_extra=protected)
    except (builder.BuildError, mrv_builder.MrvBuildError) as exc:
        verdict = "unsupported" if "no validated template" in str(exc) else "failed"
        rec.update(verdict=verdict, reason=str(exc), platform=work.facts.platform)
        return rec
    path = ctx.fix_path(work.device)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(builder.render_file(result, work.facts, ctx.run_id, ctx.cm_number, work.before_source),
                    encoding="utf-8")
    work.build = result
    rec.update(file=path.name, sha=builder.fingerprint(path), line_count=len(result.config_lines),
               compliant=result.compliant and not result.finalize_lines, platform=result.platform,
               notes=result.notes, warnings=result.warnings, finalize_count=len(result.finalize_lines))
    return rec


# --------------------------------------------------------------------------- deploy
def deploy(ctx: RunContext, work: DeviceWork) -> dict:
    rec = _base_record(status.DEPLOY, ctx)
    path = ctx.fix_path(work.device)
    if not path.exists():
        rec.update(verdict="failed", reason="no fix file -- run build first")
        return rec
    prof = work.profile or _profile_for(ctx, work.device)
    if prof.commit_model == STAGED:
        return _deploy_staged(ctx, work, prof, path)
    try:
        lines = session.validate_config_lines(builder.config_lines_from_file(path), path.name)
    except session.SessionError as exc:
        rec.update(verdict="failed", reason=str(exc))
        return rec
    text = session.build_command_file(session.COMMIT, config_lines=lines, commit_comment=ctx.comment,
                                      confirmed_minutes=ctx.confirmed_minutes)
    ladder = _ladder_starting_with(ctx.creds.discover, work.credential)
    result = _run_session(ctx, work.device, session.COMMIT, text, ladder)
    rec = _base_record(status.DEPLOY, ctx, result)
    rec.update(file=path.name, sha=builder.fingerprint(path), has_diff=result.has_diff,
               confirmed_minutes=ctx.confirmed_minutes)
    if result.ok:
        due = datetime.now() + timedelta(minutes=result.confirmed_minutes or ctx.confirmed_minutes)
        rec["rollback_due_at"] = due.isoformat(timespec="seconds")
        work.credential = result.credential
    return rec


def _deploy_staged(ctx: RunContext, work: DeviceWork, prof: Profile, path: Path) -> dict:
    """MRV: apply inside configure terminal, NO save. The box keeps its old startup config
    until the confirm session (new AAA login proven) writes memory."""
    rec = _base_record(status.DEPLOY, ctx, prof=prof)
    lines = builder.config_lines_from_file(path)
    bad = platforms.validate_cli_lines(lines)
    if bad:
        rec.update(verdict="failed", reason=f"{path.name}: refusing to send {bad!r}")
        return rec
    if not lines:
        rec.update(verdict="failed", reason="no deploy lines (only finalize lines?) -- nothing to apply")
        return rec
    text = platforms.mrv_config_session(lines, save=False)
    ladder = _ladder_starting_with(ctx.creds.discover, work.credential)
    result = _run_session(ctx, work.device, session.COMMIT, text, ladder, prof)
    rec = _base_record(status.DEPLOY, ctx, result, prof)
    rec.update(file=path.name, sha=builder.fingerprint(path), saved=False)
    if result.ok:
        work.credential = result.credential
    return rec


def _ladder_starting_with(ladder: list[Credential], label: str) -> list[Credential]:
    """The credential that got discovery in goes first -- the others stay as fallbacks."""
    if not label:
        return list(ladder)
    first = [c for c in ladder if c.label == label]
    return first + [c for c in ladder if c.label != label]


# --------------------------------------------------------------------------- confirm
def confirm(ctx: RunContext, work: DeviceWork, rollback_due_at: str = "") -> dict:
    path = ctx.fix_path(work.device)
    if ctx.live and ctx.confirm_delay > 0:
        time.sleep(ctx.confirm_delay)
    prof = work.profile or _profile_for(ctx, work.device)
    if prof.commit_model == STAGED:
        return _confirm_staged(ctx, work, prof, path)
    shows = builder.show_commands_from_file(path) if path.exists() else ["show system commit | no-more"]
    text = session.build_command_file(session.CONFIRM, show_commands=shows, commit_comment=ctx.comment)
    result = _run_session(ctx, work.device, session.CONFIRM, text, ctx.creds.confirm)
    rec = _base_record(status.CONFIRM, ctx, result)
    rec["rollback_due_at"] = rollback_due_at
    if not ctx.live:
        return rec
    if not result.ok:
        rec["reason"] = rec["reason"] or "login through the new AAA failed -- the box will revert itself"
        return rec
    config_lines = builder.config_lines_from_file(path) if path.exists() else []
    forbidden = builder.forbidden_paths(config_lines)
    check = verify.check(result.output, config_lines, forbidden)
    rec.update(verify=check.summary, missing=check.missing[:20], still_present=check.still_present[:20])
    if not check.passed:
        rec.update(verdict="failed", reason=f"commit went through but verification {check.summary}")
    return rec


def _confirm_staged(ctx: RunContext, work: DeviceWork, prof: Profile, path: Path) -> dict:
    """New login through the confirm ladder: show running-config (verify the deploy landed),
    finalize (old TACACS hosts out), write memory, show running-config again (verify the
    removals). Nothing is saved unless this session gets in."""
    finalize = builder.finalize_lines_from_file(path) if path.exists() else []
    expected, forbidden = builder.expectations_from_file(path) if path.exists() else ([], [])
    text = platforms.mrv_confirm_session([prof.running_config_command], finalize)
    result = _run_session(ctx, work.device, session.CONFIRM, text, ctx.creds.confirm, prof)
    rec = _base_record(status.CONFIRM, ctx, result, prof)
    rec["finalize_count"] = len(finalize)
    if not ctx.live:
        return rec
    if not result.ok:
        rec["reason"] = rec["reason"] or "login through the new AAA failed -- nothing was saved"
        return rec
    check = verify.check_statements(_last_running_config(result.output, prof), expected, forbidden)
    rec.update(verify=check.summary, missing=check.missing[:20], still_present=check.still_present[:20], saved=True)
    if not check.passed:
        rec.update(verdict="failed", reason=f"config saved but verification {check.summary} -- read the log")
    return rec


def _last_running_config(transcript: str, prof: Profile) -> str:
    """The confirm session shows the config twice (before and after finalize): judge the last one."""
    marker = prof.running_config_command
    idx = transcript.rfind(marker)
    return transcript[idx + len(marker):] if idx >= 0 else transcript


def _recheck_staged(ctx: RunContext, work: DeviceWork, prof: Profile, path: Path) -> dict:
    """MRV has no timer: the tool itself puts the pre-change statements back (the startup
    config was never saved, so only the running config needs undoing), then checks."""
    rec = _base_record(status.RECHECK, ctx, prof=prof)
    rollback = builder.rollback_lines_from_file(path) if path.exists() else []
    expected, _ = builder.expectations_from_file(path) if path.exists() else ([], [])
    if not rollback:
        rec.update(verdict="unknown", reason="no rollback lines in the fix file")
        return rec
    bad = platforms.validate_cli_lines(rollback)
    if bad:
        rec.update(verdict="failed", reason=f"refusing to send rollback line {bad!r}")
        return rec
    body = platforms.mrv_config_session(rollback, save=False).rstrip("\n").rsplit("\n", 1)[0]  # drop trailing exit
    text = body + f"\n{prof.running_config_command}\nexit\n"
    result = _run_session(ctx, work.device, session.ROLLBACK, text, ctx.creds.discover, prof)
    rec = _base_record(status.RECHECK, ctx, result, prof)
    if not ctx.live or not result.ok:
        if ctx.live and not result.ok:
            rec["reason"] = rec["reason"] or "could not log in to roll the box back"
        return rec
    still = verify.check_statements(_last_running_config(result.output, prof), [], expected)
    rec["verdict"] = "ok" if not still.still_present else "still-applied"
    if still.still_present:
        rec["reason"] = f"{len(still.still_present)} new statement(s) still in the running config after the rollback"
    return rec


# --------------------------------------------------------------------------- recheck
def recheck(ctx: RunContext, work: DeviceWork, due_at: str) -> dict:
    rec = _base_record(status.RECHECK, ctx)
    path = ctx.fix_path(work.device)
    prof = work.profile or _profile_for(ctx, work.device)
    if prof.commit_model == STAGED:
        return _recheck_staged(ctx, work, prof, path)
    if due_at:
        try:
            remaining = (datetime.fromisoformat(due_at) - datetime.now()).total_seconds()
        except ValueError:
            remaining = 0
        if remaining > 0:
            rec.update(verdict="too-early", reason=f"rollback timer still running, due {due_at}")
            return rec
    shows = builder.show_commands_from_file(path) if path.exists() else ["show configuration system tacplus-server | display set | no-more"]
    text = session.build_command_file(session.SNAPSHOT, show_commands=shows)
    result = _run_session(ctx, work.device, session.SNAPSHOT, text, ctx.creds.discover)
    rec = _base_record(status.RECHECK, ctx, result)
    if not ctx.live or not result.ok:
        return rec
    verdict = verify.check_rollback(result.output, builder.config_lines_from_file(path),
                                    builder.rollback_lines_from_file(path))
    rec["verdict"] = verdict
    if verdict == "still-applied":
        rec["reason"] = "the new config is still on the box -- confirm it by hand or roll back"
    elif verdict == "unknown":
        rec["reason"] = "neither the old nor the new lines match cleanly -- read the log"
    return rec
