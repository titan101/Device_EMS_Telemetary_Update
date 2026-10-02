# Ciena SAOS -- placeholder

Not built yet. No real SAOS sample with AAA exists in the sample folder; the
synthetic corpus (`Test_Device_Configurations/ciena/`) shows the shape:

- SAOS 6: flat imperative CLI -- `system set host-name`, `snmp community ... access read-only`,
  `ntp client enable`, `ntp server add server <ip>`, `configuration save`. TACACS is
  `tacacs ...` / `aaa ...` add/remove commands (from vendor docs). No candidate config,
  so no `commit confirmed` safety net -- the session driver has to stage an explicit
  undo list instead.
- SAOS 10: `config` ... `commit` block -- `system ntp server <ip> prefer true`,
  `system logging syslog-server <ip> severity info`, `system aaa ...` YANG paths.

Needed: parser, session driver (SAOS has its own login script in RANCID), and
`templates/saos/ciena/ems_fix.cli.j2`. See docs/DESIGN.md section 10.
