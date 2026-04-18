from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Generic, Literal, TypeVar

from PIL import Image

from .families import Ae30V5Family, DeviceFamily
from .protocol import (
    build_mxw01_data_lines,
    build_mxw01_flush_command,
    build_mxw01_intensity_command,
    build_mxw01_print_command,
    build_mxw01_status_command,
)
from .transport import BleTransport
from .types import ConnectOptions, PrintOptions

TFamily = TypeVar("TFamily", bound=DeviceFamily[bytes])
WireMode = Literal["v5"]


class PrinterDevice(ABC, Generic[TFamily]):
    def __init__(self, family: TFamily, transport: BleTransport) -> None:
        self.family = family
        self.transport = transport

    async def connect(self, *, address: str | None, options: ConnectOptions) -> None:
        await self.transport.connect(
            address=address,
            timeout_seconds=options.timeout_seconds,
            target_name=options.target_name,
            scan_timeout_seconds=options.scan_timeout_seconds,
        )
        if options.post_connect_delay_seconds > 0:
            await asyncio.sleep(options.post_connect_delay_seconds)
        # Add 200ms delay before subscribing to notifications (Java: delayOpenNotification)
        await asyncio.sleep(0.200)
        await self.transport.start_notifications(
            notify_uuid=self.family.profile.notify_uuid,
            data_notify_uuid=self.family.profile.data_notify_uuid,
            flow_mode=self.family.profile.flow_control,
        )

    async def disconnect(self) -> None:
        await self.transport.disconnect()

    async def print_image(
        self,
        *,
        image: Image.Image,
        print_options: PrintOptions,
        mode: WireMode,
        is_command: bool,
        write_uuid_override: str | None = None,
    ) -> None:
        payload = self._encode_payload(
            image=image, print_options=print_options, mode=mode
        )
        write_uuid = (
            write_uuid_override
            if write_uuid_override is not None
            else self.family.choose_write_uuid(is_command=is_command)
        )
        await self.transport.write_stream(
            write_uuid=write_uuid,
            payload=payload,
            chunk_size=print_options.package_length,
            interval_ms=print_options.write_interval_ms,
        )

    async def print_raw(
        self,
        *,
        payload: bytes,
        print_options: PrintOptions,
        is_command: bool,
        write_uuid_override: str | None = None,
    ) -> None:
        write_uuid = (
            write_uuid_override
            if write_uuid_override is not None
            else self.family.choose_write_uuid(is_command=is_command)
        )
        await self.transport.write_stream(
            write_uuid=write_uuid,
            payload=payload,
            chunk_size=print_options.package_length,
            interval_ms=print_options.write_interval_ms,
        )

    @abstractmethod
    def _encode_payload(
        self, *, image: Image.Image, print_options: PrintOptions, mode: WireMode
    ) -> bytes:
        raise NotImplementedError


class Ae30V5Device(PrinterDevice[Ae30V5Family]):
    def __init__(self, transport: BleTransport) -> None:
        super().__init__(family=Ae30V5Family(), transport=transport)

    async def print_image(
        self,
        *,
        image: Image.Image,
        print_options: PrintOptions,
        mode: WireMode,
        is_command: bool,
        write_uuid_override: str | None = None,
    ) -> None:
        control_uuid = (
            self.family.profile.command_write_uuid
            if self.family.profile.command_write_uuid is not None
            else self.family.profile.write_uuid
        )
        data_uuid = (
            write_uuid_override
            if write_uuid_override is not None
            else self.family.profile.write_uuid
        )

        rows = build_mxw01_data_lines(
            image,
            target_width=image.width,
            minimum_lines=90,
        )

        self.transport.drain_mxw01_notifications()
        await self.transport.write_packet(
            write_uuid=control_uuid,
            payload=build_mxw01_intensity_command(print_options.intensity),
        )
        await asyncio.sleep(0.10)

        await self.transport.write_packet(
            write_uuid=control_uuid,
            payload=build_mxw01_status_command(),
        )
        status_payload = await self.transport.wait_for_mxw01_notification(
            command_id=0xA1,
            timeout_seconds=8.0,
        )
        if status_payload is None:
            raise RuntimeError("Timed out waiting for MXW01 A1 status notification")

        await self.transport.write_packet(
            write_uuid=control_uuid,
            payload=build_mxw01_print_command(line_count=len(rows)),
        )
        print_ack = await self.transport.wait_for_mxw01_notification(
            command_id=0xA9,
            timeout_seconds=8.0,
        )
        if print_ack is None:
            raise RuntimeError("Timed out waiting for MXW01 A9 print acknowledgement")
        if print_ack and print_ack[0] != 0x00:
            raise RuntimeError(
                f"MXW01 printer rejected job with A9 status: {print_ack.hex()}"
            )

        for row in rows:
            await self.transport.write_packet(write_uuid=data_uuid, payload=row)
            if print_options.write_interval_ms > 0:
                await asyncio.sleep(print_options.write_interval_ms / 1000.0)

        await self.transport.write_packet(
            write_uuid=control_uuid,
            payload=build_mxw01_flush_command(),
        )
        complete = await self.transport.wait_for_mxw01_notification(
            command_id=0xAA,
            timeout_seconds=25.0,
        )
        if complete is None:
            raise RuntimeError("Timed out waiting for MXW01 AA completion notification")

    def _encode_payload(
        self, *, image: Image.Image, print_options: PrintOptions, mode: WireMode
    ) -> bytes:
        raise NotImplementedError("AE30 uses custom MXW01 transaction path")


def create_device(transport: BleTransport) -> Ae30V5Device:
    return Ae30V5Device(transport)
