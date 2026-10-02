# Handoff -- Device EMS Console rebuild

Written for: the next session (any model) continuing this work, and for the
operator reading what was built overnight. Read `docs/DESIGN.md` first.

## State at hand-off (2026-10-02, overnight build)

Built and tested on the laptop against real sample dumps (two production MX
display-set captures and one EX3200, kept outside the repo) and the local
jlogin simulator:

- `core/` engine: parser, desired-state loader with per-platform overrides,
  three templates (MX+ACX / EX / SRX-unvalidated), builder with minimisation
  and restore-style rollback, structural verifier, jlogin session runner with
  credential ladder + transcript verdicts, per-device pipeline, parallel
  resumable bulk runner, status.json merge-on-save, SQLite fleet ledger, MOP.
- `cli.py`: check / new / rehearse / fix / discover / build / recheck / status /
  ledger / mop / templates / preview / hash-password.
- `webapp.py`: Flask console on :5460 (HotCut-style shell), device board with
  selection helpers, two-touch live fix, desired-state editor, credentials
  editor (0600), templates view, ledger, errors, manual, settings.
- Tests: `tests/` (parser, desired, builder, verify, session, status/creds/
  ledger, bulk+pipeline with the simulator, cli, webapp) -- all passing.
- Docs: README (incl. old-vs-new comparison), DESIGN.md, RUN_FLOW.html,
  LEGACY_README.md (the original app's README, kept verbatim).
- `config/desired_state.json` (gitignored) is pre-filled with the fleet's
  current standard; three values still read REPLACE_WITH: `tacacs.secret` and
  the two SNMP community names. `cli.py check` lists them.
- The legacy Streamlit tool is untouched and still runnable (`./run.sh`, its
  own `.venv`, `requirements.txt`).

## What is NOT done / needs Varun

1. **Real-device rehearsal.** Nothing has touched a real router. First real use:
   `cli.py new CM-x --targets <one MX>`, `cli.py rehearse` against the real
   RANCID mount, read the fix file, then `cli.py fix --live --yes` on that one
   box with `--confirmed-minutes 10`, watch the confirm, then a second box.
2. **ISE account in `~/.cloginrc`** on the work server must be the account that
   authenticates through the NEW servers -- otherwise every confirm fails and
   every box rolls back (by design). Confirm this before any bulk run.
3. **Secret + communities** in `config/desired_state.json`.
4. **SRX**: template is the EX shape, flagged UNVALIDATED. Rehearse an SRX and
   diverge the file if the diff shows anything SRX-specific (security zones
   host-inbound-traffic for ssh/snmp/ntp is NOT touched by this tool).
5. **ACX**: follows the MX template (same ccf classes), but the one ACX sample
   had `config-provision-ccf permissions all` and no trap-group -- decide
   whether ACX gets its own `platforms.acx` override or is brought to the MX
   standard. Default today: MX standard.
6. **EX3200 on Junos 12.3**: `single-connection` and `deny-commands-regexps`
   exist there, but rehearse one before the EX bulk run.
7. **Rollback timer vs. confirm session**: the confirm waits
   `confirm_delay_seconds` (20) then logs in. On a slow box a 180 s session
   timeout + ladder retries could eat a few minutes; the 20-minute default
   leaves plenty of room. Don't drop `confirmed_minutes` below ~8.
8. **Git**: all of this is committed locally; nothing pushed. Push needs
   Varun's per-push OK and the standing content scrub (no real hostnames in
   tracked files -- `config/*.json` and `runs/` are ignored; README/docs use
   RFC 5737 addresses only). The repo is github.com/titan101/Device_EMS_Telemetary_Update.
9. **Agent review** (Network Engineer + security + QA, per the project rule)
   has not been run on this code yet -- it is the next step before any
   production use. Brief them with DESIGN.md and the real sample dumps.
10. **ASR9k / Ciena**: placeholders only (`templates/iosxr/asr9k/README.md`,
    `templates/saos/ciena/README.md`), see DESIGN.md section 10.

## How to continue

```bash
cd Device_EMS_Telemetary_Update
venv/Scripts/python.exe -m pytest tests/ -q          # Windows laptop
# local end-to-end without a device:
export JLOGIN_BIN=$PWD/captures/fake_jlogin.cmd FAKE_RANCID=<folder of dumps> EMS_SKIP_DNS_CHECK=1
venv/Scripts/python.exe cli.py new T1 --targets <dump name>
venv/Scripts/python.exe cli.py fix T1 --live --yes --delay 0 --desired <file without placeholders>
```

Real-shaped dumps for local runs: copy `show configuration | display set`
captures into a scratch folder as `<host>.txt` (never into the repo).
