"""Did the change land? Checks a confirm/recheck transcript against the fix file.

Structured matching only: an expected line must appear as a whole `set ...`
line of the device's own display-set output. The transcript also carries the
echoed commands and the fix file never echoes `set` lines, so a session that
never reached the box cannot pass by accident.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .builder import mask_secret

_SECRET_WORDS = (" secret ",)


@dataclass
class VerifyResult:
    passed: bool
    missing: list[str] = field(default_factory=list)
    still_present: list[str] = field(default_factory=list)
    checked: int = 0

    @property
    def summary(self) -> str:
        if self.passed:
            return f"PASS -- {self.checked} line(s) confirmed on the box"
        parts = []
        if self.missing:
            parts.append(f"{len(self.missing)} expected line(s) missing")
        if self.still_present:
            parts.append(f"{len(self.still_present)} deleted object(s) still present")
        return "FAIL -- " + ", ".join(parts)


def transcript_set_lines(transcript: str) -> set[str]:
    out: set[str] = set()
    for raw in transcript.splitlines():
        line = raw.strip()
        if line.startswith("set "):
            out.add(line)
    return out


def _expected_matches(expected: str, have: set[str], have_masked: set[str]) -> bool:
    if any(w in expected for w in _SECRET_WORDS):
        prefix = expected.split(" secret ", 1)[0] + " secret "
        return any(l.startswith(prefix) for l in have)
    return expected in have or mask_secret(expected) in have_masked


def check(transcript: str, config_lines: list[str], forbidden_paths: list[str]) -> VerifyResult:
    have = transcript_set_lines(transcript)
    have_masked = {mask_secret(l) for l in have}
    expected = [l for l in config_lines if l.startswith("set ")]
    missing = [l for l in expected if not _expected_matches(l, have, have_masked)]
    present = [p for p in forbidden_paths if any(l == f"set {p}" or l.startswith(f"set {p} ") for l in have)]
    return VerifyResult(passed=not missing and not present, missing=missing, still_present=present,
                        checked=len(expected) + len(forbidden_paths))


def check_rollback(transcript: str, config_lines: list[str], rollback_lines: list[str]) -> str:
    """After the timer: 'ok' if the old lines are back, 'still-applied' if the new ones remain,
    'unknown' when neither can be told."""
    have = transcript_set_lines(transcript)
    have_masked = {mask_secret(l) for l in have}
    new_sets = [l for l in config_lines if l.startswith("set ")]
    old_sets = [l for l in rollback_lines if l.startswith("set ")]
    new_present = sum(1 for l in new_sets if _expected_matches(l, have, have_masked))
    old_present = sum(1 for l in old_sets if _expected_matches(l, have, have_masked))
    if not have:
        return "unknown"
    if new_sets and new_present == len(new_sets):
        return "still-applied"
    if old_sets and old_present == len(old_sets):
        return "ok"
    if not old_sets and new_present == 0:
        return "ok"
    return "unknown"


# --------------------------------------------------------------------------- IOS-like (MRV)
def _split_statement(entry: str) -> tuple[str, str]:
    block, _, statement = entry.partition("|") if "|" in entry else ("", "", entry)
    return block, statement


def _statement_present(block: str, statement: str, blocks: dict[str, list[str]], top: list[str]) -> bool:
    pool = blocks.get(block, []) if block else top
    if " key " in statement:
        head = statement.split(" key ", 1)[0] + " key "
        return any(l.startswith(head) for l in pool)
    return statement in pool or any(l.startswith(statement + " ") for l in pool)


def check_statements(transcript: str, expected: list[str], forbidden: list[str]) -> VerifyResult:
    """For running-config platforms: every expected 'block|statement' present, every forbidden absent."""
    from . import mrv_parser
    lines = mrv_parser.config_lines(transcript)
    blocks = mrv_parser.blocks(lines)
    top = [l.strip() for l in lines if not l.startswith((" ", "	"))]
    if not lines:
        return VerifyResult(passed=False, missing=list(expected), still_present=[], checked=len(expected) + len(forbidden))
    missing = [e for e in expected if not _statement_present(*_split_statement(e), blocks, top)]
    present = [f for f in forbidden if _statement_present(*_split_statement(f), blocks, top)]
    return VerifyResult(passed=not missing and not present, missing=missing, still_present=present,
                        checked=len(expected) + len(forbidden))
