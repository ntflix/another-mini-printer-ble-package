from __future__ import annotations

import argparse
import asyncio
from typing import Protocol, cast

from PIL import Image

from .devices import WireMode, create_device
from .protocol import (
    DitherAlgorithm,
    ProbeCommand,
    apply_dithering,
    build_mxw01_intensity_command,
    build_probe_payload,
    build_test_image,
)
from .transport import BleTransport
from .types import ConnectOptions, ImageOptions, PrintOptions

PRINTER_DOT_WIDTH = 384


class CliArgs(Protocol):
    scan: bool
    address: str | None
    name: str | None
    scan_timeout: float
    connect_settle_ms: int
    hold_seconds: float
    post_send_hold_seconds: float
    mode: str
    text: str
    image: str | None
    dither: str
    halftone_cell_size: int
    speed: int
    intensity: int
    quality: int
    energy: int
    package_length: int
    interval_ms: int
    timeout: float
    write_uuid: str | None
    verbose: bool
    probe: str | None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="End-to-end BLE mini-printer demo")
    _ = parser.add_argument(
        "--scan", action="store_true", help="Scan nearby BLE devices and exit"
    )
    _ = parser.add_argument("--address", type=str, help="Target BLE address/UUID")
    _ = parser.add_argument(
        "--name", type=str, help="Target BLE name for scan-based resolution"
    )
    _ = parser.add_argument(
        "--scan-timeout", type=float, default=8.0, help="BLE scan timeout in seconds"
    )
    _ = parser.add_argument(
        "--connect-settle-ms",
        type=int,
        default=300,
        help="Delay after connect before subscribing to notifications",
    )
    _ = parser.add_argument(
        "--hold-seconds",
        type=float,
        default=0.0,
        help="Alias for post-send hold; keeps the BLE connection open after sending data",
    )
    _ = parser.add_argument(
        "--post-send-hold-seconds",
        type=float,
        default=0.0,
        help="Keep the BLE connection open after reporting success",
    )
    _ = parser.add_argument(
        "--mode", type=str, default="v5", choices=["v5"], help="Wire mode"
    )
    _ = parser.add_argument("--text", type=str, default="Hello mini printer")
    _ = parser.add_argument(
        "--image",
        type=str,
        help="Optional image file path to print instead of generated test image",
    )
    _ = parser.add_argument(
        "--dither",
        type=str,
        default="none",
        choices=["none", "floyd-steinberg", "halftone"],
        help="Dithering algorithm used before 1-bit rasterization",
    )
    _ = parser.add_argument(
        "--halftone-cell-size",
        type=int,
        default=4,
        help="Cell size for halftone dithering (minimum 2)",
    )
    _ = parser.add_argument("--speed", type=int, default=25)
    _ = parser.add_argument(
        "--intensity",
        type=lambda value: int(value, 0),
        default=0x5D,
        help="MXW01 A2 intensity value in range 0x00-0xFF (higher is darker)",
    )
    _ = parser.add_argument("--quality", type=int, default=3)
    _ = parser.add_argument("--energy", type=int, default=12000)
    _ = parser.add_argument("--package-length", type=int, default=20)
    _ = parser.add_argument("--interval-ms", type=int, default=20)
    _ = parser.add_argument("--timeout", type=float, default=20.0)
    _ = parser.add_argument(
        "--write-uuid",
        type=str,
        help="Force write characteristic UUID (e.g. 0000ae01-0000-1000-8000-00805f9b34fb)",
    )
    _ = parser.add_argument(
        "--verbose", action="store_true", help="Enable verbose BLE logging"
    )
    _ = parser.add_argument(
        "--probe",
        type=str,
        choices=["status", "intensity", "flush"],
        help="Send a single MXW01 control packet on AE01 instead of a full print",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> CliArgs:
    parsed = build_parser().parse_args(argv)
    return cast(CliArgs, cast(object, parsed))


async def run_cli(args: CliArgs) -> None:
    transport = BleTransport()
    transport.set_verbose(bool(args.verbose))

    if args.scan:
        devices = await BleTransport.scan(timeout_seconds=args.scan_timeout)
        for device in devices:
            print(f"{device.address:>20}  name={device.name!r}  rssi={device.rssi}")
        return

    if args.address is None and args.name is None:
        raise SystemExit("Either --address or --name is required unless --scan is used")

    if args.intensity < 0x00 or args.intensity > 0xFF:
        raise SystemExit("--intensity must be in range 0x00-0xFF")

    mode = cast(WireMode, args.mode)
    dither_algorithm = DitherAlgorithm(args.dither)
    image_options = ImageOptions(width=PRINTER_DOT_WIDTH, text=args.text)
    print_options = PrintOptions(
        intensity=args.intensity,
        speed=args.speed,
        quality=args.quality,
        energy=args.energy,
        package_length=args.package_length,
        write_interval_ms=args.interval_ms,
    )
    connect_options = ConnectOptions(
        timeout_seconds=args.timeout,
        target_name=args.name,
        scan_timeout_seconds=args.scan_timeout,
        post_connect_delay_seconds=max(0.0, args.connect_settle_ms / 1000.0),
    )

    device = create_device(transport)

    target_display = args.address if args.address is not None else args.name
    print(f"Connecting to {target_display} using family=ae30-v5 mode={mode}")
    await device.connect(address=args.address, options=connect_options)
    try:
        effective_post_send_hold_seconds = max(
            0.0,
            (
                args.post_send_hold_seconds
                if args.post_send_hold_seconds > 0
                else args.hold_seconds
            ),
        )
        if args.probe is not None:
            probe_command = ProbeCommand(args.probe)
            if probe_command is ProbeCommand.INTENSITY:
                payload = build_mxw01_intensity_command(args.intensity)
            else:
                payload = build_probe_payload(probe_command)
            print(
                f"Sending probe={probe_command.value} payload_len={len(payload)} bytes is_command=True"
            )
            await device.print_raw(
                payload=payload,
                print_options=print_options,
                is_command=True,
                write_uuid_override=args.write_uuid,
            )
        else:
            if args.image is not None:
                with Image.open(args.image) as source_image:
                    image = source_image.convert("RGB")
                if image.width <= 0 or image.height <= 0:
                    raise SystemExit("Input image has invalid dimensions")
                scaled_height = max(
                    1, round(image.height * PRINTER_DOT_WIDTH / image.width)
                )
                image = image.resize(  # pyright: ignore[reportUnknownMemberType]
                    (PRINTER_DOT_WIDTH, scaled_height),
                    Image.Resampling.LANCZOS,
                )
            else:
                image = build_test_image(image_options)

            image = apply_dithering(
                image,
                algorithm=dither_algorithm,
                halftone_cell_size=args.halftone_cell_size,
            )
            print(
                "Sending image print payload "
                + f"width={image.width} height={image.height} "
                + f"package_length={print_options.package_length} "
                + f"dither={dither_algorithm.value} intensity=0x{print_options.intensity:02x}"
            )
            await device.print_image(
                image=image,
                print_options=print_options,
                mode=mode,
                is_command=False,
                write_uuid_override=args.write_uuid,
            )
        status = transport.latest_status
        if status is not None:
            print(f"Final status: {status}")
        print("Print job sent successfully")
        if effective_post_send_hold_seconds > 0:
            print(
                f"Holding connection after success for {effective_post_send_hold_seconds} seconds"
            )
            await asyncio.sleep(effective_post_send_hold_seconds)
    finally:
        await device.disconnect()


def main(argv: list[str] | None = None) -> None:
    asyncio.run(run_cli(parse_args(argv)))
