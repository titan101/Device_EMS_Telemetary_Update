#!/usr/bin/env python3
"""Device EMS Console -- a thin Flask launcher over cli.py.

Every button builds a cli.py argv, runs it as a subprocess and tails its log;
the device board is read from runs/<run>/status.json. Binds 127.0.0.1 only.
Starts DISARMED: nothing can reach a device until LIVE is typed, and even then
each live action carries its own tick.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template_string, request, send_file, url_for
from werkzeug.exceptions import HTTPException

import cli
from core import credentials, desired, errorlog, ledger, runs, session, status
from core import templates as tpl

SCRIPT_DIR = Path(__file__).resolve().parent
RUNS_DIR = SCRIPT_DIR / "webapp_runs"
SETTINGS_FILE = SCRIPT_DIR / "webapp_settings.json"
DEFAULT_PORT = 5460
THEMES = ("dark", "gray", "light")
DEFAULT_SETTINGS = {"theme": "dark", "rancid_folder": "", "workers": 8}

app = Flask(__name__)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
LIVE_MODE = {"armed": False, "since": None}
STARTED = datetime.now()


# --------------------------------------------------------------------------- guards
@app.before_request
def _reject_cross_origin_state_change():
    if request.method != "POST":
        return None
    for header in ("Origin", "Referer"):
        value = request.headers.get(header)
        if not value:
            continue
        host = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value).split("/", 1)[0]
        if host != request.host:
            return Response("Refused: Origin/Referer doesn't match this server (cross-site request).", status=403)
    return None


def _armed() -> bool:
    return bool(LIVE_MODE["armed"])


def _settings() -> dict:
    out = dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            out.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return out


def _save_settings(values: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(values, indent=2), encoding="utf-8")


def _py() -> str:
    return sys.executable


# --------------------------------------------------------------------------- jobs
def launch(kind: str, argv: list[str], run_id: str = "") -> str:
    RUNS_DIR.mkdir(exist_ok=True)
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    log_path = RUNS_DIR / f"{job_id}.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([_py(), str(SCRIPT_DIR / "cli.py"), *argv], stdout=log_file, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, cwd=SCRIPT_DIR, text=True,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    with JOBS_LOCK:
        JOBS[job_id] = {"kind": kind, "argv": argv, "proc": proc, "log_path": log_path, "log_file": log_file,
                        "run_id": run_id, "started": datetime.now().isoformat(timespec="seconds"), "reported": False}
    return job_id


def _job_running(job: dict) -> bool:
    return job["proc"].poll() is None


def _active_job_for(run_id: str) -> str | None:
    with JOBS_LOCK:
        for job_id, job in JOBS.items():
            if job["run_id"] == run_id and _job_running(job):
                return job_id
    return None


def _job_log(job_id: str) -> tuple[str, bool]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return "(unknown job)", False
    running = _job_running(job)
    if not running:
        try:
            job["log_file"].close()
        except OSError:
            pass
        if job["proc"].returncode not in (0, None) and not job["reported"]:
            job["reported"] = True
            errorlog.record(SCRIPT_DIR, f"console job {job['kind']}: {' '.join(job['argv'])}",
                            message=f"exited {job['proc'].returncode} -- see {job['log_path'].name}", level="FAILED")
    try:
        text = job["log_path"].read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return text[-60000:], running


# --------------------------------------------------------------------------- helpers
def _board(run_dir: Path, devices: list[str]) -> list[dict]:
    rows = runs.device_states(run_dir, devices)
    for row in rows:
        recs = row["records"]
        disc = recs.get(status.DISCOVER) or recs.get(status.rehearsal_key(status.DISCOVER)) or {}
        row["platform"] = disc.get("platform", "")
        row["model"] = disc.get("model", "")
        row["chip"] = _chip_class(row["state"])
        dev_dir = run_dir / "devices" / row["device"]
        row["fix"] = f"01_{row['device']}_ems_fix.txt" if (dev_dir / f"01_{row['device']}_ems_fix.txt").exists() else ""
        logs = sorted((dev_dir / "logs").glob("*.log")) if (dev_dir / "logs").is_dir() else []
        row["log"] = logs[-1].name if logs else ""
        row["rollback_due_at"] = (recs.get(status.DEPLOY) or {}).get("rollback_due_at", "")
    return rows


def _chip_class(state: str) -> str:
    if state in (status.ST_CONFIRMED, status.ST_COMPLIANT):
        return "green"
    if state in (status.ST_ROLLBACK_PENDING, status.ST_PENDING_CONFIRM, status.ST_ROLLED_BACK):
        return "amber"
    if state in status.BAD_STATES:
        return "red"
    if state == status.ST_NEW:
        return ""
    return "teal"


def _resolve_run(run_id: str):
    try:
        return runs.load_run(cli.RUNS_ROOT, run_id)
    except runs.RunError:
        return None


def _device_file(run_dir: Path, device: str, name: str) -> Path | None:
    try:
        candidate = (run_dir / "devices" / device / name).resolve()
        if name.startswith("logs/") or "/" in name or "\\" in name:
            candidate = (run_dir / "devices" / device / "logs" / Path(name).name).resolve()
        candidate.relative_to(cli.RUNS_ROOT.resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _desired_summary() -> dict:
    path = cli.CONFIG_DIR / desired.DESIRED_FILE
    out = {"exists": path.exists(), "path": str(path), "error": "", "placeholders": [], "platforms": {}}
    if not path.exists():
        return out
    try:
        des = desired.load_desired(path)
    except desired.DesiredError as exc:
        out["error"] = str(exc)
        return out
    for platform in tpl.TEMPLATE_FOR:
        d = des.for_platform(platform)
        out["platforms"][platform] = {"tacacs": d["tacacs"]["servers"], "classes": len(d["login"]["classes"]),
                                      "users": len(d["login"]["users"]), "ntp": d["ntp"]["servers"],
                                      "syslog": list(d["syslog"]["hosts"]), "communities": len(d["snmp"]["communities"])}
    out["placeholders"] = des.placeholders("mx") + [h for h in des.placeholders("ex") if h not in des.placeholders("mx")]
    out["commit"] = des.commit
    return out


def _creds_summary() -> dict:
    path = cli.CONFIG_DIR / credentials.CREDENTIALS_FILE
    try:
        creds = credentials.load_credentials(path)
        return {"path": str(path), "exists": path.exists(), "error": "", **credentials.public_summary(creds)}
    except credentials.CredentialError as exc:
        return {"path": str(path), "exists": path.exists(), "error": str(exc), "discover": [], "confirm": []}


# --------------------------------------------------------------------------- CSS + shell
BASE_CSS = r"""
:root{--bg:#0c0f16;--ink:#080a10;--panel:#141925;--panel2:#1c2333;--line:#2a3345;--line2:#3a4660;--text:#eef1f7;--muted:#98a2b8;
--brand:#ffb454;--brand-strong:#f59e0b;--old:#ff7b72;--new:#7ee787;--red:#ff5c5c;--red-bg:#3b1a1f;--amber:#ffb454;--amber-bg:#3a2a12;
--green:#7ee787;--glow1-rgb:30,36,64;--glow2-rgb:42,31,20;--term-bg:var(--ink);--term-text:#d7deea;
--mono:ui-monospace,"Cascadia Mono",Consolas,"Courier New",monospace}
:root[data-theme="gray"]{--bg:#1e2126;--ink:#16181c;--panel:#25282e;--panel2:#2d3138;--line:#3a3f47;--line2:#4a4f58;
--text:#e8eaed;--muted:#9198a1;--old:#ff8a7a;--new:#8fe89a;--red:#ff6b6b;--red-bg:#3d2020;--amber-bg:#3d2f18;--green:#8fe89a;
--glow1-rgb:42,47,58;--glow2-rgb:54,43,31}
:root[data-theme="light"]{--bg:#f5f6f8;--ink:#ffffff;--panel:#ffffff;--panel2:#eef0f3;--line:#dde1e6;--line2:#c6cbd2;
--text:#171a1f;--muted:#5b6472;--brand:#b45309;--brand-strong:#92400e;--old:#c2410c;--new:#15803d;--red:#dc2626;
--red-bg:#fee2e2;--amber:#b45309;--amber-bg:#fef3c7;--green:#15803d;--glow1-rgb:254,243,226;--glow2-rgb:234,244,255;--term-bg:#1e2126}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--text);line-height:1.45;
background:radial-gradient(1100px 520px at 8% -8%,rgba(var(--glow1-rgb),1) 0%,rgba(var(--glow1-rgb),0) 62%),radial-gradient(900px 480px at 100% 110%,rgba(var(--glow2-rgb),1) 0%,rgba(var(--glow2-rgb),0) 60%),var(--bg);background-attachment:fixed}
a{color:var(--brand);text-decoration:none} a:hover{text-decoration:underline}
main{max-width:1240px;margin:0 auto;padding:26px 20px 60px}
h1{font-size:26px;margin:6px 0 10px;letter-spacing:-.01em}
h1::after{content:"";display:block;width:56px;height:3px;margin-top:8px;border-radius:2px;background:linear-gradient(90deg,var(--brand),var(--old))}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin:0 0 12px}
h2.section{margin:30px 0 12px}
h3{font-size:15px;margin:0 0 6px}
p{margin:0 0 10px}
.lead{color:var(--muted);max-width:860px}
.mono{font-family:var(--mono)} .muted{color:var(--muted)}
.hint{margin:4px 0 0;font-size:12px;color:var(--muted)}
nav.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:4px;padding:0 20px;height:52px;background:var(--ink);border-bottom:1px solid var(--line);box-shadow:0 1px 0 rgba(255,180,84,.18),0 8px 24px rgba(0,0,0,.35)}
nav.top .brand{font-weight:700;color:var(--text);margin-right:18px;white-space:nowrap}
nav.top .brand span{color:var(--brand)} nav.top .brand:hover{text-decoration:none}
nav.top a.nl{color:var(--muted);padding:6px 10px;border-radius:6px;font-size:14px}
nav.top a.nl:hover{color:var(--text);background:var(--panel2);text-decoration:none}
nav.top a.nl.active{color:var(--text);background:var(--panel2)}
nav.top .spacer{flex:1}
.badge{display:inline-block;min-width:18px;padding:0 6px;margin-left:6px;border-radius:9px;background:var(--amber);color:#1b1200;font-size:11px;font-weight:700;text-align:center;line-height:18px}
.pill{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:5px 11px;border-radius:999px;border:1px solid var(--line2);color:var(--muted);white-space:nowrap}
.pill.live{background:var(--red-bg);border-color:var(--red);color:var(--red)}
.pill.live::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--red);margin-right:7px;vertical-align:1px;animation:blink 1.2s infinite}
@keyframes blink{50%{opacity:.25}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:0 0 16px;box-shadow:0 6px 20px rgba(0,0,0,.25)}
.card.danger{border-color:var(--red);background:var(--red-bg)} .card.warn{border-color:var(--amber);background:var(--amber-bg)} .card.teal{border-color:var(--brand)}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));margin-bottom:8px} .grid .card{margin:0}
a.card{display:block;color:var(--text)} a.card:hover{text-decoration:none;border-color:var(--brand)}
a.card h3{color:var(--brand)} a.card p{color:var(--muted);font-size:13px;margin:0}
ol.stepper{list-style:none;margin:0 0 8px;padding:0}
ol.stepper li{position:relative;padding:0 0 14px 40px;min-height:30px}
ol.stepper li::before{content:"";position:absolute;left:12px;top:28px;bottom:-2px;width:2px;background:var(--line)}
ol.stepper li:last-child::before{display:none}
ol.stepper .num{position:absolute;left:0;top:0;width:26px;height:26px;border-radius:50%;background:var(--panel2);border:1px solid var(--line2);color:var(--brand);font-size:12px;font-weight:700;display:flex;align-items:center;justify-content:center}
ol.stepper a{font-weight:600} ol.stepper .note{display:block;font-size:13px;color:var(--muted)}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:middle}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.06em}
tr:last-child td{border-bottom:none} td.mono{font-family:var(--mono);font-size:13px}
.btn,button{font:inherit;font-size:13px;padding:7px 13px;border-radius:7px;border:1px solid var(--line2);background:var(--panel2);color:var(--text);cursor:pointer;display:inline-block}
.btn:hover,button:hover{border-color:var(--brand);text-decoration:none}
.btn.primary,button.primary{background:var(--brand-strong);border-color:var(--brand-strong);color:#1a1200;font-weight:700}
button.danger{background:var(--red);border-color:var(--red);color:#fff;font-weight:700}
button.amber{color:var(--amber);border-color:var(--amber)} button.green{color:var(--green);border-color:var(--green)}
button.sm,.btn.sm{padding:4px 9px;font-size:12px} button:disabled{opacity:.45;cursor:not-allowed}
.btns{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
input[type=text],input[type=password],input[type=number],select,textarea{font:inherit;font-family:var(--mono);font-size:13px;padding:7px 9px;background:var(--ink);color:var(--text);border:1px solid var(--line2);border-radius:7px}
input:focus,select:focus,textarea:focus{outline:2px solid var(--brand);outline-offset:1px}
input[type=checkbox]{accent-color:var(--brand);width:15px;height:15px}
label{font-size:13px;color:var(--muted)}
.field{margin-top:14px} .field label{display:block;margin-bottom:5px} .field label.check{display:flex;gap:8px;align-items:center;color:var(--text)}
.field input[type=text],.field textarea{width:100%}
pre{background:var(--term-bg);color:var(--term-text);padding:14px;border-radius:8px;overflow-x:auto;white-space:pre-wrap;border:1px solid var(--line);font-family:var(--mono);font-size:13px;margin:0 0 10px}
pre.term{white-space:pre;overflow:auto;max-height:70vh}
.chip{display:inline-block;font-size:11px;font-weight:600;letter-spacing:.04em;padding:2px 8px;border-radius:999px;border:1px solid var(--line2);color:var(--muted);font-family:var(--mono);vertical-align:middle;white-space:nowrap}
.chip.amber{color:var(--amber);border-color:var(--amber)} .chip.red{color:var(--red);border-color:var(--red)}
.chip.green{color:var(--green);border-color:var(--green)} .chip.teal{color:var(--brand);border-color:var(--brand)}
.devboard td,.devboard th{vertical-align:middle}
tr.devrow.green>td:first-child{border-left:3px solid var(--green)} tr.devrow.red>td:first-child{border-left:3px solid var(--red)}
tr.devrow.amber>td:first-child{border-left:3px solid var(--amber)}
.banner{border-radius:10px;padding:12px 16px;margin:0 0 18px;border:1px solid var(--line);background:var(--panel)}
.banner.live{border-color:var(--red);background:var(--red-bg)}
.banner form{display:inline-flex;gap:8px;align-items:center;margin-left:8px}
.stattiles{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-bottom:16px}
.stattile{background:var(--panel2);border:1px solid var(--line2);border-radius:8px;padding:14px 12px;text-align:center}
.stattile .k{display:block;font-size:24px;font-weight:700;color:var(--brand);font-family:var(--mono);line-height:1.2}
.stattile .v{display:block;font-size:11px;color:var(--muted);margin-top:5px}
.page-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:4px} .page-head h1{margin:0}
.two{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(360px,1fr))}
@media (max-width:640px){nav.top .brand{margin-right:6px} nav.top a.nl{padding:6px 7px;font-size:13px} main{padding:18px 14px 40px}}
"""

SHELL_HEAD = """<!doctype html>
<html lang="en" data-theme="{{ nav_theme }}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>""" + BASE_CSS + """</style></head>
<body>
<nav class="top">
<a class="brand" href="{{ url_for('index') }}">Device <span>EMS</span> Console</a>
<a class="nl {% if request.path == '/' %}active{% endif %}" href="{{ url_for('index') }}">Console</a>
<a class="nl {% if request.path.startswith('/run') %}active{% endif %}" href="{{ url_for('runs_page') }}">Runs</a>
<a class="nl {% if request.path.startswith('/desired') %}active{% endif %}" href="{{ url_for('desired_page') }}">Desired state</a>
<a class="nl {% if request.path.startswith('/credentials') %}active{% endif %}" href="{{ url_for('credentials_page') }}">Credentials</a>
<a class="nl {% if request.path.startswith('/templates') %}active{% endif %}" href="{{ url_for('templates_page') }}">Templates</a>
<a class="nl {% if request.path.startswith('/ledger') %}active{% endif %}" href="{{ url_for('ledger_page') }}">Ledger</a>
<a class="nl {% if request.path == '/errors' %}active{% endif %}" href="{{ url_for('errors_page') }}">Errors{% if nav_errors %}<span class="badge">{{ nav_errors }}</span>{% endif %}</a>
<a class="nl {% if request.path == '/manual' %}active{% endif %}" href="{{ url_for('manual_page') }}">Manual</a>
<a class="nl {% if request.path == '/settings' %}active{% endif %}" href="{{ url_for('settings_page') }}">Settings</a>
<span class="spacer"></span>
{% if nav_armed %}<span class="pill live">Live since {{ nav_armed_since }}</span>{% else %}<span class="pill">Dry run</span>{% endif %}
</nav>
<main>
"""
SHELL_FOOT = "\n</main></body></html>\n"

ARM_BANNER = """
{% if nav_armed %}
<div class="banner live"><strong style="color:var(--red)">LIVE MODE -- armed at {{ nav_armed_since }}.</strong>
Live buttons will log into real devices (each one still needs its own tick).
<form method="post" action="{{ url_for('live_mode') }}"><input type="hidden" name="back" value="{{ request.path }}">
<button type="submit" class="primary sm">Disarm (back to dry run)</button></form></div>
{% else %}
<div class="banner"><strong style="color:var(--brand)">DRY RUN -- nothing can reach a device.</strong>
Rehearse reads RANCID and writes the files; live buttons are hidden.
<form method="post" action="{{ url_for('live_mode') }}"><input type="hidden" name="back" value="{{ request.path }}">
<input type="text" name="confirm" placeholder="type LIVE to arm" size="18"><button type="submit" class="danger sm">Arm live mode</button></form></div>
{% endif %}
"""

POLL_JS = """
<script>
function esc(s){return String(s==null?'':s);}
async function poll(){
  const r = await fetch("{{ url_for('job_status', job_id=job_id) }}");
  const data = await r.json();
  document.getElementById('log').textContent = data.log;
  if (data.running) { setTimeout(poll, 1500); }
  else { const n = document.getElementById('runnote'); if (n) n.textContent = 'finished -- reload to refresh the board'; {% if reload_when_done %}location.replace("{{ reload_to }}");{% endif %} }
}
poll();
</script>
"""


def _page(title: str, body: str) -> str:
    return SHELL_HEAD.replace("__TITLE__", title) + body + SHELL_FOOT


@app.context_processor
def _nav_context():
    return {"nav_armed": _armed(), "nav_armed_since": LIVE_MODE["since"], "nav_errors": errorlog.count(SCRIPT_DIR),
            "nav_theme": _settings().get("theme", "dark")}


# --------------------------------------------------------------------------- pages
INDEX_HTML = _page("Device EMS Console", """
<h1>Device EMS Console</h1>
<p class="lead">TACACS+ to ISE, login classes and users, accounting, NTP, syslog and SNMP -- one device or the whole fleet,
with a self-reverting commit and a second login that proves the new AAA works before anything is made permanent.</p>
""" + ARM_BANNER + """
<div class="stattiles">
{% for s, n in counts.items() %}<div class="stattile"><span class="k">{{ n }}</span><span class="v">{{ s.replace('_',' ') }}</span></div>{% endfor %}
{% if not counts %}<div class="stattile"><span class="k">0</span><span class="v">devices in the ledger</span></div>{% endif %}
</div>
<div class="two">
<section class="card">
<h2>How a run goes</h2>
<ol class="stepper">
<li><span class="num">1</span><a href="{{ url_for('desired_page') }}">Desired state</a><span class="note">The standard every box is brought to.
{% if ds.placeholders %}<span style="color:var(--red)">{{ ds.placeholders|length }} placeholder(s) still to fill.</span>{% elif ds.exists %}<span style="color:var(--green)">ready</span>{% else %}<span style="color:var(--red)">missing</span>{% endif %}</span></li>
<li><span class="num">2</span><a href="{{ url_for('credentials_page') }}">Credentials</a><span class="note">jlogin's own account first, then the static local users, in order. {{ health.message }}</span></li>
<li><span class="num">3</span><a href="{{ url_for('new_run') }}">New run</a><span class="note">Paste the devices (one per line) or upload a list; name it after the CM.</span></li>
<li><span class="num">4</span>Rehearse<span class="note">From RANCID: fix file + exact rollback per device, nothing contacted. Read the diffs.</span></li>
<li><span class="num">5</span>Run the ISE fix<span class="note">Arm (type LIVE), tick, go: discover -> build -> <span class="mono">commit confirmed</span> -> second login + verify -> commit. Resumable.</span></li>
<li><span class="num">6</span>Recheck + MOP<span class="note">Boxes whose second login failed revert themselves; Recheck proves it. MOP and CSV from the run page.</span></li>
</ol>
</section>
<section class="card">
<h2>Recent runs</h2>
{% if recent %}<table><tr><th>run</th><th>devices</th><th>states</th><th>updated</th></tr>
{% for r in recent %}<tr><td><a href="{{ url_for('run_page', run_id=r.run_id) }}">{{ r.run_id }}</a>{% if r.meta.cm_number %} <span class="muted">{{ r.meta.cm_number }}</span>{% endif %}</td>
<td>{{ r.devices }}</td><td class="mono" style="font-size:12px">{{ r.counts.items()|map('join', ' ')|join(', ') }}</td><td class="muted">{{ r.updated }}</td></tr>{% endfor %}</table>
{% else %}<p class="muted">No runs yet. <a href="{{ url_for('new_run') }}">Create one.</a></p>{% endif %}
<p style="margin-top:12px"><a class="btn primary" href="{{ url_for('new_run') }}">New run</a> <a class="btn" href="{{ url_for('runs_page') }}">All runs</a></p>
</section>
</div>
""")

RUNS_HTML = _page("Runs", """
<div class="page-head"><h1>Runs</h1><a class="btn primary" href="{{ url_for('new_run') }}">New run</a></div>
<section class="card">
{% if all_runs %}<table><tr><th>run</th><th>CM</th><th>devices</th><th>states</th><th>updated</th></tr>
{% for r in all_runs %}<tr><td><a href="{{ url_for('run_page', run_id=r.run_id) }}">{{ r.run_id }}</a></td><td>{{ r.meta.cm_number or '-' }}</td>
<td>{{ r.devices }}</td><td class="mono" style="font-size:12px">{% for s, n in r.counts.items() %}{{ n }} {{ s }}{{ ', ' if not loop.last }}{% endfor %}</td><td class="muted">{{ r.updated }}</td></tr>{% endfor %}</table>
{% else %}<p class="muted">No runs yet.</p>{% endif %}
</section>
""")

NEW_RUN_HTML = _page("New run", """
<h1>New run</h1>
<p class="lead">One device per line -- hostname or management address. Add <span class="mono">,ex</span> / <span class="mono">,srx</span> after a name only when the platform can't be read from the box (offline RANCID builds).</p>
{% if error %}<div class="card danger">{{ error }}</div>{% endif %}
<section class="card"><form method="post" enctype="multipart/form-data">
<div class="field"><label>Run name (the CM number is a good one)</label><input type="text" name="run" value="{{ values.run }}" required></div>
<div class="field"><label>CM / ticket</label><input type="text" name="cm" value="{{ values.cm }}"></div>
<div class="field"><label>Devices</label><textarea name="targets" rows="12" placeholder="example-pe01&#10;example-pe02&#10;192.0.2.10,ex">{{ values.targets }}</textarea></div>
<div class="field"><label>...or upload a list (txt/csv, one device per line)</label><input type="file" name="file"></div>
<div class="field"><label>Note</label><input type="text" name="note" value="{{ values.note }}"></div>
<p style="margin-top:14px"><button class="primary" type="submit">Create run</button></p>
</form></section>
""")

RUN_HTML = _page("Run", """
<div class="page-head"><h1>{{ run_id }}</h1>{% if meta.cm_number %}<span class="chip teal">{{ meta.cm_number }}</span>{% endif %}
<span class="muted">{{ devices|length }} device(s)</span></div>
""" + ARM_BANNER + """
{% if error %}<div class="card danger">{{ error }}</div>{% endif %}
{% if pending %}
<div class="card danger"><h3>Run the ISE fix on {{ pending.count }} device(s) for real?</h3>
<p>The log below is the plan (ladders, timers, devices). This opens jlogin sessions and commits <span class="mono">commit confirmed {{ ds.commit.confirmed_minutes }}</span> on each box, then logs in again through the new AAA to confirm.</p>
<form method="post" class="btns">
<input type="hidden" name="action" value="fix"><input type="hidden" name="yes" value="1">
<input type="hidden" name="selected" value="{{ pending.selected }}"><input type="hidden" name="workers" value="{{ pending.workers }}">
{% if pending.redo %}<input type="hidden" name="redo" value="1">{% endif %}
<label style="color:var(--red)"><input type="checkbox" name="live" value="1"> log into these devices for real</label>
<button class="danger" type="submit">Run it</button></form></div>
{% endif %}
<div class="stattiles">
{% for s, n in counts.items() %}<div class="stattile"><span class="k">{{ n }}</span><span class="v">{{ s.replace('_',' ') }}</span></div>{% endfor %}
</div>
<section class="card">
<h2>Device board</h2>
<form method="post" id="actform">
<div class="btns" style="margin-bottom:10px">
<button type="button" class="sm" id="sel-all">All</button><button type="button" class="sm" id="sel-none">None</button>
<button type="button" class="sm" id="sel-notdone">Select not done</button><button type="button" class="sm" id="sel-failed">Select failed</button>
<button type="button" class="sm" id="sel-rb">Select rollback pending</button>
<span class="muted" id="selcount">0 selected</span>
</div>
<table class="devboard">
<tr><th></th><th>device</th><th>platform</th><th>state</th><th>detail</th><th>files</th></tr>
{% for d in board %}
<tr class="devrow {{ d.chip }}" data-state="{{ d.state }}">
<td><input type="checkbox" class="dev" name="devices" value="{{ d.device }}"></td>
<td class="mono">{{ d.device }}</td><td class="muted">{{ d.platform }} {{ d.model }}</td>
<td><span class="chip {{ d.chip }}">{{ d.state.replace('_',' ') }}</span></td>
<td style="font-size:13px">{{ d.detail }}</td>
<td style="font-size:12px;white-space:nowrap">
{% if d.fix %}<a href="{{ url_for('device_file', run_id=run_id, device=d.device, name=d.fix) }}">fix file</a>{% endif %}
{% if d.log %} <a href="{{ url_for('device_log', run_id=run_id, device=d.device, name=d.log) }}">last log</a>{% endif %}
</td></tr>
{% endfor %}
</table>
<h2 class="section">Actions on the selected devices</h2>
<div class="btns">
<label>devices at a time <input type="number" name="workers" value="{{ workers }}" min="1" max="10" style="width:60px"></label>
<label><input type="checkbox" name="redo" value="1"> redo (repeat confirmed/compliant)</label>
<label>RANCID folder <input type="text" name="rancid" value="{{ rancid }}" size="34" placeholder="for Rehearse"></label>
</div>
<div class="btns" style="margin-top:10px">
<button type="submit" name="action" value="rehearse" class="primary">Rehearse (RANCID, no device)</button>
{% if nav_armed %}
<button type="submit" name="action" value="discover" class="amber" onclick="return confirm('Open read-only discovery sessions on the selected devices?')">Discover (live, read-only)</button>
<button type="submit" name="action" value="build" class="amber" onclick="return confirm('Discover live and build the fix files? Nothing is committed.')">Build (live, read-only)</button>
<button type="submit" name="action" value="fix" class="danger">Run ISE fix (live)</button>
<button type="submit" name="action" value="recheck" class="green" onclick="return confirm('Log into the rollback-pending devices and verify they reverted?')">Recheck rollbacks (live)</button>
{% endif %}
<button type="submit" name="action" value="mop">Write MOP</button>
<a class="btn" href="{{ url_for('run_csv', run_id=run_id) }}">CSV</a>
</div>
{% if not nav_armed %}<p class="hint">Live buttons appear once the console is armed.</p>{% endif %}
</form>
</section>
{% if job_id %}
<section class="card"><h2>Output <span class="muted" id="runnote">running...</span></h2><pre id="log" class="term">(starting...)</pre></section>
""" + POLL_JS + """
{% endif %}
{% if mop_files %}<section class="card"><h2>MOP</h2>{% for f in mop_files %}<a href="{{ url_for('run_file', run_id=run_id, name=f) }}">{{ f }}</a> {% endfor %}</section>{% endif %}
<script>
const devs = Array.from(document.querySelectorAll('input.dev'));
function sync(){ const n = devs.filter(d=>d.checked).length; document.getElementById('selcount').textContent = n + ' selected'; }
function pick(keep){ devs.forEach(d => { d.checked = keep(d.closest('tr').dataset.state); }); sync(); }
document.getElementById('sel-all').onclick = () => pick(() => true);
document.getElementById('sel-none').onclick = () => pick(() => false);
document.getElementById('sel-notdone').onclick = () => pick(s => !['confirmed','compliant'].includes(s));
document.getElementById('sel-failed').onclick = () => pick(s => ['failed','unsupported','rollback_unknown','rehearsal_failed','rolled_back'].includes(s));
document.getElementById('sel-rb').onclick = () => pick(s => ['rollback_pending','rollback_unknown'].includes(s));
devs.forEach(d => d.addEventListener('change', sync)); sync();
</script>
""")

LOG_HTML = _page("Log", """
<div class="page-head"><h1>{{ name }}</h1><span class="muted">{{ device }} / {{ run_id }}</span></div>
<p><a href="{{ url_for('run_page', run_id=run_id) }}">&larr; back to the run</a> &nbsp; <a href="{{ url_for('device_file', run_id=run_id, device=device, name=name) }}">download</a></p>
<pre class="term">{{ text }}</pre>
""")

DESIRED_HTML = _page("Desired state", """
<h1>Desired state</h1>
<p class="lead">The standard every device is brought to -- <span class="mono">config/desired_state.json</span>. Top-level sections apply to every platform; <span class="mono">platforms.ex</span> / <span class="mono">platforms.srx</span> override them (a mapping like <span class="mono">classes</span> or <span class="mono">users</span> is replaced, scalars and lists replace, other objects merge).</p>
{% if saved %}<div class="card teal">Saved.</div>{% endif %}
{% if error %}<div class="card danger">{{ error }}</div>{% endif %}
{% if ds.placeholders %}<div class="card warn"><strong>{{ ds.placeholders|length }} placeholder(s) to fill before any build:</strong> {{ ds.placeholders|join('; ') }}</div>{% endif %}
{% if ds.platforms %}<section class="card"><h2>What each platform gets</h2><table><tr><th>platform</th><th>tacacs</th><th>classes</th><th>users</th><th>ntp</th><th>syslog</th><th>communities</th></tr>
{% for p, v in ds.platforms.items() %}<tr><td class="mono">{{ p }}</td><td class="mono">{{ v.tacacs|join(', ') }}</td><td>{{ v.classes }}</td><td>{{ v.users }}</td><td class="mono">{{ v.ntp|join(', ') }}</td><td class="mono">{{ v.syslog|join(', ') }}</td><td>{{ v.communities }}</td></tr>{% endfor %}</table>
<p class="hint">commit confirmed {{ ds.commit.confirmed_minutes }} min, comment {{ ds.commit.comment }}, confirm after {{ ds.commit.confirm_delay_seconds }}s, session timeout {{ ds.commit.timeout_seconds }}s</p></section>{% endif %}
<section class="card"><form method="post">
<div class="field"><label>{{ ds.path }}</label><textarea name="text" rows="34" spellcheck="false">{{ text }}</textarea></div>
<p style="margin-top:12px" class="btns"><button class="primary" type="submit" name="do" value="save">Validate and save</button>
<button type="submit" name="do" value="validate">Validate only</button>
{% if not ds.exists %}<button type="submit" name="do" value="example">Start from the example</button>{% endif %}</p>
</form></section>
""")

CREDENTIALS_HTML = _page("Credentials", """
<h1>Credentials</h1>
<p class="lead">Two ladders. <strong>Discover / deploy</strong>: jlogin's own account (<span class="mono">~/.cloginrc</span>) first, then static local users for boxes whose TACACS is already dead. <strong>Confirm</strong>: the second login that proves the new AAA works -- by default only the jlogin account, so a box the ISE login can't reach reverts itself instead of being confirmed through a local account.</p>
<div class="card {% if health.ok %}teal{% else %}warn{% endif %}"><strong>jlogin:</strong> {{ health.message }}</div>
{% if cs.error %}<div class="card danger">{{ cs.error }}</div>{% endif %}
{% if saved %}<div class="card teal">Saved {{ cs.path }}.</div>{% endif %}
<div class="two">
<section class="card"><h2>Discover / deploy ladder</h2><ol>{% for c in cs.discover %}<li class="mono">{{ c.label }} <span class="muted">({{ c.username }})</span></li>{% endfor %}</ol></section>
<section class="card"><h2>Confirm ladder</h2><ol>{% for c in cs.confirm %}<li class="mono">{{ c.label }} <span class="muted">({{ c.username }})</span></li>{% endfor %}</ol>
<p class="hint">Add a local user here only if you accept that a box can be confirmed without the ISE login working.</p></section>
</div>
<section class="card"><h2>Edit {{ cs.path }}</h2>
<form method="post">
<div class="field"><label>Local users -- one per line: <span class="mono">label,username,password</span> (existing passwords are kept when the password column is left blank)</label>
<textarea name="users" rows="6" spellcheck="false">{{ users_text }}</textarea></div>
<div class="field"><label>Discover / deploy order (labels, comma-separated; <span class="mono">jlogin-default</span> is the .cloginrc account)</label><input type="text" name="discover_order" value="{{ discover_order }}"></div>
<div class="field"><label>Confirm order</label><input type="text" name="confirm_order" value="{{ confirm_order }}"></div>
<p style="margin-top:12px"><button class="primary" type="submit">Save (file is written 0600)</button></p>
</form></section>
""")

TEMPLATES_HTML = _page("Templates", """
<h1>Templates</h1>
<p class="lead">One file per platform under <span class="mono">templates/</span>. Written as exact-state (delete the object, set it in full); the builder then drops every object the box already has, so only real changes are sent.</p>
{% for t in items %}<section class="card"><h3 class="mono">{{ t.path }} <span class="chip teal">{{ t.platforms|join(', ') or 'unused' }}</span></h3><pre>{{ t.text }}</pre></section>{% endfor %}
""")

LEDGER_HTML = _page("Ledger", """
<div class="page-head"><h1>Fleet ledger</h1><a class="btn" href="{{ url_for('ledger_csv') }}">CSV</a></div>
<div class="stattiles">{% for s, n in counts.items() %}<div class="stattile"><span class="k">{{ n }}</span><span class="v">{{ s.replace('_',' ') }}</span></div>{% endfor %}</div>
<section class="card"><form method="get" class="btns"><label>state <input type="text" name="state" value="{{ state }}" size="16"></label><label>run <input type="text" name="run" value="{{ run }}" size="16"></label><button type="submit" class="sm">Filter</button></form>
<table><tr><th>device</th><th>platform</th><th>state</th><th>run</th><th>credential</th><th>updated</th><th>detail</th></tr>
{% for r in rows %}<tr><td class="mono">{{ r.device }}</td><td>{{ r.platform or '?' }} {{ r.model or '' }}</td><td><span class="chip {{ chip(r.state) }}">{{ r.state.replace('_',' ') }}</span></td>
<td><a href="{{ url_for('run_page', run_id=r.run_id) }}">{{ r.run_id }}</a></td><td class="mono">{{ r.credential or '' }}</td><td class="muted">{{ r.updated_at }}</td><td style="font-size:13px">{{ r.detail }}</td></tr>{% endfor %}</table></section>
""")

ERRORS_HTML = _page("Errors", """
<h1>Errors</h1><p class="lead">Newest first, from <span class="mono">logs/errors.log</span>.</p>
{% for e in entries %}<pre class="err">{{ e }}</pre>{% else %}<p class="muted">Nothing recorded.</p>{% endfor %}
""")

SETTINGS_HTML = _page("Settings", """
<h1>Settings</h1>
{% if saved %}<div class="card teal">Saved.</div>{% endif %}
<section class="card"><form method="post">
<div class="field"><label>Theme</label><select name="theme">{% for t in themes %}<option value="{{ t }}" {% if t == s.theme %}selected{% endif %}>{{ t }}</option>{% endfor %}</select></div>
<div class="field"><label>Default RANCID folder (for Rehearse)</label><input type="text" name="rancid_folder" value="{{ s.rancid_folder }}"></div>
<div class="field"><label>Default devices at a time</label><input type="number" name="workers" value="{{ s.workers }}" min="1" max="10"></div>
<p style="margin-top:12px"><button class="primary" type="submit">Save</button></p></form></section>
<section class="card"><h2>This server</h2><p class="mono" style="font-size:13px">started {{ started }} &middot; python {{ py }} &middot; runs in {{ runs_root }}</p></section>
""")

MANUAL_HTML = _page("Manual", """
<h1>Manual</h1>
<section class="card"><h2>What one device goes through</h2>
<ol class="stepper">
<li><span class="num">1</span>Discover<span class="note">Read-only jlogin: show version + display-set of system / groups / snmp / lo0 / fxp0. Credential ladder: jlogin default, then static local users.</span></li>
<li><span class="num">2</span>Build<span class="note">Platform template rendered, then minimised against what the box has. Fix file = header, config, verify commands, exact restore-rollback. Zero lines = COMPLIANT, done.</span></li>
<li><span class="num">3</span>Deploy<span class="note">configure exclusive / lines / show | compare / <span class="mono">commit confirmed N comment ISE_FIX_Script_PENDING</span>. Same credential that discovered the box.</span></li>
<li><span class="num">4</span>Confirm<span class="note">A NEW login through the confirm ladder (the ISE account). Verify shows, then <span class="mono">commit comment ISE_FIX_Script and-quit</span>. Login or verification failing = nothing committed; the box reverts at +N minutes.</span></li>
<li><span class="num">5</span>Recheck<span class="note">After the timer, for the failures only: is the old config back? ROLLED BACK (fix by hand) / still applied (needs a human).</span></li>
</ol></section>
<section class="card"><h2>Safety</h2>
<p>Nothing reaches a device unless asked twice: the console starts disarmed, arming is typing <span class="mono">LIVE</span>, a restart disarms, every live action has its own tick, and the ISE fix shows its plan first and runs only on the second click. jlogin's exit code is never trusted -- every session gets a verdict read from the transcript (ok / timeout / dns / unreachable / auth / no-session / rejected / failed). Dry runs never overwrite live records. Protected users (root, the remote* template users, anything on the ladder) are never deleted.</p></section>
<section class="card"><h2>Resuming</h2>
<p>Every step of every device is recorded in <span class="mono">runs/&lt;run&gt;/status.json</span> as it finishes. Running the fix again skips devices already confirmed or compliant; tick <em>redo</em> to repeat them. Ctrl-C (or stopping the server) never starts a queued device; an open session is recorded as "outcome unknown".</p></section>
<section class="card"><h2>Command line</h2>
<pre>cli.py check
cli.py new CM12345 --file targets.txt --cm CM12345
cli.py rehearse CM12345 --rancid-folder /mnt/.../configs
cli.py fix CM12345 --live --yes            # resumable; --devices a,b  --redo  --workers 8
cli.py recheck CM12345 --live --yes --wait
cli.py status CM12345 [--csv out.csv]
cli.py mop CM12345
cli.py preview --config-file dump.txt      # the fix file one dump would get
cli.py ledger [--state failed] [--export fleet.csv]</pre></section>
""")


# --------------------------------------------------------------------------- routes
@app.route("/")
def index():
    conn = ledger.connect(cli.LEDGER_PATH)
    return render_template_string(INDEX_HTML, counts=ledger.state_counts(conn), ds=_desired_summary(),
                                  health=session.check_jlogin_health(), recent=runs.list_runs(cli.RUNS_ROOT)[:8])


@app.route("/runs")
def runs_page():
    return render_template_string(RUNS_HTML, all_runs=runs.list_runs(cli.RUNS_ROOT))


@app.route("/runs/new", methods=["GET", "POST"])
def new_run():
    values = {"run": "", "cm": "", "targets": "", "note": ""}
    error = ""
    if request.method == "POST":
        values = {k: request.form.get(k, "").strip() for k in values}
        text = values["targets"]
        upload = request.files.get("file")
        if upload and upload.filename:
            text = upload.read().decode("utf-8", "replace")
        try:
            devices, hints = runs.parse_targets(text)
            run_id = runs.safe_run_id(values["run"])
            runs.create_run(cli.RUNS_ROOT, run_id, devices, hints, cm_number=values["cm"], note=values["note"])
            ledger.record_run(ledger.connect(cli.LEDGER_PATH), run_id, datetime.now().isoformat(timespec="seconds"),
                              values["cm"], values["note"])
            return redirect(url_for("run_page", run_id=run_id))
        except runs.RunError as exc:
            error = str(exc)
    return render_template_string(NEW_RUN_HTML, values=values, error=error)


def _run_argv(action: str, run_id: str, selected: list[str], form, live: bool, rancid: str) -> list[str]:
    argv = [action if action != "fix" else "fix", run_id]
    if selected:
        argv += ["--devices", ",".join(selected)]
    workers = form.get("workers", "").strip()
    if workers.isdigit():
        argv += ["--workers", workers]
    if form.get("redo"):
        argv.append("--redo")
    if action == "rehearse":
        argv += ["--rancid-folder", rancid]
    elif live:
        argv.append("--live")
    return argv


@app.route("/run/<run_id>", methods=["GET", "POST"])
def run_page(run_id):
    loaded = _resolve_run(run_id)
    if not loaded:
        return Response("no such run", status=404)
    run_dir, meta, devices, _ = loaded
    settings = _settings()
    error, job_id, pending = "", request.args.get("job", ""), None
    if request.method == "POST":
        action = request.form.get("action", "")
        selected = [d for d in request.form.getlist("devices") if d in devices]
        if request.form.get("selected"):
            selected = [d for d in request.form["selected"].split(",") if d in devices]
        rancid = request.form.get("rancid", "").strip() or settings.get("rancid_folder", "")
        live_actions = ("discover", "build", "fix", "recheck")
        if action == "mop":
            job_id = launch("mop", ["mop", run_dir.name], run_dir.name)
        elif action not in live_actions + ("rehearse",):
            error = "unknown action"
        elif _active_job_for(run_dir.name):
            error = "a job is already running on this run -- wait for it to finish"
        elif not selected and action != "recheck":
            error = "select at least one device"
        elif action == "rehearse" and not rancid:
            error = "Rehearse needs a RANCID folder (field above, or Settings)"
        elif action in live_actions and not _armed():
            error = "the console is not armed -- type LIVE above"
        elif action == "fix" and not request.form.get("yes"):
            argv = _run_argv("fix", run_dir.name, selected, request.form, True, rancid)
            job_id = launch("fix-preview", argv, run_dir.name)
            pending = {"count": len(selected), "selected": ",".join(selected),
                       "workers": request.form.get("workers", ""), "redo": bool(request.form.get("redo"))}
        elif action == "fix":
            if not request.form.get("live"):
                error = "tick 'log into these devices for real' to run it -- nothing was sent"
            else:
                argv = _run_argv("fix", run_dir.name, selected, request.form, True, rancid) + ["--yes"]
                job_id = launch("fix", argv, run_dir.name)
        else:
            argv = _run_argv(action, run_dir.name, selected, request.form, action != "rehearse", rancid)
            if action in live_actions:
                argv.append("--yes")
            job_id = launch(action, argv, run_dir.name)
        if job_id and not pending and not error:
            return redirect(url_for("run_page", run_id=run_dir.name, job=job_id))
    board = _board(run_dir, devices)
    counts: dict[str, int] = {}
    for row in board:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    mop_files = sorted(p.name for p in run_dir.glob("MOP_*"))
    return render_template_string(RUN_HTML, run_id=run_dir.name, meta=meta, devices=devices, board=board,
                                  counts=counts, error=error, job_id=job_id, pending=pending, ds=_desired_summary(),
                                  workers=settings.get("workers", 8), rancid=settings.get("rancid_folder", ""),
                                  mop_files=mop_files, reload_when_done=bool(job_id and not pending),
                                  reload_to=url_for("run_page", run_id=run_dir.name))


@app.route("/run/<run_id>/csv")
def run_csv(run_id):
    loaded = _resolve_run(run_id)
    if not loaded:
        return Response("no such run", status=404)
    run_dir, _, devices, _ = loaded
    path = run_dir / f"board_{run_dir.name}.csv"
    runs.export_csv(run_dir, devices, path)
    return send_file(path, as_attachment=True)


@app.route("/run/<run_id>/file/<name>")
def run_file(run_id, name):
    loaded = _resolve_run(run_id)
    if not loaded:
        return Response("no such run", status=404)
    try:
        candidate = (loaded[0] / Path(name).name).resolve()
        candidate.relative_to(cli.RUNS_ROOT.resolve())
    except (OSError, ValueError):
        return Response("not found", status=404)
    if not candidate.is_file():
        return Response("not found", status=404)
    return send_file(candidate, as_attachment=True)


@app.route("/run/<run_id>/device/<device>/file/<path:name>")
def device_file(run_id, device, name):
    loaded = _resolve_run(run_id)
    path = _device_file(loaded[0], device, name) if loaded else None
    if not path:
        return Response("not found", status=404)
    return send_file(path, as_attachment=True)


@app.route("/run/<run_id>/device/<device>/log/<path:name>")
def device_log(run_id, device, name):
    loaded = _resolve_run(run_id)
    path = _device_file(loaded[0], device, name) if loaded else None
    if not path:
        return Response("not found", status=404)
    return render_template_string(LOG_HTML, run_id=run_id, device=device, name=path.name,
                                  text=path.read_text(encoding="utf-8", errors="replace"))


@app.route("/job/<job_id>/status")
def job_status(job_id):
    log, running = _job_log(job_id)
    return jsonify({"log": log, "running": running})


@app.route("/desired", methods=["GET", "POST"])
def desired_page():
    path = cli.CONFIG_DIR / desired.DESIRED_FILE
    example = cli.CONFIG_DIR / "desired_state.example.json"
    error, saved = "", False
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if request.method == "POST":
        do = request.form.get("do", "validate")
        if do == "example":
            text = example.read_text(encoding="utf-8") if example.exists() else "{}"
        else:
            text = request.form.get("text", "")
            state, error = desired.validate_text(text)
            if state and do == "save":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                saved = True
    return render_template_string(DESIRED_HTML, ds=_desired_summary(), text=text, error=error, saved=saved)


@app.route("/credentials", methods=["GET", "POST"])
def credentials_page():
    path = cli.CONFIG_DIR / credentials.CREDENTIALS_FILE
    saved = False
    current = {}
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        current = {}
    if request.method == "POST":
        existing = {u.get("label"): u for u in current.get("local_users", []) if isinstance(u, dict)}
        users = []
        for raw in request.form.get("users", "").splitlines():
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) < 2 or not parts[0]:
                continue
            label, username = parts[0], parts[1]
            password = parts[2] if len(parts) > 2 and parts[2] else (existing.get(label) or {}).get("password", "")
            users.append({"label": label, "username": username, "password": password})
        data = {"jlogin_default": True, "local_users": users,
                "discover_order": [x.strip() for x in request.form.get("discover_order", "").split(",") if x.strip()],
                "confirm_order": [x.strip() for x in request.form.get("confirm_order", "").split(",") if x.strip()]}
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        current, saved = data, True
    users_text = "\n".join(f"{u.get('label')},{u.get('username')}," for u in current.get("local_users", [])
                           if isinstance(u, dict))
    return render_template_string(CREDENTIALS_HTML, cs=_creds_summary(), health=session.check_jlogin_health(),
                                  saved=saved, users_text=users_text,
                                  discover_order=", ".join(current.get("discover_order") or ["jlogin-default"]),
                                  confirm_order=", ".join(current.get("confirm_order") or ["jlogin-default"]))


@app.route("/templates")
def templates_page():
    return render_template_string(TEMPLATES_HTML, items=tpl.list_templates())


@app.route("/ledger")
def ledger_page():
    conn = ledger.connect(cli.LEDGER_PATH)
    state, run = request.args.get("state", ""), request.args.get("run", "")
    rows = [dict(r) for r in ledger.list_devices(conn, state=state, run_id=run)]
    return render_template_string(LEDGER_HTML, rows=rows, counts=ledger.state_counts(conn), state=state, run=run,
                                  chip=_chip_class)


@app.route("/ledger/export.csv")
def ledger_csv():
    conn = ledger.connect(cli.LEDGER_PATH)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["device", "platform", "model", "version", "state", "detail", "run_id", "credential", "updated_at"])
    for r in ledger.list_devices(conn):
        w.writerow([r["device"], r["platform"], r["model"], r["version"], r["state"], r["detail"], r["run_id"],
                    r["credential"], r["updated_at"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=fleet_ledger.csv"})


@app.route("/errors")
def errors_page():
    return render_template_string(ERRORS_HTML, entries=errorlog.entries(SCRIPT_DIR, limit=40))


@app.route("/manual")
def manual_page():
    return render_template_string(MANUAL_HTML)


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    s = _settings()
    saved = False
    if request.method == "POST":
        theme = request.form.get("theme", "dark")
        s["theme"] = theme if theme in THEMES else "dark"
        s["rancid_folder"] = request.form.get("rancid_folder", "").strip()
        workers = request.form.get("workers", "8").strip()
        s["workers"] = max(1, min(int(workers), 10)) if workers.isdigit() else 8
        _save_settings(s)
        saved = True
    return render_template_string(SETTINGS_HTML, s=s, themes=THEMES, saved=saved,
                                  started=STARTED.strftime("%Y-%m-%d %H:%M"), py=sys.version.split()[0],
                                  runs_root=str(cli.RUNS_ROOT))


@app.route("/live-mode", methods=["POST"])
def live_mode():
    want_live = request.form.get("confirm", "").strip().upper() == "LIVE"
    LIVE_MODE["armed"] = want_live
    LIVE_MODE["since"] = datetime.now().strftime("%H:%M") if want_live else None
    back = request.form.get("back") or url_for("index")
    if not back.startswith("/"):
        back = url_for("index")
    return redirect(back)


@app.errorhandler(Exception)
def _record_unhandled(exc):
    if isinstance(exc, HTTPException):   # a plain 404/403/405 is not an error worth logging
        return exc
    errorlog.record(SCRIPT_DIR, f"webapp {request.method} {request.path}", exc)
    return Response(f"Internal error: {type(exc).__name__}: {exc} -- recorded in /errors", status=500)


def find_available_port(start: int) -> int:
    import socket
    for port in range(start, start + 10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


if __name__ == "__main__":
    port = int(os.environ.get("EMS_CONSOLE_PORT", find_available_port(DEFAULT_PORT)))
    print(f"Device EMS Console on http://127.0.0.1:{port}  (DRY RUN until LIVE is typed)")
    app.run(host="127.0.0.1", port=port, debug=False)
