"""Strict codecs for the legacy SLP1 and Sprint 1.5 SLP2 profile packets.

The wire format is little-endian, with each packet represented as unsigned
32-bit words.  This module deliberately uses only the Python standard library
so it can run on the PYNQ host and on a clean desktop Python installation.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Final

from .generated import (
    MAX_PROFILE_POINTS,
    POINT_WORDS,
    PROFILES,
    ROW_BITS,
    SLP1_HEADER_WORDS,
    SLP1_MAGIC,
    SLP1_VALID_BIT,
    SLP2_HEADER_WORDS,
    SLP2_MAGIC,
    SLP2_VALID_BIT,
    XC_BITS,
    XC_FRACTION_BITS,
)


MAX_POINTS: Final[int] = MAX_PROFILE_POINTS
XC_FIXED_BITS: Final[int] = XC_BITS
_LAYOUTS = {layout["version"]: layout for layout in PROFILES}
FLAGS_BITS: Final[int] = _LAYOUTS[1]["flags_bits"]
QUALITY_BITS: Final[int] = _LAYOUTS[1]["quality_bits"]
UINT32_MAX: Final[int] = 0xFFFF_FFFF
UINT64_MAX: Final[int] = 0xFFFF_FFFF_FFFF_FFFF
XC_FIXED_MAX: Final[int] = (1 << XC_FIXED_BITS) - 1
ROW_MAX: Final[int] = (1 << ROW_BITS) - 1
FLAGS_MAX: Final[int] = (1 << FLAGS_BITS) - 1
QUALITY_MAX: Final[int] = (1 << QUALITY_BITS) - 1

_WORD = struct.Struct("<I")
_SLP1_HEADER_WORDS: Final[int] = SLP1_HEADER_WORDS
_SLP2_HEADER_WORDS: Final[int] = SLP2_HEADER_WORDS
_POINT_WORDS: Final[int] = POINT_WORDS


class ProfileError(ValueError):
    """Raised when a Profile packet or its in-memory representation is invalid."""


def _require_uint(name: str, value: object, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError(f"{name} must be an integer")
    if value < 0 or value > maximum:
        raise ProfileError(f"{name} must be in range 0..{maximum}")
    return value


def _validate_frame_dimensions(frame_width: object, frame_height: object) -> tuple[int, int]:
    # xc_fixed has 10 integer bits and row has 9 bits in the defined hardware
    # contract.  A 752x480 MT9V034 frame and the 640x480 offline frame fit.
    width = _require_uint("frame_width", frame_width, 1 << (XC_FIXED_BITS - XC_FRACTION_BITS))
    height = _require_uint("frame_height", frame_height, 1 << ROW_BITS)
    if width == 0 or height == 0:
        raise ProfileError("frame_width and frame_height must be positive")
    return width, height


@dataclass(frozen=True, slots=True)
class ProfilePoint:
    """One row result. ``quality`` is an 8-bit peak-related value, not a probability."""

    row: int
    xc_fixed: int
    quality: int
    valid: bool
    flags: int = 0

    def __post_init__(self) -> None:
        _require_uint("row", self.row, ROW_MAX)
        _require_uint("xc_fixed", self.xc_fixed, XC_FIXED_MAX)
        _require_uint("quality", self.quality, QUALITY_MAX)
        _require_uint("flags", self.flags, FLAGS_MAX)
        if type(self.valid) is not bool:
            raise ProfileError("valid must be bool")


@dataclass(frozen=True, slots=True)
class Profile:
    """A decoded SLP1/SLP2 profile plus the frame dimensions used to validate it.

    ``angle_raw`` and ``timestamp`` preserve their original unsigned wire values.
    SLP1 angle units and timestamp frequency are unspecified, so they are never
    guessed.  SLP2 angle is unsigned Q16.16 radians and its timestamp is the
    100 MHz system counter sampled when a capture request is accepted.
    """

    format_version: int
    profile_id: int
    angle_raw: int
    timestamp: int
    error_flags: int
    points: tuple[ProfilePoint, ...]
    frame_width: int = 640
    frame_height: int = 480

    def __post_init__(self) -> None:
        _require_uint("format_version", self.format_version, 2)
        if self.format_version not in (1, 2):
            raise ProfileError("format_version must be 1 (SLP1) or 2 (SLP2)")
        _require_uint("profile_id", self.profile_id, UINT32_MAX)
        _require_uint("angle_raw", self.angle_raw, UINT32_MAX)
        ts_max = UINT32_MAX if self.format_version == 1 else UINT64_MAX
        _require_uint("timestamp", self.timestamp, ts_max)
        _require_uint("error_flags", self.error_flags, UINT32_MAX)
        width, height = _validate_frame_dimensions(self.frame_width, self.frame_height)
        object.__setattr__(self, "frame_width", width)
        object.__setattr__(self, "frame_height", height)

        if not isinstance(self.points, tuple):
            try:
                object.__setattr__(self, "points", tuple(self.points))
            except TypeError as exc:
                raise ProfileError("points must be an iterable of ProfilePoint values") from exc
        if len(self.points) > MAX_POINTS:
            raise ProfileError(f"point_count must be at most {MAX_POINTS}")

        seen_rows: set[int] = set()
        for index, point in enumerate(self.points):
            if not isinstance(point, ProfilePoint):
                raise ProfileError(f"points[{index}] must be a ProfilePoint")
            # Recheck the nested frozen value as well: callers can bypass a
            # frozen dataclass with object.__setattr__, and encode must fail
            # with a domain error rather than leak a struct.error.
            point.__post_init__()
            if point.row >= height:
                raise ProfileError(f"points[{index}].row {point.row} is outside frame height {height}")
            if point.row in seen_rows:
                raise ProfileError(f"duplicate row {point.row}")
            seen_rows.add(point.row)
            if point.valid and point.xc_fixed >= (width << XC_FRACTION_BITS):
                raise ProfileError(
                    f"points[{index}].xc_fixed is outside frame width {width}"
                )

    @property
    def point_count(self) -> int:
        return len(self.points)

    @property
    def valid_count(self) -> int:
        return sum(point.valid for point in self.points)

    @property
    def angle_rad(self) -> float | None:
        if self.format_version == 1:
            return None
        return self.angle_raw / (1 << _LAYOUTS[2]["angle_fraction_bits"])

    @property
    def timestamp_semantics(self) -> str:
        if self.format_version == 1:
            return "raw_32bit_unspecified_frequency"
        return "capture_request_accepted_system_counter_100mhz"

    @property
    def timestamp_bits(self) -> int:
        return 32 if self.format_version == 1 else 64

    @property
    def frame_id(self) -> None:
        """Physical camera frame ID is absent from both packet formats."""
        return None

    @property
    def diagnostics(self) -> None:
        """S0/S1 and candidate count are absent from the legacy wire layouts."""
        return None


def _payload_words(payload: bytes | bytearray | memoryview) -> tuple[int, ...]:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ProfileError("payload must be bytes-like")
    try:
        data = bytes(payload)
    except (TypeError, ValueError) as exc:
        raise ProfileError("payload must be a contiguous bytes-like object") from exc
    if not data:
        raise ProfileError("empty payload: a profile must contain its complete header")
    if len(data) % _WORD.size:
        raise ProfileError(f"payload length {len(data)} is not aligned to 32-bit words")
    return tuple(item[0] for item in struct.iter_unpack("<I", data))


def decode_profile(
    payload: bytes | bytearray | memoryview,
    *,
    frame_width: int,
    frame_height: int,
) -> Profile:
    """Decode one SLP1 or SLP2 packet, validating its layout and coordinates."""

    width, height = _validate_frame_dimensions(frame_width, frame_height)
    words = _payload_words(payload)
    magic = words[0]
    if magic == SLP1_MAGIC:
        version = 1
        header_words = _SLP1_HEADER_WORDS
        if len(words) < header_words:
            raise ProfileError(f"truncated SLP1 header: expected {header_words} words")
        _, profile_id, angle_raw, timestamp, declared_valid, error_flags, point_count = words[:header_words]
    elif magic == SLP2_MAGIC:
        version = 2
        header_words = _SLP2_HEADER_WORDS
        if len(words) < header_words:
            raise ProfileError(f"truncated SLP2 header: expected {header_words} words")
        (
            _,
            profile_id,
            angle_raw,
            timestamp_low,
            timestamp_high,
            declared_valid,
            error_flags,
            point_count,
        ) = words[:header_words]
        timestamp = timestamp_low | (timestamp_high << 32)
    else:
        raise ProfileError(f"unknown profile magic 0x{magic:08X}")

    if point_count > MAX_POINTS:
        raise ProfileError(f"point_count {point_count} exceeds maximum {MAX_POINTS}")
    expected_words = header_words + point_count * _POINT_WORDS
    if len(words) < expected_words:
        raise ProfileError(
            f"truncated packet: header declares {point_count} points, "
            f"expected {expected_words} words, received {len(words)}"
        )
    if len(words) > expected_words:
        raise ProfileError(
            f"packet has {len(words) - expected_words} extra word(s) after {point_count} points"
        )

    points: list[ProfilePoint] = []
    for index in range(point_count):
        base = header_words + index * _POINT_WORDS
        row, xc_fixed, packed = words[base : base + _POINT_WORDS]
        layout = _LAYOUTS[version]
        if packed & layout["reserved_mask"]:
            raise ProfileError(f"{layout['name']} point {index} has nonzero reserved bits")
        valid = bool(packed & (1 << layout["valid_bit"]))
        flags = (packed >> layout["flags_lsb"]) & FLAGS_MAX
        quality = (packed >> layout["quality_lsb"]) & QUALITY_MAX
        points.append(ProfilePoint(row=row, xc_fixed=xc_fixed, quality=quality, valid=valid, flags=flags))

    profile = Profile(
        format_version=version,
        profile_id=profile_id,
        angle_raw=angle_raw,
        timestamp=timestamp,
        error_flags=error_flags,
        points=tuple(points),
        frame_width=width,
        frame_height=height,
    )
    if declared_valid != profile.valid_count:
        raise ProfileError(
            f"header valid_count {declared_valid} does not match point valid bits "
            f"({profile.valid_count})"
        )
    return profile


def encode_profile(profile: Profile) -> bytes:
    """Encode a Profile in its original SLP1 or SLP2 layout."""

    if not isinstance(profile, Profile):
        raise ProfileError("profile must be a Profile")
    # Re-run invariant checks so mutation through a deliberately bypassed frozen
    # dataclass cannot silently produce malformed hardware packets.
    profile.__post_init__()

    if profile.format_version == 1:
        words = [
            SLP1_MAGIC,
            profile.profile_id,
            profile.angle_raw,
            profile.timestamp,
            profile.valid_count,
            profile.error_flags,
            profile.point_count,
        ]
    else:
        words = [
            SLP2_MAGIC,
            profile.profile_id,
            profile.angle_raw,
            profile.timestamp & UINT32_MAX,
            (profile.timestamp >> 32) & UINT32_MAX,
            profile.valid_count,
            profile.error_flags,
            profile.point_count,
        ]

    for point in profile.points:
        layout = _LAYOUTS[profile.format_version]
        common = (point.flags << layout["flags_lsb"]) | (point.quality << layout["quality_lsb"])
        valid_bit = 1 << layout["valid_bit"]
        packed = common | (valid_bit if point.valid else 0)
        words.extend((point.row, point.xc_fixed, packed))
    return b"".join(_WORD.pack(word) for word in words)


__all__ = [
    "MAX_POINTS",
    "Profile",
    "ProfileError",
    "ProfilePoint",
    "SLP1_MAGIC",
    "SLP2_MAGIC",
    "decode_profile",
    "encode_profile",
]
