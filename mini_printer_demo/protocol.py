from __future__ import annotations

import zlib
from dataclasses import dataclass
from enum import Enum
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from .types import ImageOptions, PrintOptions

QX_PREFIX: Final[bytes] = bytes((0x51, 0x78))
QX_SUFFIX: Final[int] = 0xFF

QUALITY_COMMANDS: Final[dict[int, bytes]] = {
    1: bytes((0x51, 0x78, 0xA4, 0x00, 0x01, 0x00, 0x31, 0x97, 0xFF)),
    2: bytes((0x51, 0x78, 0xA4, 0x00, 0x01, 0x00, 0x32, 0x9E, 0xFF)),
    3: bytes((0x51, 0x78, 0xA4, 0x00, 0x01, 0x00, 0x33, 0x99, 0xFF)),
    4: bytes((0x51, 0x78, 0xA4, 0x00, 0x01, 0x00, 0x34, 0x8C, 0xFF)),
    5: bytes((0x51, 0x78, 0xA4, 0x00, 0x01, 0x00, 0x35, 0x8B, 0xFF)),
}

PRINT_IMAGE_MODE: Final[bytes] = bytes(
    (0x51, 0x78, 0xBE, 0x00, 0x01, 0x00, 0x00, 0x00, 0xFF)
)
PRINT_LATTICE: Final[bytes] = bytes(
    (
        0x51,
        0x78,
        0xA6,
        0x00,
        0x0B,
        0x00,
        0xAA,
        0x55,
        0x17,
        0x38,
        0x44,
        0x5F,
        0x5F,
        0x5F,
        0x44,
        0x38,
        0x2C,
        0xA1,
        0xFF,
    )
)
PAPER_FEED_200_DPI: Final[bytes] = bytes(
    (0x51, 0x78, 0xA1, 0x00, 0x02, 0x00, 0x30, 0x00, 0xF9, 0xFF)
)
FINISH_LATTICE: Final[bytes] = bytes(
    (
        0x51,
        0x78,
        0xA6,
        0x00,
        0x0B,
        0x00,
        0xAA,
        0x55,
        0x17,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x17,
        0x11,
        0xFF,
    )
)
GET_DEV_STATE: Final[bytes] = bytes(
    (0x51, 0x78, 0xA3, 0x00, 0x01, 0x00, 0x00, 0x00, 0xFF)
)


@dataclass(frozen=True)
class V5Payload:
    full_payload: bytes


@dataclass(frozen=True)
class V10Payload:
    full_payload: bytes


class ProbeCommand(Enum):
    STATUS = "status"
    INTENSITY = "intensity"
    FLUSH = "flush"


class DitherAlgorithm(Enum):
    NONE = "none"
    FLOYD_STEINBERG = "floyd-steinberg"
    HALFTONE = "halftone"


def _clamp_int(value: float, *, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(round(value))))


def apply_floyd_steinberg_dither(image: Image.Image) -> Image.Image:
    gray = image.convert("L")
    width, height = gray.size
    pixels = [
        [float(gray.getpixel((x, y))) for x in range(width)] for y in range(height)
    ]

    for y in range(height):
        for x in range(width):
            old_value = pixels[y][x]
            new_value = 0.0 if old_value < 128.0 else 255.0
            pixels[y][x] = new_value
            error = old_value - new_value

            if x + 1 < width:
                pixels[y][x + 1] += error * (7.0 / 16.0)
            if y + 1 < height:
                if x > 0:
                    pixels[y + 1][x - 1] += error * (3.0 / 16.0)
                pixels[y + 1][x] += error * (5.0 / 16.0)
                if x + 1 < width:
                    pixels[y + 1][x + 1] += error * (1.0 / 16.0)

    out = Image.new("L", (width, height), color=255)
    out_data = bytearray(width * height)
    i = 0
    for y in range(height):
        for x in range(width):
            out_data[i] = 0 if pixels[y][x] < 128.0 else 255
            i += 1
    out.frombytes(bytes(out_data))
    return out


def apply_halftone_dither(image: Image.Image, *, cell_size: int = 4) -> Image.Image:
    clamped_cell_size = max(2, cell_size)
    gray = image.convert("L")
    width, height = gray.size
    out = Image.new("L", (width, height), color=255)
    draw = ImageDraw.Draw(out)

    for top in range(0, height, clamped_cell_size):
        for left in range(0, width, clamped_cell_size):
            right = min(left + clamped_cell_size, width)
            bottom = min(top + clamped_cell_size, height)
            count = (right - left) * (bottom - top)
            if count <= 0:
                continue

            total = 0
            for y in range(top, bottom):
                for x in range(left, right):
                    total += int(gray.getpixel((x, y)))

            average = total / count
            darkness = 1.0 - (average / 255.0)
            if darkness <= 0:
                continue

            max_radius = min((right - left), (bottom - top)) / 2.0
            radius = max_radius * (darkness**0.5)
            center_x = left + (right - left) / 2.0
            center_y = top + (bottom - top) / 2.0
            draw.ellipse(
                (
                    _clamp_int(center_x - radius, minimum=0, maximum=width - 1),
                    _clamp_int(center_y - radius, minimum=0, maximum=height - 1),
                    _clamp_int(center_x + radius, minimum=0, maximum=width - 1),
                    _clamp_int(center_y + radius, minimum=0, maximum=height - 1),
                ),
                fill=0,
            )

    return out


def apply_dithering(
    image: Image.Image,
    *,
    algorithm: DitherAlgorithm,
    halftone_cell_size: int = 4,
) -> Image.Image:
    if algorithm is DitherAlgorithm.FLOYD_STEINBERG:
        return apply_floyd_steinberg_dither(image)
    if algorithm is DitherAlgorithm.HALFTONE:
        return apply_halftone_dither(image, cell_size=halftone_cell_size)
    return image.convert("L")


def mxw01_crc8(data: bytes, *, poly: int = 0x07, init: int = 0x00) -> int:
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def build_mxw01_control_packet(cmd_id: int, payload: bytes) -> bytes:
    payload_len = len(payload)
    header = bytes(
        (
            0x22,
            0x21,
            cmd_id & 0xFF,
            0x00,
            payload_len & 0xFF,
            (payload_len >> 8) & 0xFF,
        )
    )
    crc = mxw01_crc8(payload)
    return header + payload + bytes((crc, 0xFF))


def parse_mxw01_packet(
    packet: bytes,
) -> tuple[int, bytes, int | None, int | None] | None:
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


def build_mxw01_status_command() -> bytes:
    return build_mxw01_control_packet(0xA1, bytes((0x00,)))


def build_mxw01_intensity_command(intensity: int = 0x5D) -> bytes:
    value = max(0, min(0xFF, intensity))
    return build_mxw01_control_packet(0xA2, bytes((value,)))


def build_mxw01_print_command(*, line_count: int, mode: int = 0x00) -> bytes:
    clamped_line_count = max(0, min(0xFFFF, line_count))
    payload = bytes(
        (
            clamped_line_count & 0xFF,
            (clamped_line_count >> 8) & 0xFF,
            0x30,
            mode & 0xFF,
        )
    )
    return build_mxw01_control_packet(0xA9, payload)


def build_mxw01_flush_command() -> bytes:
    return build_mxw01_control_packet(0xAD, bytes((0x00,)))


def build_mxw01_data_lines(
    image: Image.Image,
    *,
    target_width: int,
    minimum_lines: int = 90,
) -> list[bytes]:
    rows = image_to_1bit_rows(image, target_width=target_width)
    width_bytes = (target_width + 7) // 8
    normalized = [row[:width_bytes].ljust(width_bytes, b"\x00") for row in rows]
    while len(normalized) < minimum_lines:
        normalized.append(bytes(width_bytes))
    return normalized


def calc_crc8(data: bytes, *, poly: int = 0x07, init: int = 0x00) -> int:
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def build_speed_command(speed: int) -> bytes:
    speed_byte = speed & 0xFF
    cmd = bytearray((0x51, 0x78, 0xBD, 0x00, 0x01, 0x00, speed_byte, 0x00, 0x00))
    cmd[7] = calc_crc8(bytes((cmd[6],)))
    cmd[8] = 0xFF
    return bytes(cmd)


def build_energy_command(energy: int) -> bytes:
    clamped = max(0, min(0xFFFF, energy))
    lo = clamped & 0xFF
    hi = (clamped >> 8) & 0xFF
    cmd = bytearray((0x51, 0x78, 0xAF, 0x00, 0x02, 0x00, lo, hi, 0x00, 0x00))
    cmd[8] = calc_crc8(bytes((lo, hi)))
    cmd[9] = 0xFF
    return bytes(cmd)


def _build_row_command(row_bytes: bytes) -> bytes:
    width = len(row_bytes)
    length_lo = width & 0xFF
    length_hi = (width >> 8) & 0xFF
    row_crc = calc_crc8(row_bytes)
    out = bytearray((0x51, 0x78, 0xA2, 0x00, length_lo, length_hi))
    out.extend(row_bytes)
    out.append(row_crc)
    out.append(QX_SUFFIX)
    return bytes(out)


def build_test_image(options: ImageOptions) -> Image.Image:
    image = Image.new("RGB", (options.width, options.height), color="white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    draw.rectangle(
        (0, 0, options.width - 1, options.height - 1), outline="black", width=2
    )
    draw.text((10, 10), options.text, fill="black", font=font)
    draw.line((10, 50, options.width - 10, 50), fill="black", width=1)
    draw.line((10, 70, options.width - 10, options.height - 20), fill="black", width=2)
    draw.ellipse(
        (options.width - 90, 10, options.width - 20, 80), outline="black", width=2
    )
    return image


def image_to_1bit_rows(image: Image.Image, *, target_width: int) -> list[bytes]:
    gray = image.convert("L")
    width, height = gray.size
    if width != target_width:
        ratio = target_width / width
        resized_height = max(1, int(height * ratio))
        gray = gray.resize((target_width, resized_height), Image.Resampling.LANCZOS)
    bw = gray.point(lambda p: 255 if p > 180 else 0, mode="1")
    w, h = bw.size
    packed_rows: list[bytes] = []
    for y in range(h):
        row = bytearray()
        for x_block in range((w + 7) // 8):
            value = 0
            for bit_index in range(8):
                x = x_block * 8 + bit_index
                if x < w and bw.getpixel((x, y)) == 0:
                    value |= 1 << bit_index
            row.append(value)
        packed_rows.append(bytes(row))
    return packed_rows


def build_v5_payload(
    image: Image.Image, options: PrintOptions, *, paper_feeds: int = 2
) -> V5Payload:
    quality = QUALITY_COMMANDS.get(options.quality, QUALITY_COMMANDS[3])
    rows = image_to_1bit_rows(image, target_width=image.width)

    # BUILD EACHLINEPIXTOCMDB COMBINED BLOCK (Java: V5g.eachLinePixToCmdB)
    # This block must contain energy + image_mode + speed + all rows as a single unit
    each_line_block = bytearray()

    # Add energy bytes if non-zero (Java: if (eneragy != 0))
    if options.energy != 0:
        each_line_block.extend(build_energy_command(options.energy))

    # Add image mode (Java: getPrintModel(printType))
    each_line_block.extend(PRINT_IMAGE_MODE)

    # Add speed command (Java: getPrintSpeed(speed))
    each_line_block.extend(build_speed_command(options.speed))

    # Add all row commands (Java: eachLinePixToCmdB rows loop)
    for row in rows:
        each_line_block.extend(_build_row_command(row))

    # NOW BUILD FINAL PACKET using the combined block
    packet_parts: list[bytes] = [
        quality,  # Java: getQuality(printQuality)
        PRINT_LATTICE,  # Java: this.printLattice
        bytes(each_line_block),  # <- SINGLE COMBINED BLOCK (not separate commands)
        build_speed_command(
            options.speed
        ),  # Java: getPrintSpeed(speed) second invocation
    ]
    packet_parts.extend(PAPER_FEED_200_DPI for _ in range(max(0, paper_feeds)))
    packet_parts.append(FINISH_LATTICE)
    packet_parts.append(GET_DEV_STATE)
    return V5Payload(full_payload=b"".join(packet_parts))


def build_v10_payload(image: Image.Image) -> V10Payload:
    rows = image_to_1bit_rows(image, target_width=image.width)
    raster = b"".join(rows)
    compressed = zlib.compress(raster, level=9)
    width_bytes = (image.width + 7) // 8
    payload = bytearray()
    payload.extend(bytes((0x1B, 0x23, 0x21)))
    payload.extend(width_bytes.to_bytes(2, byteorder="big", signed=False))
    payload.extend(image.height.to_bytes(2, byteorder="big", signed=False))
    payload.extend(len(compressed).to_bytes(4, byteorder="big", signed=False))
    payload.extend(compressed)
    return V10Payload(full_payload=bytes(payload))


def build_probe_payload(command: ProbeCommand) -> bytes:
    if command is ProbeCommand.STATUS:
        return build_mxw01_status_command()
    if command is ProbeCommand.INTENSITY:
        return build_mxw01_intensity_command(0x5D)
    return build_mxw01_flush_command()
