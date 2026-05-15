from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import CredentialProfile, DesiredState, DeviceRecord


class DeviceAdapter(ABC):
    vendor = "generic"

    @abstractmethod
    def discover(self, target: str, credentials: list[CredentialProfile]) -> DeviceRecord:
        raise NotImplementedError

    @abstractmethod
    def deploy(
        self,
        record: DeviceRecord,
        credentials: list[CredentialProfile],
        desired: DesiredState,
        change_id: str,
        dry_run: bool = True,
    ) -> DeviceRecord:
        raise NotImplementedError

    @abstractmethod
    def audit(
        self,
        record: DeviceRecord,
        credentials: list[CredentialProfile],
        desired: DesiredState,
        change_id: str,
        confirm_commit: bool = False,
        audit_only: bool = True,
    ) -> DeviceRecord:
        raise NotImplementedError
