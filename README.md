# Device EMS Update

Brings the management plane of every Juniper router and switch to one standard --
TACACS+ servers (the move to ISE), authentication order, login classes and users,
accounting, NTP, syslog and SNMP -- on one device or five hundred, with a
self-reverting commit and a second login that proves the new AAA works before
anything is made permanent.

## Quick start

```bash
git clone <this repo>
cd Device_EMS_Telemetary_Update
./setup.sh                                   # venv + requirements.txt (flask, jinja2, pytest)
venv/bin/python cli.py check                 # creates config/desired_state.json from the shipped standard
                                             # and lists exactly what is left to fill
./run_webapp.sh                              # http://127.0.0.1:5460
```

Windows: double-click `run_dashboard.bat` (builds the venv, starts the server, opens the
browser once it answers). The console binds 127.0.0.1 only and starts in **DRY RUN**.

**What a new user fills in** -- everything else ships prefilled with the fleet standard:

| where | what |
|---|---|
| `config/desired_state.json` (created on first start, not in git) | `tacacs.secret`, the two SNMP community names |
| Console -> Settings | the RANCID folder (for Rehearse) |
| `~/.cloginrc` | your own TACACS/ISE account -- jlogin's, not the tool's |
| Console -> Credentials (`config/credentials.json`) | static local users for boxes whose TACACS is already dead -- tried automatically when jlogin's own login is refused |

## The fix, device by device

```
1 DISCOVER   jlogin, read-only   show version + display-set of system / groups / snmp / lo0 / fxp0
             ladder: jlogin default (.cloginrc) -> static local users in order
2 BUILD      no device           platform template -> 01_<host>_ems_fix.txt, minimised:
                                 lines the box already has are dropped; nothing left = COMPLIANT
3 DEPLOY     same credential     configure exclusive / lines / show | compare /
                                 commit confirmed <N> comment ISE_FIX_Script_PENDING
4 CONFIRM    CONFIRM ladder      a NEW login (by default only the jlogin/ISE account):
   (+delay)                      verify shows, then commit comment ISE_FIX_Script and-quit
                                 login fails or verification fails -> nothing committed,
                                 the box reverts itself at +N min  (ROLLBACK PENDING)
5 RECHECK    after the timer     only for the failures: is the old config back?
                                 ROLLED BACK (fix by hand) / still applied (needs a human)
```

Why the second login: the deploy session got in with whatever worked (old TACACS or
a local account). Logging in again *through the new servers* is the only proof the
change didn't lock the fleet out. If that login can't happen the device must revert
on its own, so the confirm ladder does not fall back to local accounts unless you put
one in `confirm_order` yourself.

## Command line

```bash
cli.py check
cli.py new CM12345 --file targets.txt --cm CM12345      # or --targets pe01,pe02
cli.py rehearse CM12345 --rancid-folder /mnt/.../configs  # files only, nothing contacted
cli.py fix CM12345 --live                                 # prints the plan
cli.py fix CM12345 --live --yes                           # runs it; resumable
cli.py fix CM12345 --live --yes --devices pe07 --redo     # one box again
cli.py recheck CM12345 --live --yes --wait                # after the timers
cli.py status CM12345 [--csv board.csv]
cli.py mop CM12345                                        # MOP_<run>.md + .html
cli.py preview --config-file some_dump.txt [--platform ex]  # the fix file one dump gets
cli.py ledger [--state rollback_pending] [--export fleet.csv]
cli.py hash-password                                      # $6$ hash for a local user
```

`targets.txt`: one device per line, optional `,ex` / `,srx` / `,mx` when the platform
can't be read from the box (offline RANCID builds). `--workers` 1-10 (default 8).

Nothing reaches a device without `--live`; `--live` without `--yes` only prints the plan.

## Desired state (`config/desired_state.json`)

In the console, **Desired state** is a form: one tab per platform (MX is the base; EX, SRX
and ACX tabs edit what that platform gets, and only the differences are stored), fields
for the TACACS/NTP/syslog servers and the secret, SNMP communities and traps, login users
(`name, class, uid, hash` lines) and classes (add/remove rows), and a **Show as set
commands** button that renders the platform's standard as plain Junos `set` lines. The
raw JSON stays available under *Advanced*. The file itself:

`config/desired_state.default.json` is the fleet standard and ships with the tool; on first
start it is copied to `desired_state.json`, which is where the secret goes and which git
ignores. One file describes the standard. Top-level sections apply to every platform;
`platforms.ex` / `platforms.srx` / `platforms.acx` override them. A mapping such as
`classes`, `users`, `communities` or `hosts` in an override *replaces* the base one;
scalars and lists replace; other objects merge. `"auto"` for a source-address keeps the
device's current one, else uses the fxp0 master-only address (MX) or lo0 (EX); if
neither exists the line is omitted and the fix file says so.

Sections: `commit` (comment, confirmed_minutes, confirm_delay_seconds, timeout_seconds),
`tacacs` (servers, secret, port, single_connection, source_address, apply_group,
rotate_secret, authentication_order, accounting), `radius.delete`, `login` (classes,
users, delete_users, delete_unlisted_users, protect_users), `ntp`, `syslog`, `snmp`
(communities, trap_group, trap_source_address, filter_interfaces, filter_duplicates,
contact, location, managers).

Rules the builder enforces whatever the file says:

- `root`, the `remote*` template users and any user on the credential ladder are never
  deleted.
- A user's `uid` is kept from the box when the file doesn't give one.
- `rotate_secret: true` (default) re-sends the TACACS block on every box even when the
  servers already match -- that is how a new ISE secret gets everywhere. Set it to
  false afterwards and compliant boxes stay untouched.
- `snmp.managers` are checked against the box's prefix-lists; a poller that no
  prefix-list covers while lo0 carries a protect filter is a WARNING in the fix file.
- A desired-state value still reading `REPLACE_WITH...` blocks every build.

## Credentials (`config/credentials.json`, 0600, never committed)

```json
{
  "jlogin_default": true,
  "local_users": [ {"label": "ccf_cm_user", "username": "ccf_cm_user", "password": "..."} ],
  "discover_order": ["jlogin-default", "ccf_cm_user"],
  "confirm_order": ["jlogin-default"]
}
```

`jlogin-default` is whatever `~/.cloginrc` holds. A static user is handed to jlogin
through a throw-away cloginrc file (`-f`, mode 0600, deleted after the session), never
on the command line. The ladder moves to the next credential only on an auth /
no-session verdict; unreachable, rejected, or a committing session that timed out
ends it, because retrying could re-send a commit.

## Templates

`templates/junos/mx/ems_fix.set.j2` (MX and ACX), `templates/junos/ex/ems_fix.set.j2`,
`templates/junos/srx/ems_fix.set.j2` (starts as the EX shape, flagged UNVALIDATED until
a real SRX diff has been read). Written as exact-state -- an object is deleted and set
in full -- so they read like config. The builder then drops every object the box
already has, every `set` already present and every `delete` of something absent.
`templates/iosxr/asr9k/` and `templates/saos/ciena/` hold notes for the next platforms.

## What a run leaves behind

```
runs/<run>/
  targets.txt  run.json  status.json          per-device records, written after every step
  devices/<host>/01_<host>_ems_fix.txt        header, config, # --- Verify ---, # --- Rollback ---
  devices/<host>/before_<host>.txt            the discovery transcript / RANCID dump
  devices/<host>/logs/<ts>_<host>_<action>.cmd / .log    exactly what was sent, and the answer
  MOP_<run>.md / .html   board_<run>.csv
ledger.db                                     fleet view across runs (cli.py ledger, console Ledger)
logs/errors.log                               every non-zero exit and unhandled exception
```

Every session log starts with `# Verdict: ...`. jlogin's exit code is never trusted --
RANCID login scripts exit 0 after `Error: Couldn't login` -- so the verdict is read
from the transcript:

| verdict | the transcript showed | what happens |
|---|---|---|
| `dns` | the name doesn't resolve (checked before the session too) | nothing sent; fix resolution or use the address |
| `unreachable` | refused / timed out / no route | nothing sent; ladder stops |
| `auth` | permission denied, password refused | next credential; all refused = nothing sent |
| `no-session` | `Couldn't login`, EOF, empty transcript, no prompt | next credential |
| `rejected` | a Junos `error:` line, check-out or commit failed | the whole commit was aborted |
| `timeout` | no answer within the timeout | for a commit: check the box, the timer reverts it |
| `ok` | `commit complete` **and** the "will be automatically rolled back" line (deploy); a prompt (read-only) | -- |

A device whose confirm failed is **ROLLBACK PENDING** with its due time; `fix` leaves it
alone until the timer has expired, `recheck` then proves the old config is back.

## Console

Dark shell, same family as the HotCut console. Pages: Console (fleet counts, the six
steps, recent runs), Runs / New run (paste or upload targets), Run (device board with
*Select not done / failed / rollback pending*, Rehearse, Discover, Build, **Run ISE
fix**, Recheck, MOP, CSV, live output), Desired state (JSON editor with validation and a
per-platform summary), Credentials (ladders, masked; edits write the file 0600),
Templates, Ledger, Errors, Manual, Settings (theme, default RANCID folder, workers).

Safety: the console starts disarmed; arming means typing `LIVE`; a restart disarms;
live buttons appear only when armed; the ISE fix shows its plan first and runs on the
second click with its own "for real" tick; POSTs are Origin/Referer-checked; downloads
are path-contained under `runs/`.

## Local rehearsal without a device

`captures/fake_jlogin.py` answers `show configuration` from a folder of dumps and
"applies" commits, so the whole flow can be run on a laptop:

```bash
export JLOGIN_BIN=$PWD/captures/fake_jlogin.sh FAKE_RANCID=/path/to/dumps EMS_SKIP_DNS_CHECK=1
export FAKE_LOCAL_ONLY=sw01 FAKE_CONFIRM_FAIL=pe02      # exercise the ladder and a rollback
venv/bin/python cli.py fix TEST --live --yes --delay 0
```

## Tests

```bash
venv/bin/python -m pytest tests/ -q
```

## Design

The previous Streamlit tool was retired on 2026-10-03 (a copy is kept outside the repo).
`docs/DESIGN.md` is the blueprint (sequence, safety model, layout, desired-state
schema, how to add a platform). `docs/RUN_FLOW.html` is the run-flow diagram.
