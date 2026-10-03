"""Build, minimise, split (deploy / finalize), verify and roll back MRV CLI changes.

Lines are IOS-like: a block header, one-space-indented children, `exit`. A
removal is `no <statement>`. The before-picture is the parsed running-config.
"""
from __future__ import annotations

import re

from . import templates as tpl
from .models import BuildResult, DeviceFacts, ExistingConfig

_KEY_RE = re.compile(r"( key )(\S+)")
BLOCK_END = "exit"


class MrvBuildError(RuntimeError):
    pass


def mask_key(line: str) -> str:
    return _KEY_RE.sub(r"\1<secret>", line)


COMMUNITY_INDEX_START = 40


def community_indexer(existing: ExistingConfig, d: dict):
    """Existing string keeps its index; new strings take the lowest free index from 40 up."""
    used = {int(i) for i in existing.snmp_community_ids.values() if str(i).isdigit()}
    assigned: dict[str, int] = {}

    def index_for(name: str) -> str:
        if name in existing.snmp_community_ids:
            return existing.snmp_community_ids[name]
        if name not in assigned:
            candidate = COMMUNITY_INDEX_START
            while candidate in used or candidate in assigned.values():
                candidate += 1
            assigned[name] = candidate
        return str(assigned[name])
    return index_for


def render(existing: ExistingConfig, d: dict) -> list[str]:
    text = tpl.environment().get_template(tpl.TEMPLATE_FOR["mrv"]).render(
        d=d, existing=existing, community_index=community_indexer(existing, d))
    return [l.rstrip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


def structure(lines: list[str]) -> list[tuple[str, str]]:
    """[(block, statement)] -- block '' for top-level statements; `exit` closes a block."""
    out: list[tuple[str, str]] = []
    block = ""
    for line in lines:
        stripped = line.strip()
        if stripped == BLOCK_END:
            block = ""
            continue
        if line.startswith((" ", "\t")):
            out.append((block, stripped))
        else:
            if stripped.startswith("no "):
                out.append(("", stripped))
                continue
            # a bare statement opens a block only if children follow; decided by the caller
            out.append(("", stripped))
            block = stripped
    return out


def _has(existing: ExistingConfig, block: str, statement: str, mask: bool) -> bool:
    pool = existing.blocks.get(block, []) if block else [l.strip() for l in existing.lines if not l.startswith((" ", "\t"))]
    if mask:
        return mask_key(statement) in {mask_key(l) for l in pool}
    return statement in pool


def minimise(rendered: list[str], existing: ExistingConfig, rotate_secret: bool) -> list[str]:
    """Keep only statements the box lacks (and removals of things it has); drop empty blocks."""
    out: list[str] = []
    block_header: str | None = None
    block_children: list[str] = []

    def flush() -> None:
        nonlocal block_header, block_children
        if block_header is not None and block_children:
            out.append(block_header)
            out.extend(block_children)
            out.append(BLOCK_END)
        block_header, block_children = None, []

    for line in rendered:
        stripped = line.strip()
        if stripped == BLOCK_END:
            flush()
            continue
        if line.startswith((" ", "\t")):
            if block_header is None:
                continue
            if _keep(existing, block_header, stripped, rotate_secret):
                block_children.append(" " + stripped)
            continue
        flush()
        if _looks_like_block_header(rendered, line):
            block_header = stripped
            continue
        if _keep(existing, "", stripped, rotate_secret):
            out.append(stripped)
    flush()
    return out


def _looks_like_block_header(rendered: list[str], line: str) -> bool:
    idx = rendered.index(line)
    return idx + 1 < len(rendered) and rendered[idx + 1].startswith((" ", "\t"))


def _keep(existing: ExistingConfig, block: str, statement: str, rotate_secret: bool) -> bool:
    if statement.startswith("no "):
        target = statement[3:]
        pool = existing.blocks.get(block, []) if block else [l.strip() for l in existing.lines]
        return any(l == target or l.startswith(target + " ") for l in pool)
    mask = not rotate_secret or " key " not in statement
    return not _has(existing, block, statement, mask)


def split_finalize(config_lines: list[str], existing: ExistingConfig, d: dict) -> tuple[list[str], list[str]]:
    """Old TACACS hosts leave in the confirm session, after ISE has proven itself."""
    finalize = [f"no tacacs-server host {s}" for s in existing.tacacs_servers if s not in d["tacacs"]["servers"]]
    return config_lines, finalize


def expectations(config_lines: list[str], finalize_lines: list[str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(expected, forbidden) as (block, statement) pairs for the running-config check.
    The deploy lines must be present; removals (deploy or finalize) must be absent."""
    expected: list[tuple[str, str]] = []
    forbidden: list[tuple[str, str]] = []
    block = ""
    for line in config_lines + finalize_lines:
        stripped = line.strip()
        if stripped == BLOCK_END:
            block = ""
            continue
        child = line.startswith((" ", "\t"))
        if not child:
            if stripped.startswith("no "):
                forbidden.append(("", stripped[3:]))
                continue
            if _is_header(stripped):
                block = stripped
                continue
            expected.append(("", stripped))
            continue
        if stripped.startswith("no "):
            forbidden.append((block, stripped[3:]))
        else:
            expected.append((block, stripped))
    return expected, forbidden


def _is_header(statement: str) -> bool:
    return statement in ("aaa", "ntp", "snmp") or statement.startswith("interface ")


def rollback_lines(config_lines: list[str], finalize_lines: list[str], existing: ExistingConfig) -> list[str]:
    """Inverse of what was sent, block by block, restoring removed statements verbatim."""
    out: list[str] = []
    block = ""
    pending: list[str] = []

    def flush() -> None:
        nonlocal pending
        if block and pending:
            out.append(block)
            out.extend(" " + p for p in pending)
            out.append(BLOCK_END)
        elif pending:
            out.extend(pending)
        pending = []

    for line in config_lines + finalize_lines:
        stripped = line.strip()
        if stripped == BLOCK_END:
            flush()
            block = ""
            continue
        if not line.startswith((" ", "\t")) and _is_header(stripped):
            flush()
            block = stripped
            continue
        if stripped.startswith("no "):
            target = stripped[3:]
            pool = existing.blocks.get(block, []) if block else [l.strip() for l in existing.lines]
            pending.extend(l for l in pool if l == target or l.startswith(target + " "))
        else:
            original = _original_for(existing, block, stripped)
            pending.append(original if original else "no " + stripped.split(" key ")[0])
    flush()
    return out


def _original_for(existing: ExistingConfig, block: str, statement: str) -> str:
    """A pre-change statement that the new one replaces (same leading tokens), if any."""
    head = statement.split(" key ")[0] if " key " in statement else " ".join(statement.split()[:2])
    pool = existing.blocks.get(block, []) if block else [l.strip() for l in existing.lines if not l.startswith((" ", "\t"))]
    for line in pool:
        if line == head or line.startswith(head + " "):
            return line
    return ""


def verify_commands() -> list[str]:
    return ["show running-config"]


def build(facts: DeviceFacts, existing: ExistingConfig, desired) -> BuildResult:
    d = desired.for_platform("mrv")
    holes = desired.placeholders("mrv")
    if holes:
        raise MrvBuildError("desired_state.json still has placeholders for mrv: " + "; ".join(holes[:5]))
    if not d["tacacs"]["servers"] or not d["tacacs"]["secret"]:
        raise MrvBuildError("desired_state.json: tacacs.servers and tacacs.secret are required")
    rendered = render(existing, d)
    config = minimise(rendered, existing, rotate_secret=bool(d["tacacs"].get("rotate_secret", False)))
    config, finalize = split_finalize(config, existing, d)
    notes = ["MRV: applied without `write memory`; the confirm session verifies, removes the old TACACS "
             "hosts and saves"]
    if not existing.login_users:
        notes.append("no local users in the running-config; `authentication login default tacacs+ local` "
                     "falls back to the built-in local account")
    expected, forbidden = expectations(config, finalize)
    result = BuildResult(device=facts.device, platform="mrv", config_lines=config, verify_commands=verify_commands(),
                         rollback_lines=rollback_lines(config, finalize, existing), notes=notes, warnings=[],
                         forbidden_paths=[f"{b}|{s}" if b else s for b, s in forbidden])
    result.finalize_lines = finalize
    result.expected_statements = [f"{b}|{s}" if b else s for b, s in expected]
    return result
