"""Append-only error log under logs/errors.log.

Two levels: ERROR is an unexpected exception with its traceback, FAILED is a
handled failure worth one line. Writing here must never break a run, so every
failure of the log itself is swallowed on purpose.
"""
from __future__ import annotations

import os
import traceback
from datetime import datetime
from pathlib import Path

LOG_NAME = "errors.log"
MAX_BYTES = 2_000_000
SEPARATOR = "-" * 78


def log_path(root: Path) -> Path:
    return Path(root) / "logs" / LOG_NAME


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_suffix(".log.1"))
    except OSError:
        pass


def record(root: Path, context: str, exc: BaseException | None = None,
           message: str = "", level: str = "") -> Path | None:
    level = level or ("ERROR" if exc is not None else "FAILED")
    path = log_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        stamp = datetime.now().isoformat(timespec="seconds")
        lines = [SEPARATOR, f"[{stamp}] {level}  pid={os.getpid()}  {context}"]
        if message:
            lines.append(f"  {message}")
        if exc is not None:
            lines.append(f"  {type(exc).__name__}: {exc}")
            lines.append("")
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            lines.extend("  " + l for l in tb.splitlines())
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path
    except Exception:  # noqa: BLE001 -- see module docstring
        return None


def entries(root: Path, limit: int = 25) -> list[str]:
    try:
        text = log_path(root).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    chunks = [c.strip() for c in text.split(SEPARATOR) if c.strip()]
    return list(reversed(chunks))[:limit]


def count(root: Path) -> int:
    try:
        return log_path(root).read_text(encoding="utf-8", errors="replace").count(SEPARATOR)
    except OSError:
        return 0
