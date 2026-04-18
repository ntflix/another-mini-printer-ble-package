# Another Mini Printer BLE Package

This project is a reusable Python package for BLE mini printers reverse-engineered from the Fun Print 8.04.11 APK and when that got infuriating I found [rbaron's reverse-engineer of it](https://github.com/rbaron/catprinter)… but at that point I was already in too deep in making a package, so decided to use their repo as protocol help and here we are.

## What it includes

- Real BLE scan/connect/write with `bleak`
- MXW01 (`ae30-v5`) protocol transaction on verified characteristics:
  - control writes on `ae01`
  - notifications on `ae02`
  - raster data rows on `ae03`
- Packaged CLI entry point: `mini-printer`
- Reusable library API: `MiniPrinterClient`
- Optional image-file printing via `--image`
- Image dithering options: `none`, `floyd-steinberg`, `halftone`

## Package layout

- `mini_printer_demo/cli.py`: package CLI implementation
- `mini_printer_demo/api.py`: high-level reusable API
- `mini_printer_demo/families.py`: MXW01 BLE profile and UUIDs
- `mini_printer_demo/devices.py`: MXW01 print transaction
- `mini_printer_demo/transport.py`: BLE transport + flow control
- `mini_printer_demo/protocol.py`: MXW01 packet and raster helpers
- `mini_printer_demo/types.py`: typed options models
- `demo_print.py`: compatibility wrapper for existing script usage

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Quick start

1. Scan:

```bash
mini-printer --scan
```

On macOS, scanned `address` values might be CoreBluetooth UUIDs, not device MAC addresses. I had some trouble here so mostly just didn't bother scanning and connected with `--name` each time.

2. Print test page:

```bash
mini-printer \
  --address AA:BB:CC:DD:EE:FF \
  --mode v5 \
  --text "Hello printer" \
  --speed 25 \
  --intensity 0x70 \
  --quality 3 \
  --energy 12000 \
  --interval-ms 15
```

If you only know the printer name (recommended on macOS):

```bash
mini-printer \
  --name MXW01 \
  --mode v5 \
  --text "Hello printer"
```

3. Print an image file:

```bash
mini-printer \
  --name MXW01 \
  --image ./sample.png \
  --dither floyd-steinberg \
  --interval-ms 15
```

The image is converted to RGB and resized to printer width (384 dots) with original aspect ratio preserved before rasterisation.
Use `--intensity` to set MXW01 print darkness (`0x00` to `0xFF`; higher values produce darker prints).

4. Use halftone dithering:

```bash
mini-printer \
  --name MXW01 \
  --image ./sample.png \
  --dither halftone \
  --halftone-cell-size 4
```

Run with verbose BLE logs:

```bash
mini-printer --name MXW01 --mode v5 --verbose
```

Run minimal probe command tests (no full image payload):

```bash
mini-printer --name MXW01 --probe status --verbose
mini-printer --name MXW01 --probe intensity --intensity 0x80 --verbose
mini-printer --name MXW01 --probe flush --verbose
```

You can also run the package module directly:

```bash
python -m mini_printer_demo --scan
```

## Reusable API

```python
import asyncio

from mini_printer_demo import ConnectOptions, DitherAlgorithm, MiniPrinterClient


async def main() -> None:
  client = MiniPrinterClient()
  client.set_verbose(True)
  await client.connect(
    address=None,
    options=ConnectOptions(target_name="MXW01", timeout_seconds=20.0),
  )
  try:
    await client.print_image_path(
      image_path="./sample.png",
      dither=DitherAlgorithm.FLOYD_STEINBERG,
    )
  finally:
    await client.disconnect()


asyncio.run(main())
```

If you scanned a CoreBluetooth UUID address, pass that exact UUID in `--address`.

## MXW01 profile

- service `0000ae30-0000-1000-8000-00805f9b34fb`
- notify `ae02`, data write `ae03`, control write `ae01`
- flow control: notify pause/resume
- print flow (`v5` mode):
  - send `A2` intensity packet on `ae01`
  - send `A1` status request and wait for `A1` notify on `ae02`
  - send `A9` print request and wait for `A9` notify on `ae02`
  - stream 48-byte raster rows to `ae03` (minimum 90 rows)
  - send `AD` flush and wait for `AA` completion notify

## Notes

- This project sends real BLE writes to a real printer.
- MXW01 uses `22 21` framed control packets with CRC-8 over payload and `FF` footer.
- On macOS, classic Bluetooth pairing/connect tools (including `blueutil --pair`/`--connect`) can fail for BLE-only printers; this demo uses direct BLE GATT and does not require classic pairing.
- Start with low speed and small images until your device is validated.
- If your printer behaves unexpectedly, try:
  - increasing `--interval-ms`
  - magnets
  - milk steak

## Type checking

```bash
pip install -e .[dev]
mypy mini_printer_demo demo_print.py
```
