"""Strict offline gray8 frame input for the P1 host reference.

The camera contract used by P1 is deliberately small: a PNG must contain a
single 8-bit grayscale channel (Pillow mode ``L``), and one byte represents
one source pixel.  This module does not convert RGB, palette, or 16-bit PNGs
implicitly.  Pillow is imported only when a PNG is actually opened so the P0
contract package remains usable without simulation extras installed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path


class FrameIOError(ValueError):
    """Raised when a frame is not an unambiguous gray8 PNG."""


@dataclass(frozen=True, slots=True)
class GrayFrame:
    """An immutable row-major gray8 frame and its byte-level fingerprint."""

    width: int
    height: int
    pixels: bytes
    sha256: str

    def __post_init__(self) -> None:
        if type(self.width) is not int or self.width <= 0:
            raise FrameIOError("width must be a positive integer")
        if type(self.height) is not int or self.height <= 0:
            raise FrameIOError("height must be a positive integer")
        if not isinstance(self.pixels, bytes):
            try:
                object.__setattr__(self, "pixels", bytes(self.pixels))
            except (TypeError, ValueError) as exc:
                raise FrameIOError("pixels must be bytes-like") from exc
        expected = self.width * self.height
        if len(self.pixels) != expected:
            raise FrameIOError(
                f"gray8 frame has {len(self.pixels)} bytes; expected {expected}"
            )
        digest = hashlib.sha256(self.pixels).hexdigest()
        if self.sha256 != digest:
            raise FrameIOError("sha256 does not match the frame pixel bytes")

    @classmethod
    def from_pixels(cls, width: int, height: int, pixels: bytes | bytearray | memoryview) -> "GrayFrame":
        data = bytes(pixels)
        return cls(width, height, data, hashlib.sha256(data).hexdigest())

    def row(self, y: int) -> bytes:
        """Return one raw row without changing its coordinate system."""

        if type(y) is not int or not 0 <= y < self.height:
            raise IndexError(f"row {y!r} is outside 0..{self.height - 1}")
        start = y * self.width
        return self.pixels[start : start + self.width]

    def pixel(self, x: int, y: int) -> int:
        if type(x) is not int or type(y) is not int:
            raise TypeError("x and y must be integers")
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"pixel ({x}, {y}) is outside the frame")
        return self.pixels[y * self.width + x]


def _require_supported_dimensions(width: int, height: int) -> None:
    if (width, height) not in {(640, 480), (752, 480)}:
        raise FrameIOError(
            f"unsupported offline frame dimensions {width}x{height}; "
            "P1 supports 640x480 and 752x480"
        )


def load_gray8_png(path: str | Path, *, require_supported_dimensions: bool = True) -> GrayFrame:
    """Read a PNG without silently changing its pixel format.

    The import of Pillow is intentionally local.  Missing simulation
    dependencies produce a useful ``FrameIOError`` instead of making
    ``import structured_light`` fail for P0 users.
    """

    source = Path(path)
    if source.suffix.lower() != ".png":
        raise FrameIOError(f"expected a .png input, got {source.name!r}")
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise FrameIOError(
            "PNG input requires the simulation dependency Pillow; "
            "install the project's 'simulation' extra"
        ) from exc
    try:
        with Image.open(source) as image:
            if image.format != "PNG":
                raise FrameIOError(f"input is not a PNG: {source}")
            if image.mode != "L":
                raise FrameIOError(
                    f"PNG mode {image.mode!r} is not gray8 mode 'L'; "
                    "RGB/palette/16-bit conversion is disabled"
                )
            width, height = image.size
            if require_supported_dimensions:
                _require_supported_dimensions(width, height)
            pixels = image.tobytes()
    except FileNotFoundError as exc:
        raise FrameIOError(f"frame file not found: {source}") from exc
    except UnidentifiedImageError as exc:
        raise FrameIOError(f"invalid PNG image: {source}") from exc
    except OSError as exc:
        raise FrameIOError(f"cannot read PNG {source}: {exc}") from exc
    return GrayFrame.from_pixels(width, height, pixels)


def save_gray8_png(frame: GrayFrame, path: str | Path) -> None:
    """Write a :class:`GrayFrame` as a gray8 PNG for deterministic fixtures."""

    if not isinstance(frame, GrayFrame):
        raise FrameIOError("frame must be a GrayFrame")
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise FrameIOError("writing PNG requires Pillow") from exc
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    image = Image.frombytes("L", (frame.width, frame.height), frame.pixels)
    image.save(target, format="PNG", optimize=False, compress_level=9)


__all__ = ["FrameIOError", "GrayFrame", "load_gray8_png", "save_gray8_png"]
