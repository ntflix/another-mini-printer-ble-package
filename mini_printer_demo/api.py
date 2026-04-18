from __future__ import annotations

from pathlib import Path

from PIL import Image

from .devices import Ae30V5Device, WireMode, create_device
from .protocol import (
    DitherAlgorithm,
    ProbeCommand,
    apply_dithering,
    build_mxw01_intensity_command,
    build_probe_payload,
)
from .transport import BleTransport, ScanResult
from .types import ConnectOptions, PrintOptions

PRINTER_DOT_WIDTH = 384


class MiniPrinterClient:
    """Reusable high-level API for AE30/MXW01 printers."""

    def __init__(self, transport: BleTransport | None = None) -> None:
        self.transport: BleTransport = (
            transport if transport is not None else BleTransport()
        )
        self.device: Ae30V5Device = create_device(self.transport)

    @staticmethod
    async def scan(timeout_seconds: float = 8.0) -> list[ScanResult]:
        return await BleTransport.scan(timeout_seconds=timeout_seconds)

    def set_verbose(self, value: bool) -> None:
        self.transport.set_verbose(value)

    async def connect(self, *, address: str | None, options: ConnectOptions) -> None:
        await self.device.connect(address=address, options=options)

    async def disconnect(self) -> None:
        await self.device.disconnect()

    async def print_image(
        self,
        *,
        image: Image.Image,
        mode: WireMode = "v5",
        print_options: PrintOptions | None = None,
        dither: DitherAlgorithm = DitherAlgorithm.NONE,
        halftone_cell_size: int = 4,
        write_uuid_override: str | None = None,
    ) -> None:
        options = print_options if print_options is not None else PrintOptions()
        prepared = apply_dithering(
            image.convert("RGB"),
            algorithm=dither,
            halftone_cell_size=halftone_cell_size,
        )
        await self.device.print_image(
            image=prepared,
            print_options=options,
            mode=mode,
            is_command=False,
            write_uuid_override=write_uuid_override,
        )

    async def print_image_path(
        self,
        *,
        image_path: str | Path,
        mode: WireMode = "v5",
        print_options: PrintOptions | None = None,
        dither: DitherAlgorithm = DitherAlgorithm.NONE,
        halftone_cell_size: int = 4,
        write_uuid_override: str | None = None,
    ) -> None:
        image_path_value = Path(image_path)
        with Image.open(image_path_value) as source_image:
            image = source_image.convert("RGB")

        if image.width <= 0 or image.height <= 0:
            raise ValueError("Input image has invalid dimensions")

        scaled_height = max(1, round(image.height * PRINTER_DOT_WIDTH / image.width))
        resized = image.resize(  # pyright: ignore[reportUnknownMemberType]
            (PRINTER_DOT_WIDTH, scaled_height),
            Image.Resampling.LANCZOS,
        )

        await self.print_image(
            image=resized,
            mode=mode,
            print_options=print_options,
            dither=dither,
            halftone_cell_size=halftone_cell_size,
            write_uuid_override=write_uuid_override,
        )

    async def send_probe(
        self,
        *,
        command: ProbeCommand,
        print_options: PrintOptions | None = None,
        intensity: int = 0x5D,
        write_uuid_override: str | None = None,
    ) -> None:
        options = print_options if print_options is not None else PrintOptions()
        if command is ProbeCommand.INTENSITY:
            payload = build_mxw01_intensity_command(intensity)
        else:
            payload = build_probe_payload(command)
        await self.device.print_raw(
            payload=payload,
            print_options=options,
            is_command=True,
            write_uuid_override=write_uuid_override,
        )

    @property
    def latest_status(self) -> str | None:
        return self.transport.latest_status
