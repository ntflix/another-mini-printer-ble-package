#!/usr/bin/env python3
"""
Comprehensive diagnostics scanner for MXW01 AE30 BLE printer.
Tries all combinations of subscriptions, reads, and command sequences.
Logs any response received with full context.
"""

from __future__ import annotations

import asyncio
import argparse
import sys
from dataclasses import dataclass
from typing import Optional

from bleak import BleakClient, BleakScanner


@dataclass
class TestResult:
    test_name: str
    notify_channels: list[str]
    read_channels: list[str]
    write_uuid: str
    command_hex: str
    response_received: bool
    response_data: Optional[str] = None
    response_source: Optional[str] = None


class DiagnosticScanner:
    def __init__(self, address: str, verbose: bool = True, legacy_probes: bool = False):
        self.address = address
        self.verbose = verbose
        self.legacy_probes = legacy_probes
        self.client: Optional[BleakClient] = None
        self.results: list[TestResult] = []
        self.response_event = asyncio.Event()
        self.last_response: Optional[tuple[str, bytes]] = None
        self.notification_queue: asyncio.Queue[tuple[int, bytes, str]] = asyncio.Queue()

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"[DIAG] {message}")

    async def connect(self) -> None:
        self.log(f"Connecting to {self.address}...")
        self.client = BleakClient(self.address, timeout=10.0)
        try:
            await self.client.connect()
            self.log("✓ Connected")
            self._log_gatt_map()
        except Exception as e:
            self.log(f"✗ Connection failed: {e}")
            raise

    def _log_gatt_map(self) -> None:
        if self.client is None or self.client.services is None:
            return
        self.log("=== GATT Map ===")
        for service in self.client.services:
            self.log(f"Service: {service.uuid}")
            for char in service.characteristics:
                props = ", ".join(char.properties)
                self.log(f"  {char.uuid} [{props}]")

    def _create_notification_callback(self, name: str) -> callable:
        def callback(_: object, data: bytearray) -> None:
            packet_hex = bytes(data).hex().lower()
            self.log(f"[NOTIFY] {name}: {packet_hex}")
            parsed = self._parse_mxw01_packet(bytes(data))
            if parsed is not None:
                cmd_id, payload, crc_received, footer = parsed
                crc_expected = self._mxw01_crc8(payload)
                if crc_received is not None and crc_received != crc_expected:
                    self.log(
                        f"[NOTIFY] CRC mismatch for 0x{cmd_id:02x}: got {crc_received:02x}, expected {crc_expected:02x}"
                    )
                if footer is not None and footer != 0xFF:
                    self.log(
                        f"[NOTIFY] Footer mismatch for 0x{cmd_id:02x}: got {footer:02x}, expected ff"
                    )
                self.log(
                    f"[NOTIFY_PARSED] cmd=0x{cmd_id:02x} payload_len={len(payload)} payload={payload.hex()}"
                )
                self.notification_queue.put_nowait((cmd_id, payload, packet_hex))
            self.last_response = (name, bytes(data))
            self.response_event.set()

        return callback

    async def subscribe_to_channel(self, uuid: str, name: str) -> bool:
        if self.client is None:
            return False
        try:
            callback = self._create_notification_callback(name)
            await self.client.start_notify(uuid, callback)
            self.log(f"✓ Subscribed to {name} ({uuid})")
            return True
        except Exception as e:
            self.log(f"✗ Failed to subscribe {name}: {e}")
            return False

    async def read_from_channel(self, uuid: str, name: str) -> Optional[str]:
        if self.client is None:
            return None
        try:
            data = await self.client.read_gatt_char(uuid)
            hex_data = bytes(data).hex().lower()
            self.log(f"[READ] {name}: {hex_data}")
            return hex_data
        except Exception as e:
            self.log(f"✗ Failed to read {name}: {e}")
            return None

    async def write_command(self, uuid: str, command_hex: str, name: str) -> bool:
        if self.client is None:
            return False
        try:
            command_bytes = bytes.fromhex(command_hex)
            await self.client.write_gatt_char(uuid, command_bytes, response=False)
            self.log(f"✓ Wrote to {name}: {command_hex}")
            return True
        except Exception as e:
            self.log(f"✗ Failed to write {name}: {e}")
            return False

    async def write_raw_bytes(self, uuid: str, payload: bytes, name: str) -> bool:
        if self.client is None:
            return False
        try:
            await self.client.write_gatt_char(uuid, payload, response=False)
            return True
        except Exception as e:
            self.log(f"✗ Failed raw write to {name}: {e}")
            return False

    def _parse_mxw01_packet(
        self, packet: bytes
    ) -> Optional[tuple[int, bytes, Optional[int], Optional[int]]]:
        if len(packet) < 6 or packet[0] != 0x22 or packet[1] != 0x21:
            return None
        cmd_id = packet[2]
        payload_len = int.from_bytes(packet[4:6], "little")
        payload_end = 6 + payload_len
        if len(packet) < payload_end:
            return None
        payload = packet[6:payload_end]
        crc_received = packet[payload_end] if len(packet) > payload_end else None
        footer = packet[payload_end + 1] if len(packet) > payload_end + 1 else None
        return cmd_id, payload, crc_received, footer

    def _mxw01_crc8(self, payload: bytes) -> int:
        # CRC-8 Dallas/Maxim style used by MXW01 control packets in AE01 flow.
        crc = 0
        for byte in payload:
            crc ^= byte
            for _ in range(8):
                if crc & 0x80:
                    crc = ((crc << 1) ^ 0x07) & 0xFF
                else:
                    crc = (crc << 1) & 0xFF
        return crc

    def _build_mxw01_control_packet(self, cmd_id: int, payload: bytes) -> str:
        data_len = len(payload)
        header = bytes(
            [
                0x22,
                0x21,
                cmd_id & 0xFF,
                0x00,
                data_len & 0xFF,
                (data_len >> 8) & 0xFF,
            ]
        )
        crc = self._mxw01_crc8(payload)
        packet = header + payload + bytes([crc, 0xFF])
        return packet.hex().lower()

    async def _wait_for_mxw01_notification(
        self, expected_cmd_id: int, timeout: float
    ) -> Optional[bytes]:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            try:
                cmd_id, payload, packet_hex = await asyncio.wait_for(
                    self.notification_queue.get(), timeout=remaining
                )
                if cmd_id == expected_cmd_id:
                    self.log(
                        f"✓ Matched MXW01 notify cmd 0x{cmd_id:02x}: payload={payload.hex()}"
                    )
                    return payload
                self.log(
                    f"[NOTIFY_PARSED] ignoring cmd 0x{cmd_id:02x} while waiting for 0x{expected_cmd_id:02x}"
                )
            except asyncio.TimeoutError:
                return None

    def _drain_notification_queue(self) -> None:
        while True:
            try:
                self.notification_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def run_mxw01_minimal_print_transaction(
        self,
        notify_uuid: str,
        control_uuid: str,
        data_uuid: str,
        intensity_cmd_hex: str,
        status_cmd_hex: str,
        print_cmd_hex: str,
        flush_cmd_hex: str,
    ) -> TestResult:
        test_name = "MXW01 minimal print transaction (A2->A1->A9->DATA->AD->AA)"
        self.log(f"\n{'='*60}")
        self.log(f"TEST: {test_name}")
        self.log(f"{'='*60}")

        self.last_response = None
        self.response_event.clear()
        self._drain_notification_queue()

        subscribed = []
        if await self.subscribe_to_channel(notify_uuid, "ae02 (main notify)"):
            subscribed.append("ae02 (main notify)")
        await asyncio.sleep(0.2)

        ok = True
        details: list[str] = []

        # A2 intensity
        ok &= await self.write_command(control_uuid, intensity_cmd_hex, "ae01")
        await asyncio.sleep(0.10)

        # A1 status
        ok &= await self.write_command(control_uuid, status_cmd_hex, "ae01")
        status_payload = await self._wait_for_mxw01_notification(0xA1, timeout=8.0)
        if status_payload is None:
            ok = False
            details.append("A1 timeout")
        else:
            details.append(f"A1:{status_payload.hex()}")
            if len(status_payload) >= 13:
                status_flag = status_payload[12]
                details.append(f"A1.flag={status_flag}")

        # A9 print request for 90 lines, mode 0.
        ok &= await self.write_command(control_uuid, print_cmd_hex, "ae01")
        print_payload = await self._wait_for_mxw01_notification(0xA9, timeout=8.0)
        if print_payload is None:
            ok = False
            details.append("A9 timeout")
        else:
            details.append(f"A9:{print_payload.hex()}")
            if len(print_payload) > 0 and print_payload[0] != 0x00:
                ok = False
                details.append("A9 rejected")

        # Send minimal data payload: 90 lines * 48 bytes = 4320 bytes.
        if ok:
            line = bytes([0x00] * 48)
            for i in range(90):
                if not await self.write_raw_bytes(data_uuid, line, "ae03"):
                    ok = False
                    details.append(f"AE03 write failed line {i+1}")
                    break
                if (i + 1) % 30 == 0:
                    self.log(f"Sent {i+1}/90 AE03 data lines")
                await asyncio.sleep(0.015)

        # AD flush + wait AA complete.
        aa_payload = None
        if ok:
            ok &= await self.write_command(control_uuid, flush_cmd_hex, "ae01")
            aa_payload = await self._wait_for_mxw01_notification(0xAA, timeout=25.0)
            if aa_payload is None:
                details.append("AA timeout")
            else:
                details.append(f"AA:{aa_payload.hex()}")

        if self.client is not None and self.client.services is not None:
            try:
                await self.client.stop_notify(notify_uuid)
            except Exception:
                pass

        response_received = (
            status_payload is not None
            or print_payload is not None
            or aa_payload is not None
        )
        result = TestResult(
            test_name=test_name,
            notify_channels=subscribed,
            read_channels=[],
            write_uuid=control_uuid,
            command_hex="MXW01_TXN",
            response_received=response_received,
            response_source="ae02",
            response_data="; ".join(details) if details else None,
        )
        self.results.append(result)
        if response_received:
            self.log("✓ Transaction produced protocol-level responses")
        else:
            self.log("✗ Transaction produced no protocol-level responses")
        return result

    async def run_test(
        self,
        test_name: str,
        notify_channels: list[tuple[str, str]],
        read_channels: list[tuple[str, str]],
        write_uuid: str,
        command_hex: str,
        write_char_name: str,
        hold_seconds: float = 2.0,
        notify_subscribe_delay: float = 0.0,
        post_write_delay: float = 0.0,
    ) -> TestResult:
        self.log(f"\n{'='*60}")
        self.log(f"TEST: {test_name}")
        self.log(f"{'='*60}")
        self.log(f"Notify channels: {notify_channels}")
        self.log(f"Read channels: {read_channels}")
        self.log(f"Write: {write_char_name} -> {command_hex}")

        # Reset response tracking
        self.last_response = None
        self.response_event.clear()

        # Subscribe to all notify channels
        subscribed: list[str] = []
        for idx, (uuid, name) in enumerate(notify_channels):
            if await self.subscribe_to_channel(uuid, name):
                subscribed.append(name)
            # Mirrors Java behavior where second notify channel is delayed.
            if notify_subscribe_delay > 0 and idx < len(notify_channels) - 1:
                await asyncio.sleep(notify_subscribe_delay)
        await asyncio.sleep(0.2)

        # Try reading from all readable channels first (baseline)
        baseline_reads: dict[str, str] = {}
        for uuid, name in read_channels:
            hex_val = await self.read_from_channel(uuid, name)
            if hex_val:
                baseline_reads[name] = hex_val
        await asyncio.sleep(0.1)

        # Send command
        if not await self.write_command(write_uuid, command_hex, write_char_name):
            result = TestResult(
                test_name=test_name,
                notify_channels=subscribed,
                read_channels=[name for _, name in read_channels],
                write_uuid=write_uuid,
                command_hex=command_hex,
                response_received=False,
            )
            self.results.append(result)
            return result

        if post_write_delay > 0:
            await asyncio.sleep(post_write_delay)

        # Poll read channels for changes
        response_received = False
        source = None
        response_hex = None

        self.log(f"Polling {hold_seconds}s for notify/read changes...")
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < hold_seconds:
            # Notify/indicate callback landed.
            if self.response_event.is_set() and self.last_response is not None:
                source, data = self.last_response
                response_received = True
                response_hex = data.hex().lower()
                self.log(f"✓ NOTIFY/INDICATE response from {source}: {response_hex}")
                break

            for uuid, name in read_channels:
                current = await self.read_from_channel(uuid, name)
                baseline = baseline_reads.get(name)
                if current is None:
                    continue
                if baseline is None:
                    baseline_reads[name] = current
                    continue
                if current != baseline:
                    response_received = True
                    source = name
                    response_hex = current
                    self.log(f"✓ READ CHANGED ({name}): {baseline} -> {current}")
                    break
            if response_received:
                break
            await asyncio.sleep(0.10)

        if not response_received:
            self.log(f"✗ No notify/read changes after {hold_seconds}s")

        # Unsubscribe
        if self.client is not None and self.client.services is not None:
            for uuid, name in notify_channels:
                try:
                    await self.client.stop_notify(uuid)
                except Exception:
                    pass

        result = TestResult(
            test_name=test_name,
            notify_channels=subscribed,
            read_channels=[name for _, name in read_channels],
            write_uuid=write_uuid,
            command_hex=command_hex,
            response_received=response_received,
            response_source=source,
            response_data=response_hex,
        )
        self.results.append(result)
        return result

    async def run_sequence_test(
        self,
        test_name: str,
        notify_channels: list[tuple[str, str]],
        read_channels: list[tuple[str, str]],
        write_uuid: str,
        write_char_name: str,
        command_sequence: list[tuple[str, float]],
        hold_seconds: float,
        notify_subscribe_delay: float = 0.0,
    ) -> TestResult:
        self.log(f"\n{'='*60}")
        self.log(f"TEST: {test_name}")
        self.log(f"{'='*60}")
        self.log(f"Notify channels: {notify_channels}")
        self.log(f"Read channels: {read_channels}")
        self.log(f"Write: {write_char_name} -> sequence({len(command_sequence)})")

        self.last_response = None
        self.response_event.clear()

        subscribed: list[str] = []
        for idx, (uuid, name) in enumerate(notify_channels):
            if await self.subscribe_to_channel(uuid, name):
                subscribed.append(name)
            if notify_subscribe_delay > 0 and idx < len(notify_channels) - 1:
                await asyncio.sleep(notify_subscribe_delay)
        await asyncio.sleep(0.2)

        baseline_reads: dict[str, str] = {}
        for uuid, name in read_channels:
            hex_val = await self.read_from_channel(uuid, name)
            if hex_val:
                baseline_reads[name] = hex_val

        for idx, (command_hex, sleep_after) in enumerate(command_sequence, start=1):
            if not await self.write_command(write_uuid, command_hex, write_char_name):
                break
            self.log(f"Sequence step {idx}/{len(command_sequence)} sent")
            if sleep_after > 0:
                await asyncio.sleep(sleep_after)

        response_received = False
        source: Optional[str] = None
        response_hex: Optional[str] = None

        self.log(f"Polling {hold_seconds}s for notify/read changes...")
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < hold_seconds:
            if self.response_event.is_set() and self.last_response is not None:
                source, data = self.last_response
                response_received = True
                response_hex = data.hex().lower()
                self.log(f"✓ NOTIFY/INDICATE response from {source}: {response_hex}")
                break

            for uuid, name in read_channels:
                current = await self.read_from_channel(uuid, name)
                baseline = baseline_reads.get(name)
                if current is None:
                    continue
                if baseline is None:
                    baseline_reads[name] = current
                    continue
                if current != baseline:
                    response_received = True
                    source = name
                    response_hex = current
                    self.log(f"✓ READ CHANGED ({name}): {baseline} -> {current}")
                    break
            if response_received:
                break
            await asyncio.sleep(0.10)

        if not response_received:
            self.log(f"✗ No notify/read changes after {hold_seconds}s")

        if self.client is not None and self.client.services is not None:
            for uuid, _ in notify_channels:
                try:
                    await self.client.stop_notify(uuid)
                except Exception:
                    pass

        result = TestResult(
            test_name=test_name,
            notify_channels=subscribed,
            read_channels=[name for _, name in read_channels],
            write_uuid=write_uuid,
            command_hex="SEQUENCE",
            response_received=response_received,
            response_source=source,
            response_data=response_hex,
        )
        self.results.append(result)
        return result

    async def run_all_tests(self) -> None:
        await self.connect()

        # Define all available characteristics
        notify_only = [
            ("0000ae02-0000-1000-8000-00805f9b34fb", "ae02 (main notify)"),
            ("0000ae04-0000-1000-8000-00805f9b34fb", "ae04 (data notify)"),
            (
                "0000ae05-0000-1000-8000-00805f9b34fb",
                "ae05 (indicate - may need handling)",
            ),
            ("0000ae3c-0000-1000-8000-00805f9b34fb", "ae3c (ae3a service notify)"),
        ]

        readable = [
            ("0000ae10-0000-1000-8000-00805f9b34fb", "ae10 (read/write)"),
            ("0000ae3c-0000-1000-8000-00805f9b34fb", "ae3c (ae3a service)"),
        ]

        commands = {
            "state": "5178a30001000000ff",
            "feed": "5178a10002003000f9ff",
            "quality": "5178a40001003399ff",
            "lattice_start": "5178a6000b00aa551738445f5f5f44382ca1ff",
            "lattice_end": "5178a6000b00aa55170000000000001711ff",
        }

        mxw01_commands = {
            "status_a1": self._build_mxw01_control_packet(0xA1, bytes([0x00])),
            "intensity_a2": self._build_mxw01_control_packet(0xA2, bytes([0x5D])),
            "print_a9_90": self._build_mxw01_control_packet(
                0xA9, bytes([0x5A, 0x00, 0x30, 0x00])
            ),
            "flush_ad": self._build_mxw01_control_packet(0xAD, bytes([0x00])),
        }

        # Default mode: run protocol-focused probes only.
        if not self.legacy_probes:
            await self.run_test(
                test_name="MXW01 A1 status on ae01 (expect ae02 notify)",
                notify_channels=[notify_only[0]],
                read_channels=[readable[0]],
                write_uuid="0000ae01-0000-1000-8000-00805f9b34fb",
                command_hex=mxw01_commands["status_a1"],
                write_char_name="ae01",
                hold_seconds=8.0,
                notify_subscribe_delay=0.0,
                post_write_delay=0.05,
            )

            await self.run_sequence_test(
                test_name="MXW01 control sequence on ae01 (A2->A1->A9)",
                notify_channels=[notify_only[0]],
                read_channels=[readable[0]],
                write_uuid="0000ae01-0000-1000-8000-00805f9b34fb",
                write_char_name="ae01",
                command_sequence=[
                    (mxw01_commands["intensity_a2"], 0.10),
                    (mxw01_commands["status_a1"], 0.10),
                    (mxw01_commands["print_a9_90"], 0.10),
                ],
                hold_seconds=8.0,
                notify_subscribe_delay=0.0,
            )

            await self.run_mxw01_minimal_print_transaction(
                notify_uuid="0000ae02-0000-1000-8000-00805f9b34fb",
                control_uuid="0000ae01-0000-1000-8000-00805f9b34fb",
                data_uuid="0000ae03-0000-1000-8000-00805f9b34fb",
                intensity_cmd_hex=mxw01_commands["intensity_a2"],
                status_cmd_hex=mxw01_commands["status_a1"],
                print_cmd_hex=mxw01_commands["print_a9_90"],
                flush_cmd_hex=mxw01_commands["flush_ad"],
            )

            if self.client is not None:
                await self.client.disconnect()
                self.log("\n✓ Disconnected")
            return

        # Test 1: State query to ae03, poll ae10
        await self.run_test(
            test_name="State query to ae03, poll ae10",
            notify_channels=[],
            read_channels=[readable[0]],  # ae10
            write_uuid="0000ae03-0000-1000-8000-00805f9b34fb",
            command_hex=commands["state"],
            write_char_name="ae03",
            hold_seconds=3.0,
        )

        # Test 2: State query to ae01 (command), poll ae10
        await self.run_test(
            test_name="State query to ae01 (command), poll ae10",
            notify_channels=[],
            read_channels=[readable[0]],  # ae10
            write_uuid="0000ae01-0000-1000-8000-00805f9b34fb",
            command_hex=commands["state"],
            write_char_name="ae01",
            hold_seconds=3.0,
        )

        # Test 3: Feed command to ae03, poll ae10
        await self.run_test(
            test_name="Feed command to ae03, poll ae10",
            notify_channels=[],
            read_channels=[readable[0]],  # ae10
            write_uuid="0000ae03-0000-1000-8000-00805f9b34fb",
            command_hex=commands["feed"],
            write_char_name="ae03",
            hold_seconds=3.0,
        )

        # Test 4: Feed command to ae01, poll ae10
        await self.run_test(
            test_name="Feed command to ae01, poll ae10",
            notify_channels=[],
            read_channels=[readable[0]],  # ae10
            write_uuid="0000ae01-0000-1000-8000-00805f9b34fb",
            command_hex=commands["feed"],
            write_char_name="ae01",
            hold_seconds=3.0,
        )

        # Test 5: Quality command to ae03, poll ae10
        await self.run_test(
            test_name="Quality command to ae03, poll ae10",
            notify_channels=[],
            read_channels=[readable[0]],  # ae10
            write_uuid="0000ae03-0000-1000-8000-00805f9b34fb",
            command_hex=commands["quality"],
            write_char_name="ae03",
            hold_seconds=3.0,
        )

        # Test 6: Java-like notify opening order and delays, then state on command char.
        await self.run_test(
            test_name="AE30 delayed notify setup + state on ae01",
            notify_channels=[notify_only[0], notify_only[1]],  # ae02 then ae04
            read_channels=[readable[0]],
            write_uuid="0000ae01-0000-1000-8000-00805f9b34fb",
            command_hex=commands["state"],
            write_char_name="ae01",
            hold_seconds=5.0,
            notify_subscribe_delay=0.30,
            post_write_delay=0.05,
        )

        # Test 7: Same setup but write state via data char.
        await self.run_test(
            test_name="AE30 delayed notify setup + state on ae03",
            notify_channels=[notify_only[0], notify_only[1]],
            read_channels=[readable[0]],
            write_uuid="0000ae03-0000-1000-8000-00805f9b34fb",
            command_hex=commands["state"],
            write_char_name="ae03",
            hold_seconds=5.0,
            notify_subscribe_delay=0.30,
            post_write_delay=0.05,
        )

        # Test 8: Include indicate channel and broad watch.
        await self.run_test(
            test_name="AE30 full subscribe (ae02/ae04/ae05/ae3c) + state",
            notify_channels=notify_only,
            read_channels=[readable[0]],
            write_uuid="0000ae01-0000-1000-8000-00805f9b34fb",
            command_hex=commands["state"],
            write_char_name="ae01",
            hold_seconds=6.0,
            notify_subscribe_delay=0.30,
            post_write_delay=0.05,
        )

        # Test 9: Lattice start command can wake print pipeline on some V5 boards.
        await self.run_test(
            test_name="Lattice start on ae03 with delayed notify setup",
            notify_channels=[notify_only[0], notify_only[1]],
            read_channels=[readable[0]],
            write_uuid="0000ae03-0000-1000-8000-00805f9b34fb",
            command_hex=commands["lattice_start"],
            write_char_name="ae03",
            hold_seconds=5.0,
            notify_subscribe_delay=0.30,
            post_write_delay=0.05,
        )

        # Test 10: Lattice end command.
        await self.run_test(
            test_name="Lattice end on ae03 with delayed notify setup",
            notify_channels=[notify_only[0], notify_only[1]],
            read_channels=[readable[0]],
            write_uuid="0000ae03-0000-1000-8000-00805f9b34fb",
            command_hex=commands["lattice_end"],
            write_char_name="ae03",
            hold_seconds=5.0,
            notify_subscribe_delay=0.30,
            post_write_delay=0.05,
        )

        # Test 11: Java-like mini sequence under delayed notify setup.
        await self.run_sequence_test(
            test_name="Mini V5 sequence on ae03 (quality->lattice->feed->end->state)",
            notify_channels=[notify_only[0], notify_only[1]],
            read_channels=[readable[0]],
            write_uuid="0000ae03-0000-1000-8000-00805f9b34fb",
            write_char_name="ae03",
            command_sequence=[
                (commands["quality"], 0.08),
                (commands["lattice_start"], 0.08),
                (commands["feed"], 0.12),
                (commands["lattice_end"], 0.08),
                (commands["state"], 0.0),
            ],
            hold_seconds=6.0,
            notify_subscribe_delay=0.30,
        )

        # Test 12: MXW01 protocol A1 status request (2221 framing on AE01).
        await self.run_test(
            test_name="MXW01 A1 status on ae01 (expect ae02 notify)",
            notify_channels=[notify_only[0]],  # ae02 main notify only
            read_channels=[readable[0]],
            write_uuid="0000ae01-0000-1000-8000-00805f9b34fb",
            command_hex=mxw01_commands["status_a1"],
            write_char_name="ae01",
            hold_seconds=8.0,
            notify_subscribe_delay=0.0,
            post_write_delay=0.05,
        )

        # Test 13: MXW01 sequence A2 -> A1 -> A9 using control channel.
        await self.run_sequence_test(
            test_name="MXW01 control sequence on ae01 (A2->A1->A9)",
            notify_channels=[notify_only[0]],
            read_channels=[readable[0]],
            write_uuid="0000ae01-0000-1000-8000-00805f9b34fb",
            write_char_name="ae01",
            command_sequence=[
                (mxw01_commands["intensity_a2"], 0.10),
                (mxw01_commands["status_a1"], 0.10),
                (mxw01_commands["print_a9_90"], 0.10),
            ],
            hold_seconds=8.0,
            notify_subscribe_delay=0.0,
        )

        # Test 14: Flush command probe (AD) on control channel.
        await self.run_test(
            test_name="MXW01 AD flush on ae01 (expect ae02/aa)",
            notify_channels=[notify_only[0]],
            read_channels=[readable[0]],
            write_uuid="0000ae01-0000-1000-8000-00805f9b34fb",
            command_hex=mxw01_commands["flush_ad"],
            write_char_name="ae01",
            hold_seconds=8.0,
            notify_subscribe_delay=0.0,
            post_write_delay=0.05,
        )

        # Test 15: State to ae3b (ae3a service), observe ae3c notify path.
        await self.run_test(
            test_name="State query to ae3b, poll ae3c",
            notify_channels=[notify_only[3]],
            read_channels=[readable[1]],  # ae3c
            write_uuid="0000ae3b-0000-1000-8000-00805f9b34fb",
            command_hex=commands["state"],
            write_char_name="ae3b",
            hold_seconds=3.0,
        )

        # Test 16: State to ae03, observe ae10 + ae3c with notify active.
        await self.run_test(
            test_name="State query to ae03, poll ae10 and ae3c",
            notify_channels=[notify_only[3]],
            read_channels=readable,  # Both ae10 and ae3c
            write_uuid="0000ae03-0000-1000-8000-00805f9b34fb",
            command_hex=commands["state"],
            write_char_name="ae03",
            hold_seconds=3.0,
        )

        # Disconnect
        if self.client is not None:
            await self.client.disconnect()
            self.log("\n✓ Disconnected")

    def print_summary(self) -> None:
        self.log(f"\n{'='*60}")
        self.log("SUMMARY")
        self.log(f"{'='*60}")

        responses_received = [r for r in self.results if r.response_received]

        if responses_received:
            self.log(f"\n✓ {len(responses_received)} test(s) received responses:\n")
            for result in responses_received:
                self.log(f"  ✓ {result.test_name}")
                self.log(f"    Notifies: {result.notify_channels}")
                self.log(f"    Reads: {result.read_channels}")
                self.log(f"    Write: {result.write_uuid}")
                self.log(f"    Command: {result.command_hex}")
                self.log(f"    Response source: {result.response_source}")
                self.log(f"    Response data: {result.response_data}\n")
        else:
            self.log("\n✗ No responses received on any test")
            self.log(f"  Ran {len(self.results)} tests with no results")


async def main() -> None:
    parser = argparse.ArgumentParser(description="MXW01 AE30 Diagnostic Scanner")
    parser.add_argument("--address", type=str, required=True, help="BLE address")
    parser.add_argument(
        "--verbose", action="store_true", default=True, help="Verbose logging"
    )
    parser.add_argument(
        "--legacy-probes",
        action="store_true",
        help="Also run older non-MXW01 probe matrix (slower).",
    )

    args = parser.parse_args()

    scanner = DiagnosticScanner(
        address=args.address,
        verbose=args.verbose,
        legacy_probes=args.legacy_probes,
    )
    await scanner.run_all_tests()
    scanner.print_summary()


if __name__ == "__main__":
    asyncio.run(main())
