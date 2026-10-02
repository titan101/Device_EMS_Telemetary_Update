"""RANCID folder reader: one `display set` dump per device, named after the host.

Used for offline planning (build + MOP without logging in) and as the
"before" picture when a device was never discovered live. Lookup is
case-insensitive and tolerates a few file-name conventions; more than one
match is an error, never a silent first-pick.
"""
from __future__ import annotations

import re
from pathlib import Path

_SUFFIXES = ("", ".txt", ".cfg", ".conf", ".snap", ".log")


class RancidError(RuntimeError):
    pass


def candidates(folder: Path, device: str) -> list[Path]:
    wanted = device.lower()
    short = wanted.split(".", 1)[0]
    hits: list[Path] = []
    try:
        entries = list(folder.iterdir())
    except OSError as exc:
        raise RancidError(f"cannot read RANCID folder {folder}: {exc}") from exc
    for path in entries:
        if not path.is_file():
            continue
        name = path.name.lower()
        for suffix in _SUFFIXES:
            if name == wanted + suffix or name == short + suffix:
                hits.append(path)
                break
    return sorted(hits)


def read_config(folder: Path, device: str) -> tuple[Path, str]:
    hits = candidates(folder, device)
    if not hits:
        raise RancidError(f"no RANCID config for {device} in {folder}")
    if len(hits) > 1:
        names = ", ".join(p.name for p in hits)
        raise RancidError(f"{device} matches more than one file in {folder}: {names} -- rename one")
    return hits[0], hits[0].read_text(encoding="utf-8", errors="replace")


def set_lines(text: str) -> list[str]:
    """Only the `set ...` lines of a dump; RANCID headers, prompts and banners drop out."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("set "):
            out.append(line)
    return out


_HOSTNAME_RE = re.compile(r"^set system host-name (\S+)", re.M)


def hostname_from_dump(text: str) -> str:
    m = _HOSTNAME_RE.search(text)
    return m.group(1).strip('"') if m else ""
