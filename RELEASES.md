# Releases

## 2026-10-03 - Console only; shipped defaults

- Removed the legacy Streamlit app from the repository (copy kept outside git); `requirements.txt` is now the console's.
- `config/desired_state.default.json` ships the fleet standard and is copied to `desired_state.json` on first start; `cli.py check` prints exactly what is left to fill.
- `run_dashboard.bat` builds the venv, starts the server and opens the browser only once it answers (the old launcher opened the browser first and looked dead).
- `deploy/device-ems-console.service` systemd unit for the console.

## 2026-10-02 - Device EMS Console (rebuild)

- New jlogin-driven engine (`core/`), CLI (`cli.py`) and Flask console (`webapp.py`, :5460) alongside the untouched legacy Streamlit app.
- Per-device sequence: discover -> build -> `commit confirmed N` -> second login through the new AAA with structural verification -> commit; failures revert themselves and are rechecked after the timer.
- Credential ladder (jlogin default, then static local users via throw-away cloginrc), transcript verdicts, parallel resumable runs, per-device status records, fleet ledger, MOP + CSV.
- One exact-state template per platform (MX+ACX, EX, SRX-unvalidated) minimised against the live config; exact restore-style rollback in every fix file.
- Local simulator (`captures/fake_jlogin.py`) and a pytest suite covering parser, builder, verifier, session, bulk runner, CLI and console.
- Docs: README (old-vs-new comparison), docs/DESIGN.md, docs/RUN_FLOW.html, docs/HANDOFF.md, docs/LEGACY_README.md.

## 2026-07-01 - Selectable EMS Jinja Templates And Headless CLI

- Added auto-discovered Jinja change templates under `templates/junos/change_templates`.
- Added UI selection for which templates render during Phase 2 build.
- Added per-device-type template matching through template metadata.
- Added a headless CLI and `run_cli.sh` launcher for credentials, targets, desired state, discovery, build, deploy, audit, status, and reporting.
- Split the standard Junos baseline into separate TACACS/login-user, SNMP, NTP, syslog, and NETCONF/LLDP templates.
- Added explicit legacy login-user cleanup fields and discovery/reporting of existing login users.
- Added optional SNMP sysDescr discovery fallback for model/version identification.
- Added per-device fix-file generation under `data/fix_files/<change>/`.
- Added a Logs tab for run-level and per-device console output.
- Added an explicit unsupported-platform build status; PTX config generation is blocked pending real-config validation.
- Moved the old platform wrapper templates under `templates/junos/_archive` as inactive migration references.

## Validation

- Run `python -m pytest` from the project virtual environment.
- Run `python -m compileall app.py device_ems_telemetary_update`.

## 2026-05-18 - Portable server launch cleanup

- Added Linux `run.sh` and `run_server.sh` launchers that create and reuse a project-local `.venv`.
- Updated the Windows dashboard launcher to use `.venv` instead of `venv`.
- Added Streamlit dark theme settings to match the companion network tools.
- Updated visible app/report text from "Telemetary" to "Telemetry"; repository and package names are unchanged for compatibility.
- Added pytest configuration in `pyproject.toml`.

## Validation

- Run `python -m pytest` from the project virtual environment.
- Run `python -m compileall app.py device_ems_telemetary_update`.
