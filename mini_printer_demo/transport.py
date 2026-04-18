from __future__ import annotations

import asyncio
import platform
import re
from dataclasses import dataclass
from typing import Final

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakDeviceNotFoundError

from .families import FlowControlMode
from .protocol import mxw01_crc8, parse_mxw01_packet

PAUSE_SIGNATURES: Final[set[str]] = {
    "aa01",
    "5178ae0101001070ff",
    "2221a800010020e0ff",
    "2221ae0101001070ff",
    "2221ae0001000000",
    "5688a70101000107ff",
}

RESUME_SIGNATURES: Final[set[str]] = {
    "aa00",
    "5178ae0101000000ff",
    "2221a80001003090ff",
    "2221ae0101000000ff",
    "2221ae0001001000",
    "5688a70101000000ff",
}


@dataclass(frozen=True)
class ScanResult:
    address: str
    name: str | None
    rssi: int | None


class BleTransport:
    def __init__(self) -> None:
        self._client: BleakClient | None = None
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._credit_event = asyncio.Event()
        self._credit_event.set()
        self._credit = 10
        self._lock = asyncio.Lock()
        self._flow_mode = FlowControlMode.NONE
        self._latest_status: str | None = None
        self._verbose = False
        self._mxw01_notifications: asyncio.Queue[tuple[int, bytes, str]] = (
            asyncio.Queue()
        )

    def set_verbose(self, value: bool) -> None:
        self._verbose = value

    def _log(self, message: str) -> None:
        if self._verbose:
            print(f"[ble] {message}")

    @property
    def latest_status(self) -> str | None:
        return self._latest_status

    @staticmethod
    async def scan(timeout_seconds: float = 8.0) -> list[ScanResult]:
        devices = await BleakScanner.discover(timeout=timeout_seconds)
        return [
            ScanResult(address=d.address, name=d.name, rssi=getattr(d, "rssi", None))
            for d in devices
        ]

    async def connect(
        self,
        *,
        address: str | None,
        timeout_seconds: float,
        target_name: str | None,
        scan_timeout_seconds: float,
    ) -> None:
        if self._client is not None and self._client.is_connected:
            self._log("already connected; skipping reconnect")
            return

        resolved_address = await self._resolve_target_address(
            address=address,
            target_name=target_name,
            scan_timeout_seconds=scan_timeout_seconds,
        )
        self._log(f"resolved target address: {resolved_address}")
        self._client = BleakClient(resolved_address, timeout=timeout_seconds)
        try:
            await self._client.connect()
            self._log("connection established")
            self._log_gatt_map()
        except BleakDeviceNotFoundError as exc:
            guidance = (
                "Device not found by CoreBluetooth. On macOS, use --scan first and pass the scanned "
                "CoreBluetooth UUID as --address, or pass --name for scan-based resolution."
            )
            raise RuntimeError(guidance) from exc

    def _log_gatt_map(self) -> None:
        if self._client is None or not self._verbose:
            return
        services = self._client.services
        if services is None:
            self._log("gatt services unavailable")
            return
        for service in services:
            self._log(f"service: {service.uuid}")
            for characteristic in service.characteristics:
                props = ",".join(characteristic.properties)
                self._log(f"  char: {characteristic.uuid} props=[{props}]")

    def _has_characteristic(self, uuid: str) -> bool:
        if self._client is None:
            return False
        services = self._client.services
        if services is None:
            return False
        return services.get_characteristic(uuid) is not None

    def _iter_characteristics(self) -> list[tuple[str, tuple[str, ...]]]:
        if self._client is None:
            return []
        services = self._client.services
        if services is None:
            return []
        out: list[tuple[str, tuple[str, ...]]] = []
        for service in services:
            for characteristic in service.characteristics:
                out.append((characteristic.uuid, tuple(characteristic.properties)))
        return out

    def _characteristic_properties(self, uuid: str) -> tuple[str, ...]:
        for current_uuid, props in self._iter_characteristics():
            if current_uuid.lower() == uuid.lower():
                return props
        return ()

    def _resolve_notify_uuid(
        self, preferred_uuid: str, *, exclude: tuple[str, ...] = ()
    ) -> str:
        excluded = {value.lower() for value in exclude}
        if (
            self._has_characteristic(preferred_uuid)
            and preferred_uuid.lower() not in excluded
        ):
            return preferred_uuid

        for uuid, props in self._iter_characteristics():
            if uuid.lower() in excluded:
                continue
            if "notify" in props or "indicate" in props:
                self._log(
                    f"notify characteristic {preferred_uuid} not found; falling back to {uuid}"
                )
                return uuid

        raise RuntimeError(
            f"Notify characteristic {preferred_uuid} not found and no fallback notify/indicate characteristic is available"
        )

    def _resolve_write_uuid(self, preferred_uuid: str) -> str:
        if self._has_characteristic(preferred_uuid):
            return preferred_uuid

        fallback_write: str | None = None
        for uuid, props in self._iter_characteristics():
            if "write-without-response" in props:
                self._log(
                    f"write characteristic {preferred_uuid} not found; falling back to {uuid} (write-without-response)"
                )
                return uuid
            if "write" in props and fallback_write is None:
                fallback_write = uuid

        if fallback_write is not None:
            self._log(
                f"write characteristic {preferred_uuid} not found; falling back to {fallback_write} (write)"
            )
            return fallback_write

        raise RuntimeError(
            f"Write characteristic {preferred_uuid} not found and no fallback write characteristic is available"
        )

    async def _resolve_target_address(
        self,
        *,
        address: str | None,
        target_name: str | None,
        scan_timeout_seconds: float,
    ) -> str:
        if address is None and target_name is None:
            raise ValueError("Either address or target_name must be provided")

        discovered = await BleakScanner.discover(timeout=scan_timeout_seconds)
        self._log(f"scan discovered {len(discovered)} devices")
        if not discovered:
            raise RuntimeError(
                "No BLE devices discovered. Ensure the printer is powered and advertising."
            )

        normalized_address = address.lower() if address is not None else None
        normalized_name = target_name.lower() if target_name is not None else None

        for device in discovered:
            if (
                normalized_address is not None
                and device.address.lower() == normalized_address
            ):
                return device.address

        for device in discovered:
            name = device.name
            if (
                normalized_name is not None
                and name is not None
                and name.lower() == normalized_name
            ):
                return device.address

        if platform.system() == "Darwin" and normalized_address is not None:
            is_mac_like = (
                re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", normalized_address)
                is not None
            )
            if is_mac_like:
                preview = ", ".join(
                    f"{d.address} ({d.name or 'unnamed'})" for d in discovered[:10]
                )
                raise RuntimeError(
                    "Provided MAC address was not found in CoreBluetooth scan results. "
                    "macOS usually exposes BLE devices as UUID identifiers. "
                    f"Use one of scanned addresses instead: {preview}"
                )

        if normalized_address is not None:
            self._log("falling back to provided address as-is")
            return address

        preview = ", ".join(
            f"{d.address} ({d.name or 'unnamed'})" for d in discovered[:10]
        )
        raise RuntimeError(
            f"Target device not found by name. Discovered devices: {preview}"
        )

    async def disconnect(self) -> None:
        if self._client is None:
            return
        if self._client.is_connected:
            self._log("disconnecting")
            await self._client.disconnect()
        self._client = None

    async def start_notifications(
        self,
        *,
        notify_uuid: str,
        data_notify_uuid: str | None,
        flow_mode: FlowControlMode,
    ) -> None:
        if self._client is None:
            raise RuntimeError("BLE client is not connected")
        self._flow_mode = flow_mode
        resolved_notify_uuid = self._resolve_notify_uuid(notify_uuid)
        self._log(f"subscribing notify: {resolved_notify_uuid}")
        await self._client.start_notify(resolved_notify_uuid, self._on_main_notify)
        self._log(f"notify subscription confirmed for {resolved_notify_uuid}")
        if data_notify_uuid is not None:
            # Add 300ms delay before subscribing secondary notify (Java: Handler().postDelayed(300L))
            await asyncio.sleep(0.300)
            resolved_data_notify_uuid = self._resolve_notify_uuid(
                data_notify_uuid, exclude=(resolved_notify_uuid,)
            )
            self._log(f"subscribing data notify: {resolved_data_notify_uuid}")
            await self._client.start_notify(
                resolved_data_notify_uuid, self._on_data_notify
            )
            self._log(
                f"data notify subscription confirmed for {resolved_data_notify_uuid}"
            )

    async def write_stream(
        self,
        *,
        write_uuid: str,
        payload: bytes,
        chunk_size: int,
        interval_ms: int,
    ) -> None:
        if self._client is None:
            raise RuntimeError("BLE client is not connected")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be > 0")
        if interval_ms < 0:
            raise ValueError("interval_ms must be >= 0")

        resolved_write_uuid = self._resolve_write_uuid(write_uuid)
        write_props = self._characteristic_properties(resolved_write_uuid)
        write_with_response = "write-without-response" not in write_props
        self._log(
            "write mode: "
            f"uuid={resolved_write_uuid} response={write_with_response} props={list(write_props)}"
        )

        async with self._lock:
            total_chunks = (len(payload) + chunk_size - 1) // chunk_size
            self._log(
                f"write begin: uuid={resolved_write_uuid} payload={len(payload)} bytes chunks={total_chunks} chunk_size={chunk_size} interval_ms={interval_ms}"
            )
            for offset in range(0, len(payload), chunk_size):
                chunk = payload[offset : offset + chunk_size]
                index = (offset // chunk_size) + 1
                await self._wait_for_flow_control()
                await self._client.write_gatt_char(
                    resolved_write_uuid, chunk, response=write_with_response
                )
                preview = chunk[:12].hex()
                self._log(
                    f"chunk {index}/{total_chunks} wrote {len(chunk)} bytes head={preview}"
                )
                if self._flow_mode is FlowControlMode.CREDIT_BASED:
                    self._credit = max(0, self._credit - 1)
                    if self._credit <= 0:
                        self._credit_event.clear()
                        self._log("credit exhausted; waiting for replenishment")
                if interval_ms:
                    await asyncio.sleep(interval_ms / 1000.0)
            self._log("write complete")

    async def write_packet(self, *, write_uuid: str, payload: bytes) -> None:
        if self._client is None:
            raise RuntimeError("BLE client is not connected")

        resolved_write_uuid = self._resolve_write_uuid(write_uuid)
        write_props = self._characteristic_properties(resolved_write_uuid)
        write_with_response = "write-without-response" not in write_props

        async with self._lock:
            await self._wait_for_flow_control()
            await self._client.write_gatt_char(
                resolved_write_uuid, payload, response=write_with_response
            )
            self._log(
                f"packet wrote {len(payload)} bytes to {resolved_write_uuid} head={payload[:12].hex()}"
            )

    def drain_mxw01_notifications(self) -> None:
        while True:
            try:
                self._mxw01_notifications.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def wait_for_mxw01_notification(
        self, *, command_id: int, timeout_seconds: float
    ) -> bytes | None:
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            try:
                cmd_id, payload, _packet_hex = await asyncio.wait_for(
                    self._mxw01_notifications.get(), timeout=remaining
                )
            except asyncio.TimeoutError:
                return None

            if cmd_id == command_id:
                self._log(
                    f"matched MXW01 notify cmd=0x{cmd_id:02x} payload={payload.hex()}"
                )
                return payload
            self._log(
                f"ignoring MXW01 notify cmd=0x{cmd_id:02x} while waiting for 0x{command_id:02x}"
            )

    async def _wait_for_flow_control(self) -> None:
        if self._flow_mode is FlowControlMode.NOTIFY_PAUSE_RESUME:
            await self._pause_event.wait()
            return
        if self._flow_mode is FlowControlMode.CREDIT_BASED:
            while self._credit <= 0:
                await self._credit_event.wait()
            return

    def _on_main_notify(self, _: object, data: bytearray) -> None:
        packet_hex = bytes(data).hex().lower()
        self._log(f"notify main: {packet_hex}")

        parsed = parse_mxw01_packet(bytes(data))
        if parsed is not None:
            cmd_id, payload, crc_received, footer = parsed
            crc_expected = mxw01_crc8(payload)
            if crc_received is not None and crc_received != crc_expected:
                self._log(
                    f"mxw01 crc mismatch cmd=0x{cmd_id:02x}: got {crc_received:02x} expected {crc_expected:02x}"
                )
            if footer is not None and footer != 0xFF:
                self._log(
                    f"mxw01 footer mismatch cmd=0x{cmd_id:02x}: got {footer:02x} expected ff"
                )
            self._mxw01_notifications.put_nowait((cmd_id, payload, packet_hex))

        if packet_hex in PAUSE_SIGNATURES:
            self._pause_event.clear()
            self._log("flow paused by notify")
            return
        if packet_hex in RESUME_SIGNATURES:
            self._pause_event.set()
            self._log("flow resumed by notify")
            return

        if packet_hex.startswith("6572723a") and len(packet_hex) == 16:
            status_bits_a = int(packet_hex[10:12], 16)
            status_bits_b = int(packet_hex[12:14], 16)
            overheat = (status_bits_a & 0b0100_0000) != 0
            no_paper = (status_bits_b & 0b0000_0011) == 0b0000_0011
            if overheat:
                self._latest_status = "overheat"
                self._log("status: overheat")
            elif no_paper:
                self._latest_status = "no-paper"
                self._log("status: no-paper")

    def _on_data_notify(self, _: object, data: bytearray) -> None:
        if not data:
            return
        tag = data[0]
        self._log(f"notify data: {bytes(data).hex().lower()}")
        if tag == 0x01 and len(data) >= 2:
            self._credit = int(data[1])
            if self._credit > 0:
                self._credit_event.set()
            self._log(f"credit updated: {self._credit}")
            return
        if tag == 0x02 and len(data) >= 3:
            mtu_reported = int.from_bytes(
                bytes(data[1:3]), byteorder="little", signed=False
            )
            self._latest_status = f"mtu-reported:{mtu_reported}"
            self._log(f"mtu reported by device: {mtu_reported}")
