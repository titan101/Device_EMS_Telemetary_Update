# Releases

## 2026-05-18 - Portable server launch cleanup

- Added Linux `run.sh` and `run_server.sh` launchers that create and reuse a project-local `.venv`.
- Updated the Windows dashboard launcher to use `.venv` instead of `venv`.
- Added Streamlit dark theme settings to match the companion network tools.
- Updated visible app/report text from "Telemetary" to "Telemetry"; repository and package names are unchanged for compatibility.
- Added pytest configuration in `pyproject.toml`.

## Validation

- Run `python -m pytest` from the project virtual environment.
- Run `python -m compileall app.py device_ems_telemetary_update`.
