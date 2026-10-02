from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_ROOT = ROOT / "templates"

# platform -> template path (relative to templates/). ACX follows the MX standard.
TEMPLATE_FOR = {
    "mx": "junos/mx/ems_fix.set.j2",
    "acx": "junos/mx/ems_fix.set.j2",
    "ex": "junos/ex/ems_fix.set.j2",
    "srx": "junos/srx/ems_fix.set.j2",
}
UNVALIDATED = {"srx": "no real SRX configuration has been reviewed yet -- rehearse and read the diff first"}


def junos_quote(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def environment() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_ROOT)), undefined=StrictUndefined,
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=False)
    env.filters["q"] = junos_quote
    return env


def template_path(platform: str) -> str:
    return TEMPLATE_FOR.get(platform, "")


def list_templates() -> list[dict]:
    out = []
    for path in sorted(TEMPLATE_ROOT.rglob("*.j2")):
        rel = path.relative_to(TEMPLATE_ROOT).as_posix()
        platforms = [p for p, t in TEMPLATE_FOR.items() if t == rel]
        out.append({"path": rel, "platforms": platforms, "text": path.read_text(encoding="utf-8")})
    return out
