# Device EMS Console -- design and structure

This document is the blueprint for the rebuilt tool. Anyone continuing the
build (or the operator reading the code) should be able to work from it
without the conversation that produced it.

## 1. What the tool is for

Rewrite the management-plane ("EMS") configuration on every Juniper router
and switch in the fleet -- TACACS+ servers (moving the AAA backend to ISE),
authentication order, login classes and users, accounting, NTP, syslog and
SNMP -- one device or 500 at a time, with a self-reverting commit and a
second login that proves the new AAA works before anything is made permanent.

Platforms today: Juniper MX (and ACX, same standard), Juniper EX (3200/3300/
3400) and SRX. The layout leaves room for Cisco ASR9k (IOS-XR) and Ciena SAOS
later: `templates/<os>/<platform>/` plus a parser/session driver per OS.

## 2. The per-device sequence (the "ISE fix")

```
 1 DISCOVER   jlogin, read-only       show version + display-set of system/groups/snmp/lo0/fxp0
               credential ladder:     jlogin default (.cloginrc)  ->  static local users in order
 2 BUILD      no device               platform template -> 01_<host>_ems_fix.txt
                                      (minimised: lines the box already has are dropped;
                                       nothing left = device is COMPLIANT, stop here)
 3 DEPLOY     jlogin, same credential configure exclusive / lines / show | compare /
                                      commit confirmed <N> comment ISE_FIX_Script_PENDING
 4 CONFIRM    jlogin, CONFIRM ladder  (default: ONLY the jlogin default account -- the ISE one)
   (after a short delay)              show configuration ... (verification) then
                                      commit comment ISE_FIX_Script and-quit
               login fails / verify fails  ->  nothing is committed; the box reverts
                                               itself at +N minutes (ROLLBACK PENDING)
 5 RECHECK    jlogin, discover ladder after the timer: is the OLD config back?
   (only for devices whose confirm failed)   ok -> ROLLED BACK (fix by hand)
                                              new config still there -> FAILED, needs a human
```

Why a second login instead of confirming inside the deploy session: the
deploy session is authenticated by whatever got us in (old TACACS or a local
account). Logging in again through the new servers is the only proof that the
change didn't lock the fleet out. If that login can't happen, the device must
revert on its own -- so the confirm ladder does not fall back to local accounts
unless the operator explicitly allows it (`confirm_order` in credentials.json).

## 3. Safety model (same as juniper_customer_migration)

- Nothing reaches a device unless asked twice: the CLI defaults to a dry run
  (`--live` opens sessions); the console starts DISARMED, arming means typing
  `LIVE`, a restart disarms, and every live action also carries its own tick.
- One write path: `core/session.py` is the only module that spawns jlogin.
  Command files are built from fixed shapes; every config line must start
  with set/delete/activate/deactivate and carry no placeholder.
- jlogin's exit code is never trusted. A verdict is read from the transcript:
  ok / timeout / dns / unreachable / auth / no-session / rejected / failed.
  OK needs positive evidence (`commit complete` AND the "will be automatically
  rolled back" line for a deploy; a Junos prompt for a read-only session).
- Dry runs never overwrite live records (`<step>_rehearsal` keys).
- Every generated file carries its header, the verify commands and an exact
  rollback built from the device's own pre-change lines (restore, not
  string-inversion) -- for use after a confirm, when the timer can't help.
- Protected users (`root`, the `remote*` template users, anything on the
  credential ladder) are never deleted, whatever the desired state says.

## 4. Reliability for a 500-device run

- Resumable by construction: `runs/<run>/status.json` holds one record per
  device per step (verdict, reason, credential, timestamps, log name, sha of
  what was sent), written after EVERY step with re-read + overlay + tmp/replace.
  A second invocation skips devices already confirmed live; `--redo` repeats.
- Parallel: a ThreadPoolExecutor of workers (default 8, max 10 -- a jump
  host's sshd MaxStartups refuses around 10). Workers only do I/O and return
  events; every shared write (stdout, status file, ledger, error log) happens
  on the main thread.
- Ctrl-C: queued devices never start, open sessions are recorded as "outcome
  unknown", exit 130.
- Heartbeat every 15 s naming the devices actually in session vs queued.
- Per-device failures are caught at the device boundary and reported; a
  reporting bug can't lose the other devices.
- The cross-run ledger (`ledger.db`, SQLite) answers "which boxes are done".

## 5. Layout

```
cli.py                      every subcommand; main() funnels all exits to the error log
webapp.py                   Flask console on :5460 -- a launcher over cli.py (subprocess + log tail)
core/
  models.py                 DeviceFacts, ExistingConfig, DesiredState, step records
  config_parser.py          display-set text -> ExistingConfig (+ platform heuristic for RANCID dumps)
  desired.py                desired_state.json loader, per-platform merge, validation
  templates.py              Jinja env, platform -> template path
  builder.py                render, minimise against the live config, verify commands, rollback, file writer
  verify.py                 confirm-session transcript -> PASS/FAIL against the fix file
  session.py                jlogin runner (command files, verdicts, credential ladder, timeouts)
  credentials.py            credentials.json (ladders) + throw-away cloginrc for static users
  pipeline.py               the per-device sequence (discover/build/deploy/confirm/recheck)
  bulk.py                   ThreadPool runner, heartbeat, Ctrl-C, main-thread recording
  status.py                 runs/<run>/status.json merge-on-save + derived device state
  ledger.py                 SQLite fleet ledger
  rancid.py                 RANCID folder reader (offline before-picture)
  mop.py                    MOP (markdown + html) per run
  errorlog.py               logs/errors.log
templates/
  junos/mx/ems_fix.set.j2   MX + ACX standard (apply-group tacplus_servers, ccf classes, accounting)
  junos/ex/ems_fix.set.j2   EX standard (per-server tacplus lines, no apply-group)
  junos/srx/ems_fix.set.j2  starts as the EX shape; marked UNVALIDATED until a real SRX is reviewed
  iosxr/asr9k/README.md     placeholder -- hierarchical config, needs its own parser/session driver
  saos/ciena/README.md      placeholder
config/
  desired_state.default.json   the fleet standard, shipped; copied to desired_state.json on first start (gitignored)
  credentials.example.json     the ladders (copy to credentials.json, chmod 600, gitignored)
  targets.example.txt          one device per line, optional ",platform"
runs/<run>/                 targets.txt, run.json, status.json, devices/<host>/{01_<host>_ems_fix.txt,
                            before_<host>.txt, logs/*.cmd, logs/*.log}, MOP_<run>.md
docs/DESIGN.md (this), RUN_FLOW.html (run-flow diagram), HANDOFF.md (what's left)
tests/                      pytest; fake jlogin launchers, fixture dumps (gitignored)
captures/fake_jlogin.py     local simulator for end-to-end rehearsal (JLOGIN_BIN=...)
```

The previous Streamlit tool was removed from the repo on 2026-10-03 (copy kept in
My_Production_Sample_Configs/Device_EMS_Telemetary_Update/original_scripts/, outside git).

## 6. Desired state (config/desired_state.json)

One JSON file describes the standard. Top-level keys apply to every platform;
`platforms.<name>` deep-merges overrides (EX has no apply-group, no accounting,
its own user list; SRX inherits EX). "auto" for a source-address means: keep
the device's current one; else the fxp0 master-only address (MX) or the lo0
address (EX); else omit the line and say so in the file header.

```
commit:    comment, confirmed_minutes, confirm_delay_seconds, timeout_seconds
tacacs:    servers[], secret, port, single_connection, timeout, source_address,
           apply_group ("" = per-server lines), authentication_order[],
           accounting {events[], destination}
radius:    delete
login:     classes{name: {idle_timeout, permissions[], allow/deny regexps[]}},
           users{name: {class, uid?, encrypted_password?}}, delete_users[],
           protect_users[], delete_unlisted_users
ntp:       servers[], source_address
syslog:    hosts{ip: ["facility severity", ...]}, source_address, delete_other_hosts
snmp:      communities{name: {authorization, clients[]}}, delete_other_communities,
           trap_group{name, version, categories[], targets[]}, trap_source_address,
           filter_interfaces, filter_duplicates, contact, location, managers[]
platforms: {mx: {}, acx: {}, ex: {...}, srx: {...}}
```

Facts from the production samples that the example file encodes:
- MX fleet standard: `groups tacplus_servers system tacplus-server <*> {port 49,
  secret, single-connection, source-address <fxp0 master-only>}`,
  `apply-groups tacplus_servers`, `authentication-order tacplus` (no local
  fallback), accounting login/change-log/interactive-commands -> tacplus,
  classes config-provision-ccf / ro-ccf / super-user-ccf / superuser-local,
  template users remote(unauthorized) / remote-admin / remote-operator /
  remote-provision plus local ccf_cm_user and zccfrancid, syslog two hosts
  `any any` + source-address, NTP two servers + source-address, SNMP two
  read-only communities, trap-group public v2 with eight categories and two
  targets, trap-options source-address, filter-interfaces + filter-duplicates.
- The ISE class fix for 21.2 (from Juniper_Snapshot.sh): config-provision-ccf
  loses `permissions shell`, gains `maintenance`, deny-commands-regexps
  "start shell" and "^request system|vmhost|chassis|session .*".
- The one real EX (EX3200, 12.3): three old ACS servers with per-server
  secrets, built-in super-user class, local users admin/hvdmc/rancid, no NTP,
  no syslog hosts, trap-options source-address lo0 (interface keyword).
- lo0 `protect-RE` filters use `apply-path` prefix-lists for tacplus and ntp
  servers, so new server IPs are permitted automatically; SNMP pollers are
  matched against explicit prefix-lists -- the build step warns when a listed
  `snmp.managers` address is not covered.
- uids differ per device; never hard-code them unless the desired state does.

## 7. Minimisation and compliance

The templates are written as exact-state (`delete <object>` then its `set`
lines) because that is readable. `builder.minimise()` then drops every object
whose existing lines already equal the desired lines, every `set` the box
already has and every `delete` of something that isn't there. A device that
ends with zero lines is recorded COMPLIANT and never gets a commit. Objects are
compared with secrets masked, because the box shows `secret "$9$..."` for the
plaintext the template holds -- a secret that is *present* is treated as equal
unless `tacacs.rotate_secret` is true.

## 8. Verification (confirm session)

The confirm command file runs the fix file's `# --- Verify ---` show commands
before the empty commit. `verify.check()` requires every emitted `set` line to
appear in the transcript as a full line (secret-bearing lines are matched up to
the keyword) and every deleted object to be absent. Only a PASS plus `commit
complete` records the device as CONFIRMED; a transcript that merely echoes the
commands can't pass.

## 9. Console (webapp.py)

Dark "midnight + ember" shell copied from the HotCut console (BASE_CSS, nav
with DRY RUN / LIVE pill, cards, chips, device board). Pages: Console (fleet
stats + stepper), New run (paste or upload targets, options), Run (device
board with Select failed / Select not done / Select rollback-pending, Rehearse,
Run ISE fix, Recheck, MOP, CSV), Desired state (per-platform form: MX base +
EX/SRX/ACX tabs storing only differences, set-command preview, raw JSON under
Advanced with the secret masked), Credentials (ladders, masked, jlogin health), Templates (read-only
view), Ledger, Errors, Manual, Settings (theme, RANCID folder, auto-close).
Binds 127.0.0.1 only. POSTs are Origin/Referer-checked. Downloads are
path-contained under `runs/`.

## 10. Adding a platform

1. A parser that produces `ExistingConfig` from that OS's running config.
2. A session driver with the OS's commit semantics (IOS-XR: `commit confirmed`
   exists; SAOS6 has no candidate -- different safety story).
3. `templates/<os>/<platform>/ems_fix.<fmt>.j2`.
4. `templates.py` platform table + `config_parser.classify_platform`.
Everything else (ladder, status, bulk runner, console) is OS-agnostic.
