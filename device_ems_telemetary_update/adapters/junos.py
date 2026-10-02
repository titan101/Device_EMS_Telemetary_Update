from __future__ import annotations

import asyncio
import re
import time
from contextlib import suppress

from .base import DeviceAdapter
from ..device_types import classify_junos_platform
from ..models import CredentialProfile, DesiredState, DeviceRecord, ExistingConfig


CONFIG_COMMANDS = {
    "tacacs": "show configuration system tacplus-server | display set | no-more",
    "radius": "show configuration | display set | match \"radius-server\" | no-more",
    "login": "show configuration system login | display set | no-more",
    "ntp": "show configuration system ntp | display set | no-more",
    "syslog": "show configuration system syslog | display set | no-more",
    "snmp": "show configuration snmp | display set | no-more",
    "netconf": "show configuration system services netconf | display set | no-more",
    "lldp": "show configuration protocols lldp | display set | no-more",
}


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _parse_existing(sections: dict[str, str]) -> ExistingConfig:
    tacacs = re.findall(r"set system tacplus-server\s+(\S+)", sections.get("tacacs", ""))
    radius = re.findall(r"set (?:system|access) radius-server\s+(\S+)", sections.get("radius", ""))
    login_users = re.findall(r"set system login user\s+(\S+)", sections.get("login", ""))
    ntp = re.findall(r"set system ntp server\s+(\S+)", sections.get("ntp", ""))
    syslog = re.findall(r"set system syslog host\s+(\S+)", sections.get("syslog", ""))
    snmp = re.findall(r"set snmp community\s+(\S+)", sections.get("snmp", ""))
    snmp_trap_targets = re.findall(r"set snmp trap-group\s+\S+\s+targets\s+(\S+)", sections.get("snmp", ""))
    radius_delete_lines = []
    for line in sections.get("radius", "").splitlines():
        line = line.strip()
        if line.startswith("set system radius-server ") or line.startswith("set access radius-server "):
            radius_delete_lines.append("delete " + line[4:])
    login_user_delete_lines = [f"delete system login user {user}" for user in _unique(login_users)]
    return ExistingConfig(
        tacacs_servers=_unique(tacacs),
        radius_servers=_unique(radius),
        radius_delete_lines=_unique(radius_delete_lines),
        login_users=_unique(login_users),
        login_user_delete_lines=login_user_delete_lines,
        ntp_servers=_unique(ntp),
        syslog_hosts=_unique(syslog),
        snmp_communities=_unique(snmp),
        snmp_trap_targets=_unique(snmp_trap_targets),
        netconf_enabled="set system services netconf ssh" in sections.get("netconf", ""),
        lldp_enabled="set protocols lldp" in sections.get("lldp", ""),
        raw_sections=sections,
    )


def parse_rancid_dump(text: str) -> ExistingConfig:
    """Parse a full 'show configuration | display set' text dump (e.g. a RANCID
    config file) using the same section regexes as live per-command discovery.
    Every regex anchors on its full 'set ...' line prefix, so passing the whole
    dump under every section key is safe and picks up all fields in one pass."""
    sections = {name: text for name in CONFIG_COMMANDS}
    return _parse_existing(sections)


def _ordered_credentials(credentials: list[CredentialProfile], audit_only: bool = False) -> list[CredentialProfile]:
    enabled = [credential for credential in credentials if credential.enabled]
    if audit_only:
        audit = [credential for credential in enabled if credential.role == "audit"]
        if audit:
            return audit
    role_order = {"primary": 0, "secondary": 1, "audit": 2}
    return sorted(enabled, key=lambda credential: role_order.get(credential.role, 9))


class JunosAdapter(DeviceAdapter):
    vendor = "junos"

    def discover(
        self,
        target: str,
        credentials: list[CredentialProfile],
        desired: DesiredState | None = None,
    ) -> DeviceRecord:
        record = DeviceRecord(target=target, address=target, vendor=self.vendor)
        for credential in _ordered_credentials(credentials):
            record.auth_attempts.append(credential.label)
            try:
                return self._discover_pyez(record, credential)
            except Exception as pyez_exc:
                record.log(f"PyEZ discovery failed with {credential.label}: {pyez_exc}")
                try:
                    return self._discover_netmiko(record, credential)
                except Exception as netmiko_exc:
                    record.log(f"Netmiko discovery failed with {credential.label}: {netmiko_exc}")
                    record.error = str(netmiko_exc)
        if desired and desired.discovery_snmp_communities:
            snmp_record = self._discover_snmp(record, desired.discovery_snmp_communities)
            if snmp_record.status == "discovery_complete":
                return snmp_record
        record.phase = "discovery"
        record.status = "auth_failed"
        if not record.error:
            record.error = "All credential profiles failed."
        return record

    def deploy(
        self,
        record: DeviceRecord,
        credentials: list[CredentialProfile],
        desired: DesiredState,
        change_id: str,
        dry_run: bool = True,
    ) -> DeviceRecord:
        if not record.generated_config:
            record.status = "build_required"
            record.error = "No generated config exists for this device."
            return record
        if dry_run:
            record.phase = "deploy"
            record.status = "dry_run"
            record.deploy_result = "Dry run only. No device connection was opened for deployment."
            record.log("Dry-run deployment reviewed.")
            return record
        credential = self._credential_for_record(record, credentials)
        try:
            if record.driver == "pyez":
                self._deploy_pyez(record, credential, desired, change_id)
            else:
                self._deploy_netmiko(record, credential, desired, change_id)
            record.phase = "deploy"
            record.status = "deployed_confirm_pending"
            record.error = ""
        except Exception as exc:
            record.phase = "deploy"
            record.status = "deploy_failed"
            record.error = str(exc)
            record.log(f"Deployment failed: {exc}")
        return record

    def audit(
        self,
        record: DeviceRecord,
        credentials: list[CredentialProfile],
        desired: DesiredState,
        change_id: str,
        confirm_commit: bool = False,
        audit_only: bool = True,
    ) -> DeviceRecord:
        wait_seconds = max(0, desired.audit_wait_seconds)
        if wait_seconds:
            record.log(f"Waiting {wait_seconds} seconds before audit login.")
            time.sleep(wait_seconds)
        last_error = ""
        for credential in _ordered_credentials(credentials, audit_only=audit_only):
            try:
                if record.driver == "pyez":
                    self._open_pyez(record.target, credential).close()
                else:
                    conn = self._open_netmiko(record.target, credential)
                    conn.disconnect()
                record.phase = "audit"
                record.status = "audit_passed"
                record.audit_result = f"Login succeeded with credential profile {credential.label}."
                record.error = ""
                record.log(record.audit_result)
                if confirm_commit:
                    self._confirm_commit(record, credential, change_id)
                    record.status = "audit_passed_commit_confirmed"
                    record.audit_result += " Commit confirmed."
                return record
            except Exception as exc:
                last_error = str(exc)
                record.log(f"Audit login failed with {credential.label}: {exc}")
        record.phase = "audit"
        record.status = "audit_failed"
        record.audit_result = "No audit credential could log in."
        record.error = last_error or record.audit_result
        return record

    def _credential_for_record(self, record: DeviceRecord, credentials: list[CredentialProfile]) -> CredentialProfile:
        for credential in credentials:
            if credential.label == record.credential_label and credential.enabled:
                return credential
        ordered = _ordered_credentials(credentials)
        if not ordered:
            raise RuntimeError("No enabled credential profiles are loaded.")
        return ordered[0]

    def _discover_pyez(self, record: DeviceRecord, credential: CredentialProfile) -> DeviceRecord:
        dev = self._open_pyez(record.target, credential)
        try:
            facts = dev.facts or {}
            sections = {}
            for name, command in CONFIG_COMMANDS.items():
                with suppress(Exception):
                    sections[name] = dev.cli(command, warning=False)
            record.hostname = str(facts.get("hostname") or "")
            record.model = str(facts.get("model") or "")
            record.device_type = classify_junos_platform(record.model)
            record.version = str(facts.get("version") or facts.get("junos_info") or "")
            record.serial_number = str(facts.get("serialnumber") or "")
            record.existing = _parse_existing(sections)
            record.driver = "pyez"
            record.credential_label = credential.label
            record.phase = "discovery"
            record.status = "discovery_complete"
            record.error = ""
            record.log(f"Discovery completed through PyEZ with {credential.label}.")
            return record
        finally:
            dev.close()

    def _discover_netmiko(self, record: DeviceRecord, credential: CredentialProfile) -> DeviceRecord:
        conn = self._open_netmiko(record.target, credential)
        try:
            version_output = conn.send_command("show version | no-more")
            sections = {name: conn.send_command(command) for name, command in CONFIG_COMMANDS.items()}
            hostname_match = re.search(r"Hostname:\s+(\S+)", version_output)
            model_match = re.search(r"Model:\s+(\S+)", version_output)
            version_match = re.search(r"Junos:\s+(\S+)", version_output)
            record.hostname = hostname_match.group(1) if hostname_match else ""
            record.model = model_match.group(1) if model_match else ""
            record.device_type = classify_junos_platform(record.model)
            record.version = version_match.group(1) if version_match else ""
            record.existing = _parse_existing(sections)
            record.driver = "netmiko"
            record.credential_label = credential.label
            record.phase = "discovery"
            record.status = "discovery_complete"
            record.error = ""
            record.log(f"Discovery completed through Netmiko with {credential.label}.")
            return record
        finally:
            conn.disconnect()

    def _discover_snmp(self, record: DeviceRecord, communities: list[str]) -> DeviceRecord:
        for community in communities:
            try:
                sys_descr = snmp_get_sysdescr(record.target, community)
            except Exception as exc:
                record.log(f"SNMP discovery failed with community {community}: {exc}")
                continue
            model = parse_model_from_sysdescr(sys_descr)
            record.phase = "discovery"
            record.status = "discovery_complete" if model else "discovery_failed"
            record.driver = "snmp"
            record.credential_label = f"snmp:{community}"
            record.model = model
            record.device_type = classify_junos_platform(model)
            record.version = parse_version_from_sysdescr(sys_descr)
            record.error = "" if model else "SNMP responded but model could not be parsed."
            record.log("Discovery completed through SNMP sysDescr.")
            return record
        return record

    def _open_pyez(self, target: str, credential: CredentialProfile):
        from jnpr.junos import Device

        dev = Device(
            host=target,
            user=credential.username,
            passwd=credential.password,
            port=22,
            normalize=True,
            gather_facts=True,
            auto_probe=5,
        )
        dev.open()
        return dev

    def _open_netmiko(self, target: str, credential: CredentialProfile):
        from netmiko import ConnectHandler

        return ConnectHandler(
            device_type="juniper_junos",
            host=target,
            username=credential.username,
            password=credential.password,
            timeout=20,
            conn_timeout=10,
            banner_timeout=20,
            auth_timeout=15,
        )

    def _deploy_pyez(
        self,
        record: DeviceRecord,
        credential: CredentialProfile,
        desired: DesiredState,
        change_id: str,
    ) -> None:
        from jnpr.junos.utils.config import Config

        dev = self._open_pyez(record.target, credential)
        cu = Config(dev)
        try:
            cu.lock()
            cu.load("\n".join(record.generated_config), format="set", merge=True)
            record.candidate_diff = cu.diff() or ""
            cu.commit_check()
            cu.commit(
                confirm=desired.commit_confirm_minutes,
                comment=f"{change_id} EMS telemetry update commit confirmed",
            )
            record.deploy_result = f"Commit confirmed {desired.commit_confirm_minutes} minutes through PyEZ."
            record.log(record.deploy_result)
        finally:
            with suppress(Exception):
                cu.unlock()
            dev.close()

    def _deploy_netmiko(
        self,
        record: DeviceRecord,
        credential: CredentialProfile,
        desired: DesiredState,
        change_id: str,
    ) -> None:
        conn = self._open_netmiko(record.target, credential)
        try:
            conn.config_mode()
            conn.send_config_set(record.generated_config, exit_config_mode=False)
            record.candidate_diff = conn.send_command("show | compare", expect_string=r"[#>]")
            conn.commit(
                confirm=True,
                confirm_delay=desired.commit_confirm_minutes,
                comment=f"{change_id} EMS telemetry update commit confirmed",
                and_quit=True,
            )
            record.deploy_result = f"Commit confirmed {desired.commit_confirm_minutes} minutes through Netmiko."
            record.log(record.deploy_result)
        finally:
            conn.disconnect()

    def _confirm_commit(self, record: DeviceRecord, credential: CredentialProfile, change_id: str) -> None:
        try:
            from jnpr.junos.utils.config import Config

            dev = self._open_pyez(record.target, credential)
            try:
                Config(dev).commit(comment=f"{change_id} EMS telemetry audit passed")
                record.log("Confirmed pending commit through PyEZ.")
                return
            finally:
                dev.close()
        except Exception as pyez_exc:
            record.log(f"PyEZ confirm failed, trying Netmiko: {pyez_exc}")
        conn = self._open_netmiko(record.target, credential)
        try:
            conn.commit(comment=f"{change_id} EMS telemetry audit passed", and_quit=True)
            record.log("Confirmed pending commit through Netmiko.")
        finally:
            conn.disconnect()


def parse_model_from_sysdescr(sys_descr: str) -> str:
    match = re.search(r"\b(EX|MX|PTX)\d+[A-Z0-9-]*\b", sys_descr or "", flags=re.IGNORECASE)
    return match.group(0).upper() if match else ""


def parse_version_from_sysdescr(sys_descr: str) -> str:
    match = re.search(r"\bJUNOS\s+([A-Z0-9.\-R]+)", sys_descr or "", flags=re.IGNORECASE)
    return match.group(1) if match else ""


def snmp_get_sysdescr(target: str, community: str, timeout: float = 1.5, retries: int = 1) -> str:
    return asyncio.run(_snmp_get_sysdescr_async(target, community, timeout, retries))


async def _snmp_get_sysdescr_async(target: str, community: str, timeout: float, retries: int) -> str:
    from pysnmp.hlapi.v3arch.asyncio import (  # type: ignore[import-untyped]
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        get_cmd,
    )

    transport = await UdpTransportTarget.create((target, 161), timeout=timeout, retries=retries)
    error_indication, error_status, _error_index, var_binds = await get_cmd(
        SnmpEngine(),
        CommunityData(community, mpModel=1),
        transport,
        ContextData(),
        ObjectType(ObjectIdentity("1.3.6.1.2.1.1.1.0")),
    )
    if error_indication:
        raise RuntimeError(str(error_indication))
    if error_status:
        raise RuntimeError(str(error_status.prettyPrint()))
    for _oid, value in var_binds:
        return str(value)
    return ""
