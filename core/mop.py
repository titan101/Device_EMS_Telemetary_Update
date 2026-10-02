"""Method of Procedure for a run: markdown and a self-contained HTML twin."""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from . import builder, status
from .pipeline import FIX_FILE


def _fix_text(run_dir: Path, device: str) -> tuple[list[str], list[str], list[str]]:
    path = run_dir / "devices" / device / FIX_FILE.format(device=device)
    if not path.exists():
        return [], [], []
    return (builder.config_lines_from_file(path), builder.show_commands_from_file(path),
            builder.rollback_lines_from_file(path))


def build_markdown(run_id: str, meta: dict, rows: list[dict], run_dir: Path, confirmed_minutes: int,
                   comment: str) -> str:
    cm = meta.get("cm_number") or "(no CM number)"
    out = [f"# MOP -- EMS standard / ISE TACACS update -- {run_id}", "",
           f"**Change:** {cm}  ", f"**Generated:** {datetime.now().isoformat(timespec='minutes')}  ",
           f"**Devices:** {len(rows)}  ", f"**Commit style:** `commit confirmed {confirmed_minutes}` "
           f"then a second login through the new AAA that runs `commit comment {comment}`", ""]
    out += ["## Scope", "", "| device | platform | state now | detail |", "|---|---|---|---|"]
    for row in rows:
        disc = row["records"].get(status.DISCOVER) or row["records"].get(status.rehearsal_key(status.DISCOVER)) or {}
        out.append(f"| {row['device']} | {disc.get('platform', '?')} {disc.get('model', '')} | {row['state']} | {row['detail']} |")
    out += ["", "## Pre-checks", "",
            "1. `cli.py check` -- jlogin reachable, `~/.cloginrc` is 0600, credentials.json ladders load, desired_state.json has no placeholders.",
            "2. The run has been **rehearsed** (`cli.py rehearse <run> --rancid-folder ...`) and every fix file below was read.",
            "3. The ISE account in `~/.cloginrc` logs into a box that is already on the new servers (otherwise every confirm fails and every device rolls back).",
            "4. Console armed (`LIVE` typed) or `--live --yes` on the command line.", "",
            "## Per-device procedure", "",
            f"Each device, up to N in parallel: discover -> build -> `commit confirmed {confirmed_minutes}` -> "
            "second login + verify -> `commit`. A device whose second login fails is left alone: it reverts itself "
            f"when the {confirmed_minutes}-minute timer expires, and `cli.py recheck <run> --live` proves it did.", ""]
    for row in rows:
        config, verify, rollback = _fix_text(run_dir, row["device"])
        out += [f"### {row['device']}", ""]
        if not config:
            out += ["_No fix file yet (not built) or COMPLIANT -- nothing to send._", ""]
            continue
        out += ["```", *config, "```", "", "Verify:", "", "```", *verify, "```", "", "Rollback (after a confirm):",
                "", "```", *rollback, "```", ""]
    out += ["## Back-out", "",
            f"* Before confirm: do nothing -- the device reverts at `commit confirmed` expiry ({confirmed_minutes} min).",
            "* After confirm: paste the device's Rollback block above in `configure exclusive`, `commit comment "
            f"{comment}_ROLLBACK and-quit`.", "",
            "## After the window", "", "* `cli.py status <run>` -- every device CONFIRMED or COMPLIANT.",
            "* `cli.py recheck <run> --live` for any ROLLBACK PENDING device, then fix those by hand.",
            "* `cli.py ledger --export` for the change record.", ""]
    return "\n".join(out)


def markdown_to_html(title: str, md: str) -> str:
    """Minimal: headings, paragraphs, tables, fenced blocks. Enough for a MOP attachment."""
    lines = md.splitlines()
    body: list[str] = []
    in_code = False
    in_table = False
    for line in lines:
        if line.startswith("```"):
            body.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            body.append(html.escape(line))
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set(line.replace("|", "").strip()) <= {"-", " "}:
                continue
            if not in_table:
                body.append("<table>")
                in_table = True
                body.append("<tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in cells) + "</tr>")
            else:
                body.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            body.append("</table>")
            in_table = False
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            body.append(f"<h{level}>{html.escape(line[level:].strip())}</h{level}>")
        elif line.startswith(("* ", "1. ", "2. ", "3. ", "4. ")):
            body.append(f"<li>{html.escape(line.split(' ', 1)[1])}</li>")
        elif line.strip():
            text = html.escape(line).replace("**", "")
            body.append(f"<p>{text}</p>")
    if in_table:
        body.append("</table>")
    css = ("body{font-family:system-ui,sans-serif;max-width:1100px;margin:30px auto;padding:0 20px;color:#171a1f}"
           "pre{background:#0c0f16;color:#d7deea;padding:12px;border-radius:8px;overflow-x:auto;font-size:13px}"
           "table{border-collapse:collapse;width:100%}th,td{border:1px solid #dde1e6;padding:6px 8px;text-align:left;font-size:14px}"
           "h1{border-bottom:3px solid #f59e0b;padding-bottom:6px}h3{margin-top:28px;color:#b45309}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>{css}</style></head><body>" + "\n".join(body) + "</body></html>")


def write_mop(run_dir: Path, run_id: str, meta: dict, rows: list[dict], confirmed_minutes: int,
              comment: str) -> tuple[Path, Path]:
    md = build_markdown(run_id, meta, rows, run_dir, confirmed_minutes, comment)
    md_path = run_dir / f"MOP_{run_id}.md"
    html_path = run_dir / f"MOP_{run_id}.html"
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(markdown_to_html(f"MOP {run_id}", md), encoding="utf-8")
    return md_path, html_path
