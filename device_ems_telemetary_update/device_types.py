from __future__ import annotations

import re


def classify_junos_platform(model: str) -> str:
    upper = (model or "").upper()
    if upper.startswith("EX"):
        return "ex"
    if upper.startswith("MX"):
        return "mx"
    if upper.startswith("PTX"):
        return "ptx"
    return "junos"


def device_type_keys(model: str) -> list[str]:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", model or "").lower()
    keys = []
    if cleaned:
        keys.append(cleaned)
        family = re.match(r"([a-z]+)", cleaned)
        if family:
            keys.append(family.group(1))
    platform = classify_junos_platform(model)
    keys.append(platform)
    keys.append("junos")
    seen: set[str] = set()
    return [key for key in keys if key and not (key in seen or seen.add(key))]
