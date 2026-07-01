# Releases

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
