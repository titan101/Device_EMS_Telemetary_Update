from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .models import DesiredState, DeviceRecord
from .storage import PROJECT_ROOT


TEMPLATE_ROOT = PROJECT_ROOT / "templates"


def junos_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def classify_junos_platform(model: str) -> str:
    upper = (model or "").upper()
    if upper.startswith("EX"):
        return "ex"
    if upper.startswith("MX"):
        return "mx"
    if upper.startswith("PTX"):
        return "ptx"
    return "junos"


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_ROOT)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["junos_quote"] = junos_quote
    return env


def _template_name(record: DeviceRecord) -> str:
    platform = classify_junos_platform(record.model)
    candidate = Path("junos") / f"{platform}_ems_tacacs.set.j2"
    if (TEMPLATE_ROOT / candidate).exists():
        return str(candidate).replace("\\", "/")
    return "junos/standard_ems_tacacs.set.j2"


def render_junos_config(record: DeviceRecord, desired: DesiredState) -> list[str]:
    env = _environment()
    template = env.get_template(_template_name(record))
    rendered = template.render(device=record, existing=record.existing, desired=desired)
    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in rendered.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return lines
