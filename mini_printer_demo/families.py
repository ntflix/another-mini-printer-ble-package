from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

PayloadT = TypeVar("PayloadT")


class FlowControlMode(Enum):
    NOTIFY_PAUSE_RESUME = "notify_pause_resume"
    NONE = "none"


@dataclass(frozen=True)
class FamilyProfile:
    service_uuid: str
    write_uuid: str
    notify_uuid: str
    command_write_uuid: str | None = None
    data_notify_uuid: str | None = None
    flow_control: FlowControlMode = FlowControlMode.NONE


class DeviceFamily(ABC, Generic[PayloadT]):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def profile(self) -> FamilyProfile:
        raise NotImplementedError

    @abstractmethod
    def build_payload(self, *, image_bytes: bytes) -> PayloadT:
        raise NotImplementedError

    def choose_write_uuid(self, *, is_command: bool) -> str:
        if is_command and self.profile.command_write_uuid is not None:
            return self.profile.command_write_uuid
        return self.profile.write_uuid


class Ae30V5Family(DeviceFamily[bytes]):
    _profile = FamilyProfile(
        service_uuid="0000ae30-0000-1000-8000-00805f9b34fb",
        write_uuid="0000ae03-0000-1000-8000-00805f9b34fb",
        command_write_uuid="0000ae01-0000-1000-8000-00805f9b34fb",
        notify_uuid="0000ae02-0000-1000-8000-00805f9b34fb",
        data_notify_uuid="0000ae04-0000-1000-8000-00805f9b34fb",
        flow_control=FlowControlMode.NOTIFY_PAUSE_RESUME,
    )

    @property
    def name(self) -> str:
        return "ae30-v5"

    @property
    def profile(self) -> FamilyProfile:
        return self._profile

    def build_payload(self, *, image_bytes: bytes) -> bytes:
        return image_bytes


FAMILY_REGISTRY: dict[str, DeviceFamily[bytes]] = {
    "ae30-v5": Ae30V5Family(),
}
