# Device EMS Telemetary Update

Dashboard and automation framework for Juniper EMS and telemetry changes across EX, MX, and PTX devices. The first release focuses on SNMP communities, TACACS servers, RADIUS cleanup, syslog hosts, NETCONF, LLDP, and NTP servers.

## Run It

```bat
cd Device_EMS_Telemetary_Update
run_dashboard.bat
```

The dashboard starts on `http://localhost:8502`.

## Screenshots

![Dashboard overview](docs/screenshots/01-dashboard-overview.png)

![Target intake](docs/screenshots/02-target-intake.png)

![Desired state](docs/screenshots/03-desired-state.png)

![Deploy safety controls](docs/screenshots/04-deploy-safety.png)

## Workflow

1. Credentials: unlock or create an encrypted local credential vault. Profiles can be `primary`, `secondary`, or `audit`.
2. Targets: enter a change number and paste device names or IPs.
3. Desired State: enter TACACS, NTP, syslog, SNMP, NETCONF, LLDP, cleanup options, commit-confirm minutes, audit wait time, and worker count.
4. Phase 1 Discovery: ping first, then log in with the stored credential profiles. PyEZ is tried first; Netmiko is the SSH CLI fallback for devices that do not yet have NETCONF enabled.
5. Phase 2 Build: generate per-device Junos `set` and `delete` configuration from the device model, version, and discovered existing config.
6. Phase 3 Deploy: dry run is selected by default. Live deploy requires two checkboxes and typing the change number. Live deploy uses `commit confirmed <minutes>`.
7. Phase 4 Audit: waits the configured seconds, logs back in, and can confirm the pending commit after a successful audit login.
8. Phase 5 Reports: writes CSV, JSON, and management Markdown files under `data/reports`.

## Architecture

- `app.py`: Streamlit dashboard.
- `device_ems_telemetary_update/models.py`: run, device, credential, and desired-state models.
- `device_ems_telemetary_update/adapters/junos.py`: Junos discovery, deploy, and audit driver.
- `device_ems_telemetary_update/template_engine.py`: model-aware Jinja template rendering.
- `templates/junos/*.set.j2`: Junos config templates by platform family.
- `data/runs`: persisted run state per change number.
- `data/reports`: generated reporting artifacts.

## Safety Defaults

- Deployment is dry run by default.
- Live deploy is locked unless both approvals are checked and the change number is typed exactly.
- Live Junos deployment uses a commit-confirm timer, defaulting to 30 minutes.
- Audit can confirm the pending commit only after a successful login.
- Credential profiles are encrypted at rest with a local passphrase-derived Fernet key.

## Research Notes

The implementation follows common network automation patterns from Junos PyEZ, Netmiko, and Nornir-style inventory/workflow separation:

- Juniper PyEZ supports candidate configuration load, diff, commit check, and confirmed commits.
- Netmiko provides a practical SSH CLI fallback for Junos devices where NETCONF is not already available.
- Nornir-style separation of inventory, task execution, and vendor adapters is reflected in the workflow and adapter modules without forcing a full Nornir inventory on day one.

## Validation

```bat
venv\Scripts\activate.bat
python -m pytest
python -m compileall .
```

## Notes For Later Expansion

- Add `adapters/saos.py` for Ciena SAOS once command and commit semantics are defined.
- Add model-specific templates for special EX/VC, MX, and PTX protection-filter cases.
- Add CSV import and export for target lists.
- Add optional `commit check` dry-run mode that opens a candidate session but rolls back before commit.
