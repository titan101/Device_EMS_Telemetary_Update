from __future__ import annotations

import platform
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed


def parse_targets(raw: str) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    for line in raw.replace(",", "\n").splitlines():
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        for token in value.split():
            if token and token not in seen:
                seen.add(token)
                targets.append(token)
    return targets


def _ping_command(host: str, timeout_ms: int) -> list[str]:
    if platform.system().lower().startswith("win"):
        return ["ping", "-n", "1", "-w", str(timeout_ms), host]
    timeout_seconds = max(1, round(timeout_ms / 1000))
    return ["ping", "-c", "1", "-W", str(timeout_seconds), host]


def ping_host(host: str, timeout_ms: int = 1000) -> tuple[bool, float | None, str]:
    try:
        completed = subprocess.run(
            _ping_command(host, timeout_ms),
            capture_output=True,
            text=True,
            timeout=max(2, timeout_ms / 1000 + 1),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, None, str(exc)
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    match = re.search(r"time[=<]\s*([0-9.]+)\s*ms", output, flags=re.IGNORECASE)
    latency = float(match.group(1)) if match else None
    return completed.returncode == 0, latency, output


def ping_many(targets: list[str], timeout_ms: int = 1000, max_workers: int = 16) -> dict[str, tuple[bool, float | None, str]]:
    results: dict[str, tuple[bool, float | None, str]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(ping_host, target, timeout_ms): target for target in targets}
        for future in as_completed(future_map):
            target = future_map[future]
            results[target] = future.result()
    return results
