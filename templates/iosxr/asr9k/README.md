# Cisco ASR9k (IOS-XR) -- placeholder

Not built yet. What the samples (`My_Production_Sample_Configs/asr9k_migration/`,
IOS-XR 5.1.3) say the equivalent stanzas look like, for whoever adds this:

- TACACS: `tacacs-server host <ip> port 49` + ` key 7 <secret>`,
  `aaa group server tacacs+ <group>` with ` server <ip>` lines,
  `aaa authentication login <list> group <group> local`,
  `aaa accounting commands default start-stop group <group>`,
  `line default` / ` login authentication <list>`.
- RADIUS: `radius-server host <ip> auth-port 1812 acct-port 1813` + key, `aaa group server radius`.
- Local users: `username X` / ` group root-system` / ` password 7 ...`.
- NTP: `ntp` block with ` server <ip> minpoll 8 maxpoll 12`.
- Syslog: `logging <ip> vrf default severity info`.
- SNMP: `snmp-server community <c> RO|RW`, `snmp-server host <ip> traps version 2c <c>`,
  `snmp-server traps ...`, `snmp-server contact/location`.
- Management-plane ACL: `control-plane management-plane inband interface Loopback0
  allow SSH|SNMP peer address ipv4 <ip>` -- new servers need peer entries.

Needed: a hierarchical parser (indent-aware blocks, not flat set lines), a session
driver using IOS-XR `commit confirmed <n>` + `commit` (clogin, not jlogin), and
`templates/iosxr/asr9k/ems_fix.cfg.j2`. See docs/DESIGN.md section 10.
