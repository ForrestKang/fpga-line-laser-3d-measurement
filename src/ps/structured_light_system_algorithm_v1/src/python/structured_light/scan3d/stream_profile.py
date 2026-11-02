"""Batch-validated SLP2 decoding into the unchanged standard frozen records.

``profile.decode_profile`` remains the specification and SLP1/error fallback.
The successful SLP2 path validates the entire wire grammar before allocating
objects, then initializes exactly Profile/ProfilePoint slots without running
the same per-point checks again in each dataclass constructor. Encoding still
revalidates every standard object, including deliberate frozen-object mutation.
"""
from __future__ import annotations

import numpy as np

from ..generated import POINT_WORDS, PROFILES, SLP2_HEADER_WORDS, SLP2_MAGIC, XC_FRACTION_BITS
from ..profile import (FLAGS_MAX, MAX_POINTS, QUALITY_MAX, ROW_MAX, XC_FIXED_MAX,
                       Profile, ProfileError, ProfilePoint, _validate_frame_dimensions, decode_profile)

_LAYOUT = next(layout for layout in PROFILES if layout["version"] == 2)
_VALID_MASK = 1 << _LAYOUT["valid_bit"]
_POINT_SETTERS = tuple(getattr(ProfilePoint, name).__set__ for name in ProfilePoint.__dataclass_fields__)


def decode_slp2_fast(payload: bytes | bytearray | memoryview, *, frame_width: int,
                     frame_height: int) -> Profile:
    """Match decode_profile admission/errors; accelerate valid SLP2 packets.

    Bytearray and memoryview inputs are snapshotted to immutable bytes before
    validation, preventing a mutable input from changing after the checks.
    Invalid packets intentionally use the original error path so compound
    faults retain its first-error priority and exact ProfileError messages.
    """
    width, height = _validate_frame_dimensions(frame_width, frame_height)
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ProfileError("payload must be bytes-like")
    try:
        data = bytes(payload)
    except (TypeError, ValueError) as exc:
        raise ProfileError("payload must be a contiguous bytes-like object") from exc
    if not data or len(data) % 4:
        return decode_profile(data, frame_width=width, frame_height=height)
    words = np.frombuffer(data, dtype="<u4")
    if int(words[0]) != SLP2_MAGIC or len(words) < SLP2_HEADER_WORDS:
        return decode_profile(data, frame_width=width, frame_height=height)
    count = int(words[7])
    if count > MAX_POINTS or len(words) != SLP2_HEADER_WORDS + count * POINT_WORDS:
        return decode_profile(data, frame_width=width, frame_height=height)

    rows, centers, packed = words[SLP2_HEADER_WORDS:].reshape(count, POINT_WORDS).T
    valid = (packed & _VALID_MASK) != 0
    # u32 parsing intrinsically validates every header field's integer type,
    # sign and width; masks below cover all point-field and Profile invariants.
    malformed = ((packed & _LAYOUT["reserved_mask"]) != 0) | (rows > ROW_MAX) | (rows >= height)
    malformed |= (centers > XC_FIXED_MAX) | (valid & (centers >= (width << XC_FRACTION_BITS)))
    ordered_unique = bool(np.all(rows[1:] > rows[:-1]))
    if (np.any(malformed) or (not ordered_unique and len(np.unique(rows)) != count)
            or int(words[5]) != int(np.count_nonzero(valid))):
        return decode_profile(data, frame_width=width, frame_height=height)

    quality = (packed >> _LAYOUT["quality_lsb"]) & QUALITY_MAX
    flags = (packed >> _LAYOUT["flags_lsb"]) & FLAGS_MAX
    # tolist creates ordinary Python int/bool values, preserving the standard
    # API on both ARMv7 NumPy and desktop NumPy. These constructors are private
    # to this validated path; there is no public unchecked-object factory.
    setter, allocate = object.__setattr__, object.__new__
    row_set, center_set, quality_set, valid_set, flags_set = _POINT_SETTERS
    points = []
    for row, center, q, v, f in zip(rows.tolist(), centers.tolist(), quality.tolist(), valid.tolist(), flags.tolist()):
        point = allocate(ProfilePoint)
        row_set(point, row)
        center_set(point, center)
        quality_set(point, q)
        valid_set(point, v)
        flags_set(point, f)
        points.append(point)
    profile = allocate(Profile)
    setter(profile, "format_version", 2)
    setter(profile, "profile_id", int(words[1]))
    setter(profile, "angle_raw", int(words[2]))
    setter(profile, "timestamp", int(words[3]) | (int(words[4]) << 32))
    setter(profile, "error_flags", int(words[6]))
    setter(profile, "points", tuple(points))
    setter(profile, "frame_width", width)
    setter(profile, "frame_height", height)
    return profile


__all__ = ["decode_slp2_fast"]
