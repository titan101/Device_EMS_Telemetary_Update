from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .device_types import classify_junos_platform, device_type_keys
from .models import DesiredState, DeviceRecord
from .storage import PROJECT_ROOT


TEMPLATE_ROOT = PROJECT_ROOT / "templates"
JUNOS_CHANGE_TEMPLATE_DIR = TEMPLATE_ROOT / "junos" / "change_templates"
DEFAULT_JUNOS_TEMPLATE_IDS = [
    "tacacs_users",
    "snmp_communities",
    "ntp_servers",
    "syslog_hosts",
    "netconf_lldp",
]


@dataclass(frozen=True)
class TemplateSpec:
    template_id: str
    label: str
    description: str
    path: str
    platforms: tuple[str, ...] = ("junos", "ex", "mx")
    device_types: tuple[str, ...] = ()


def junos_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_ROOT)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["junos_quote"] = junos_quote
    return env


def _template_id(path: Path) -> str:
    name = path.name
    if name.endswith(".set.j2"):
        return name[: -len(".set.j2")]
    return path.stem


def _metadata(path: Path) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines()[:12]:
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        key, _, value = stripped[1:].partition(":")
        if key and value:
            metadata[key.strip().lower()] = value.strip()
    return metadata


def available_junos_templates() -> list[TemplateSpec]:
    specs: list[TemplateSpec] = []
    for path in sorted(JUNOS_CHANGE_TEMPLATE_DIR.glob("*.set.j2")):
        metadata = _metadata(path)
        template_id = metadata.get("id") or _template_id(path)
        platforms = tuple(
            item.strip().lower()
            for item in metadata.get("platforms", "junos,ex,mx").split(",")
            if item.strip()
        )
        device_types = tuple(
            item.strip().lower()
            for item in metadata.get("device_types", "").split(",")
            if item.strip()
        )
        specs.append(
            TemplateSpec(
                template_id=template_id,
                label=metadata.get("label") or template_id.replace("_", " ").title(),
                description=metadata.get("description", ""),
                path=str(path.relative_to(TEMPLATE_ROOT)).replace("\\", "/"),
                platforms=platforms or ("junos",),
                device_types=device_types,
            )
        )
    return specs


def selected_junos_templates(desired: DesiredState, record: DeviceRecord) -> list[TemplateSpec]:
    selected_ids = desired.selected_templates or DEFAULT_JUNOS_TEMPLATE_IDS
    selected_set = set(selected_ids)
    platform = classify_junos_platform(record.model)
    type_keys = set(device_type_keys(record.model or record.device_type))
    specs = []
    for spec in available_junos_templates():
        if spec.template_id not in selected_set:
            continue
        if platform not in spec.platforms:
            continue
        if spec.device_types and not type_keys.intersection(spec.device_types):
            continue
        specs.append(spec)
    return specs


def render_junos_config(record: DeviceRecord, desired: DesiredState) -> list[str]:
    env = _environment()
    lines: list[str] = []
    seen: set[str] = set()
    for spec in selected_junos_templates(desired, record):
        template = env.get_template(spec.path)
        rendered = template.render(device=record, existing=record.existing, desired=desired, template=spec)
        for raw_line in rendered.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line not in seen:
                seen.add(line)
                lines.append(line)
    return lines
