from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PrintTone(Enum):
    DOT = "dot"
    GRAY = "gray"


@dataclass(frozen=True)
class PrintOptions:
    intensity: int = 0x5D
    speed: int = 25
    quality: int = 3
    energy: int = 12000
    package_length: int = 20
    write_interval_ms: int = 20
    tone: PrintTone = PrintTone.DOT


@dataclass(frozen=True)
class ConnectOptions:
    timeout_seconds: float = 20.0
    mtu: int | None = None
    target_name: str | None = None
    scan_timeout_seconds: float = 8.0
    post_connect_delay_seconds: float = 0.3


@dataclass(frozen=True)
class ImageOptions:
    width: int = 384
    height: int = 240
    text: str = "Hello mini printer"
