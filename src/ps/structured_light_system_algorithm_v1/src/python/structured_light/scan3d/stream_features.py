"""Strict SLF1 wire admission and immutable, same-capture feature records.

The accepted-request timestamp is deliberately not an exposure timestamp.
Legacy Profile and DiagnosticRow records remain the public capture interface.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np

from ..p2.errors import ProtocolError
from ..p2.types import DiagnosticRow
from ..profile import Profile, ProfilePoint
from ..stripe_v1.types import StripeConfig

SLF1_MAGIC = 0x534C4631
SLF1_ABI = 0x00010000
SLF1_MAX_BYTES = (20 + 11 * 480) * 4
FEATURE_DMA_CONTRACT = dict(enabled=True, format='SLF1', abi=SLF1_ABI,
    capability_magic=0x52464431, capability_offset=0x134, abi_offset=0x138,
    mode_offset=0x13c, max_packet_bytes=SLF1_MAX_BYTES)
_ROW_SETTERS = tuple(getattr(DiagnosticRow, name).__set__ for name in DiagnosticRow.__dataclass_fields__)
_POINT_SETTERS = tuple(getattr(ProfilePoint, name).__set__ for name in ProfilePoint.__dataclass_fields__)


@dataclass(frozen=True)
class FeatureConfiguration:
    width: int
    height: int
    fixed_threshold: int
    roi: tuple[int, int, int, int]
    min_width: int
    max_width: int
    min_sum: int
    min_peak: int
    saturation_level: int

    def matches(self, config: StripeConfig) -> bool:
        return (config.threshold_mode == 'fixed' and config.saturation_level == 255
                and self.saturation_level == 255
                and (config.roi or (0, 0, self.width, self.height)) == self.roi
                and all(getattr(config, key) == getattr(self, key) for key in
                        ('fixed_threshold', 'min_width', 'max_width', 'min_sum', 'min_peak')))


@dataclass(frozen=True)
class FeaturePacket:
    """Decoded immutable wire evidence; always reparse payload at admission.

    Callers must not use a Python type or mutable attribute as wire evidence.
    """
    payload: bytes
    request_id: int
    epoch: int
    configuration: FeatureConfiguration
    profile: Profile
    rows: tuple[DiagnosticRow, ...]
    support: tuple[tuple[int, int, int], ...]


def decode_slf1(payload: bytes | bytearray | memoryview, *, frame_width: int,
                frame_height: int, request_id: int | None = None,
                capture_epoch: int | None = None) -> FeaturePacket:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ProtocolError('SLF1 payload must be bytes-like')
    data = bytes(payload)
    if len(data) < 80 or len(data) > SLF1_MAX_BYTES or len(data) % 4:
        raise ProtocolError('SLF1 packet length mismatch')
    words = np.frombuffer(data, dtype='<u4')
    h = [int(v) for v in words[:20]]
    count = h[10]
    if (h[0] != SLF1_MAGIC or h[1] != SLF1_ABI or h[2] != len(words)
            or not 1 <= count <= 480 or len(words) != 20 + 11 * count):
        raise ProtocolError('SLF1 magic/ABI/word count mismatch')
    if (h[4] >> 16 or h[9] >> 16 or h[13] >> 8 or h[14] >> 20
            or h[15] >> 20 or h[16] >> 20 or h[17] >> 18 or h[18] >> 16 or h[19]):
        raise ProtocolError('SLF1 reserved header bits are nonzero')
    if (type(frame_width) is not int or type(frame_height) is not int
            or not 1 <= frame_width <= 752 or not 1 <= frame_height <= 480
            or (h[11], h[12], count) != (frame_width, frame_height, frame_height)):
        raise ProtocolError('SLF1 frame geometry mismatch')
    if ((request_id is not None and h[3] != request_id)
            or (capture_epoch is not None and h[4] != capture_epoch)):
        raise ProtocolError('SLF1 capture identity/epoch mismatch')
    x0, x1 = h[14] & 1023, h[14] >> 10
    y0, y1 = h[15] & 1023, h[15] >> 10
    min_width, max_width = h[16] & 1023, h[16] >> 10
    if (not 0 <= x0 < x1 <= frame_width or not 0 <= y0 < y1 <= frame_height
            or not 1 <= min_width <= max_width <= 1023 or h[18] >> 8 != 255):
        raise ProtocolError('SLF1 configuration bounds mismatch')
    cfg = FeatureConfiguration(frame_width, frame_height, h[13], (x0, y0, x1, y1),
                               min_width, max_width, h[17], h[18] & 255, h[18] >> 8)
    values = words[20:].reshape(count, 11)
    if np.any(values[:, 9] != h[3]):
        raise ProtocolError('SLF1 mixed row request identity')
    if not np.array_equal(values[:, 0], np.arange(count)):
        raise ProtocolError('SLF1 row order or duplicate mismatch')
    limits = np.array([479, (1 << 22) - 1, (1 << 18) - 1, (1 << 27) - 1,
                       255, frame_width, frame_width, (1 << 24) - 1, 15,
                       0xffffffff, (1 << 30) - 1], dtype=np.uint32)
    if np.any(values > limits):
        raise ProtocolError('SLF1 row field overflow/reserved bits')

    # All fields are now bounded. Signed int64 keeps every difference and
    # Q12 cross-product exact on both ARMv7 and desktop (largest < 2**41).
    row, xc, s0, s1, peak, width, components, packed, diagnostics, identity, meta = values.astype(np.int64).T
    first, last, saturated = meta & 1023, (meta >> 10) & 1023, meta >> 20
    valid = (packed & (1 << 23)) != 0
    positive = components != 0
    quality, flags = packed & 255, (packed >> 8) & 0x7fff
    d = 255 - h[13]
    high_support = width - components + 1
    low_support = np.where(components == 1, width, components)
    scaled = s1 << 12
    invalid_valid = valid & (~positive | (s0 == 0) | (xc >= frame_width << 12))
    invalid_center = valid & ((xc * s0 > scaled) | (scaled >= (xc + 1) * s0))
    invalid_inactive = ~valid & ((xc != 0) | (quality != 0))
    invalid_empty = ~positive & ((meta != 0) | (s0 != 0) | (s1 != 0) | (peak != 0) | (width != 0))
    invalid_support = positive & ((row < y0) | (row >= y1) | (first < x0) | (first > last)
        | (last >= x1) | (width != last - first + 1) | (components > (width + 1) // 2)
        | (saturated > high_support) | (s0 < low_support) | (s0 > high_support * d)
        | (s1 < s0 * first) | (s1 > s0 * last) | (peak < 1) | (peak > d))

    # MULTI width includes gaps. Preserve the scalar oracle's existence test
    # for a realizable positive-support cardinality K; do not assume K=width.
    low_k = np.where(components == 1, width, np.maximum(components, saturated))
    high_k = np.where(components == 1, width, high_support)
    if d <= 1:
        # For d==0, positive rows already fail support bounds; this branch
        # also avoids a zero denominator while retaining their first error.
        possible = (low_k <= saturated) & (saturated <= high_k) & (s0 == saturated)
    else:
        low_k = np.maximum(low_k, saturated + np.maximum(0, (s0 - saturated * d + d - 2) // (d - 1)))
        high_k = np.minimum(high_k, saturated + s0 - saturated * d)
        low_k = np.maximum(low_k, (s0 + np.maximum(peak, 1) - 1) // np.maximum(peak, 1))
        high_k = np.minimum(high_k, s0 - peak + 1)
        possible = low_k <= high_k
    invalid_saturation = positive & (~possible | ((saturated > 0) & (peak != d))
                                    | ((saturated == 0) & (peak >= d)))
    # The original grammar visits source rows first, then rules in this order.
    # Retain that exact compound-fault priority without a Python row loop.
    faults = np.zeros(count, np.uint8)
    for code, mask in enumerate((invalid_valid, invalid_center, invalid_inactive,
                                invalid_empty, invalid_support, invalid_saturation), 1):
        faults[(faults == 0) & mask] = code
    bad = np.flatnonzero(faults)
    if len(bad):
        messages = ('', 'SLF1 valid row has invalid support/center',
                    'SLF1 valid center differs from exact support sums',
                    'SLF1 invalid row has nonzero center/quality',
                    'SLF1 empty row has nonzero support', 'SLF1 support bounds/sums mismatch',
                    'SLF1 saturation metadata is inconsistent with support sums/peak')
        raise ProtocolError(messages[int(faults[bad[0]])])
    if h[8] != int(np.count_nonzero(valid)):
        raise ProtocolError('SLF1 valid count mismatch')

    rows, points = [None] * count, [None] * count
    support = tuple(map(tuple, np.column_stack((first, last, saturated)).tolist()))
    records = np.column_stack((row, xc, s0, s1, peak, width, components, flags,
                               quality, diagnostics, identity)).tolist()
    allocate = object.__new__
    row_set, xc_set, s0_set, s1_set, peak_set, width_set, count_set, flags_set, quality_set, valid_set, diag_set, id_set = _ROW_SETTERS
    p_row, p_xc, p_quality, p_valid, p_flags = _POINT_SETTERS
    for (row, xc, s0, s1, peak, width, components, flags, quality, diagnostics, identity), active in zip(records, valid.tolist()):
        # Only allocation remains row-local. All wire grammar and physical
        # constraints passed above; standard frozen records need no recheck.
        item = allocate(DiagnosticRow)
        row_set(item, row); xc_set(item, xc); s0_set(item, s0); s1_set(item, s1)
        peak_set(item, peak); width_set(item, width); count_set(item, components)
        flags_set(item, flags); quality_set(item, quality); valid_set(item, active)
        diag_set(item, diagnostics); id_set(item, identity)
        rows[row] = item
        point = allocate(ProfilePoint)
        p_row(point, row); p_xc(point, xc); p_quality(point, quality); p_valid(point, active); p_flags(point, flags)
        points[row] = point
    profile = allocate(Profile)
    for name, value in dict(format_version=2, profile_id=h[3], angle_raw=h[5],
                           timestamp=h[6] | h[7] << 32, error_flags=h[9], points=tuple(points),
                           frame_width=frame_width, frame_height=frame_height).items():
        object.__setattr__(profile, name, value)
    return FeaturePacket(data, h[3], h[4], cfg, profile, tuple(rows), tuple(support))
