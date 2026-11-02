"""Integer gray8 ROW-centroid reference for the P1 software Golden Model.

The implementation intentionally follows the operations that the planned RTL
will perform.  It does not use NumPy or floating-point arithmetic for the
center calculation, and it keeps original image coordinates when an ROI is
used.  The returned diagnostic fields are useful for a row transaction
scoreboard; only ``xc_fixed``, ``quality``, ``valid`` and the low 15 flag bits
are carried by the existing SLP2 point format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .frame_io import GrayFrame


FRACTIONAL_BITS = 12
S0_BITS = 18
S1_BITS = 27
XC_BITS = 22
S0_MAX = (1 << S0_BITS) - 1
S1_MAX = (1 << S1_BITS) - 1
XC_MAX = (1 << XC_BITS) - 1

FLAG_NO_SUPPORT = 1 << 0
FLAG_WEAK = 1 << 1
FLAG_WIDE = 1 << 2
FLAG_MULTIPLE = 1 << 3
FLAG_BAD_LINE = 1 << 4
FLAG_OVERFLOW = 1 << 5


class GrayReferenceError(ValueError):
    """Raised for an invalid frame, ROI, or ROW-reference parameter."""


@dataclass(frozen=True, slots=True)
class RowResult:
    """One ROW transaction, including fields that are absent from SLP2."""

    row: int
    xc_fixed: int
    s0: int
    s1: int
    peak: int
    width: int
    candidate_count: int
    quality: int
    valid: bool
    flags: int
    # Versioned diagnostic sidecar fields.  They do not change SLP2 layout.
    too_narrow: bool = False
    roi_excluded: bool = False
    line_bad: bool = False
    overflow: bool = False
    support_start: int | None = None
    support_end: int | None = None

    @property
    def xc(self) -> float:
        """The Q10.12 center as a display value; RTL comparisons use xc_fixed."""

        return self.xc_fixed / float(1 << FRACTIONAL_BITS)

    def to_profile_point(self):
        """Convert to the existing SLP2 point type without changing flags."""

        from .profile import ProfilePoint

        return ProfilePoint(
            row=self.row,
            xc_fixed=self.xc_fixed,
            quality=self.quality,
            valid=self.valid,
            flags=self.flags,
        )


def _int_parameter(value: int, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise GrayReferenceError(f"{name} must be an integer >= {minimum}")
    return value


def _roi_for_frame(
    width: int,
    height: int,
    roi: tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int]:
    if roi is None:
        return 0, 0, width, height
    if not isinstance(roi, tuple) or len(roi) != 4:
        raise GrayReferenceError("roi must be a (x0, y0, x1, y1) tuple")
    x0, y0, x1, y1 = roi
    for value, name in zip(roi, ("roi.x0", "roi.y0", "roi.x1", "roi.y1")):
        _int_parameter(value, name)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise GrayReferenceError("ROI must satisfy 0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height")
    return roi


def _validate_thresholds(
    threshold: int,
    min_width: int,
    max_width: int,
    min_sum: int,
    min_peak: int,
    fractional_bits: int,
) -> None:
    _int_parameter(threshold, "threshold")
    if threshold > 255:
        raise GrayReferenceError("threshold must be in [0, 255]")
    _int_parameter(min_width, "min_width", minimum=1)
    _int_parameter(max_width, "max_width", minimum=1)
    if min_width > max_width:
        raise GrayReferenceError("min_width cannot exceed max_width")
    _int_parameter(min_sum, "min_sum")
    _int_parameter(min_peak, "min_peak")
    if min_peak > 255:
        raise GrayReferenceError("min_peak must be in [0, 255]")
    _int_parameter(fractional_bits, "fractional_bits")


def _line_bad_values(line_bad: bool | Sequence[bool] | None, height: int) -> tuple[bool, ...]:
    if line_bad is None:
        return (False,) * height
    if type(line_bad) is bool:
        return (line_bad,) * height
    try:
        values = tuple(line_bad)
    except TypeError as exc:
        raise GrayReferenceError("line_bad must be bool or one value per row") from exc
    if len(values) != height or any(type(value) is not bool for value in values):
        raise GrayReferenceError("line_bad must contain one bool per frame row")
    return values


def _frame_bytes(
    frame: GrayFrame | bytes | bytearray | memoryview,
    *,
    width: int | None,
    height: int | None,
) -> tuple[int, int, bytes]:
    if isinstance(frame, GrayFrame):
        if width is not None and width != frame.width or height is not None and height != frame.height:
            raise GrayReferenceError("explicit dimensions do not match GrayFrame")
        return frame.width, frame.height, frame.pixels
    if width is None or height is None:
        raise GrayReferenceError("width and height are required when frame is bytes-like")
    _int_parameter(width, "width", minimum=1)
    _int_parameter(height, "height", minimum=1)
    try:
        pixels = bytes(frame)
    except (TypeError, ValueError) as exc:
        raise GrayReferenceError("frame must be GrayFrame or bytes-like") from exc
    if len(pixels) != width * height:
        raise GrayReferenceError(
            f"gray8 frame has {len(pixels)} bytes; expected {width * height}"
        )
    return width, height, pixels


def row_centroid(
    pixels: bytes | bytearray | memoryview,
    *,
    row: int = 0,
    x0: int = 0,
    x1: int | None = None,
    threshold: int = 30,
    min_width: int = 1,
    max_width: int = 32,
    min_sum: int = 30,
    min_peak: int = 15,
    line_bad: bool = False,
    fractional_bits: int = FRACTIONAL_BITS,
) -> RowResult:
    """Compute one ROW result from a row in original frame coordinates."""

    data = bytes(pixels)
    if x1 is None:
        x1 = len(data)
    if not (0 <= x0 < x1 <= len(data)):
        raise GrayReferenceError("row x range must satisfy 0 <= x0 < x1 <= row length")
    _int_parameter(row, "row")
    _validate_thresholds(threshold, min_width, max_width, min_sum, min_peak, fractional_bits)

    s0 = 0
    s1 = 0
    peak = 0
    start: int | None = None
    end: int | None = None
    candidate_count = 0
    in_run = False
    for x in range(x0, x1):
        weight = int(data[x]) - threshold
        if weight > 0:
            s0 += weight
            s1 += x * weight
            peak = max(peak, weight)
            if not in_run:
                candidate_count += 1
                if start is None:
                    start = x
                in_run = True
            end = x
        else:
            in_run = False

    width = 0 if start is None or end is None else end - start + 1
    overflow = s0 > S0_MAX or s1 > S1_MAX
    # The supported 640/752 frames cannot overflow these fields.  For a
    # deliberately oversized standalone row, expose the bounded wire values
    # while retaining an explicit overflow diagnostic and invalid result.
    wire_s0 = min(s0, S0_MAX)
    wire_s1 = min(s1, S1_MAX)
    too_narrow = bool(start is not None and width < min_width)
    wide = bool(start is not None and width > max_width)
    weak = s0 < min_sum or peak < min_peak
    no_support = s0 == 0
    multiple = candidate_count > 1
    flags = 0
    if no_support:
        flags |= FLAG_NO_SUPPORT
    if weak:
        flags |= FLAG_WEAK
    if wide:
        flags |= FLAG_WIDE
    if multiple:
        flags |= FLAG_MULTIPLE
    if line_bad:
        flags |= FLAG_BAD_LINE
    if overflow:
        flags |= FLAG_OVERFLOW

    valid = bool(
        not no_support
        and not weak
        and not too_narrow
        and not wide
        and not multiple
        and not line_bad
        and not overflow
    )
    if valid:
        xc_fixed = (s1 << fractional_bits) // s0
        if xc_fixed > XC_MAX:
            overflow = True
            flags |= FLAG_OVERFLOW
            valid = False
            xc_fixed = 0
    else:
        xc_fixed = 0
    quality = peak if valid else 0
    return RowResult(
        row=row,
        xc_fixed=xc_fixed,
        s0=wire_s0,
        s1=wire_s1,
        peak=min(peak, 255),
        width=width,
        candidate_count=candidate_count,
        quality=quality,
        valid=valid,
        flags=flags,
        too_narrow=too_narrow,
        line_bad=line_bad,
        overflow=overflow,
        support_start=start,
        support_end=end,
    )


def reference_rows(
    frame: GrayFrame | bytes | bytearray | memoryview,
    *,
    width: int | None = None,
    height: int | None = None,
    threshold: int = 30,
    roi: tuple[int, int, int, int] | None = None,
    min_width: int = 1,
    max_width: int = 32,
    min_sum: int = 30,
    min_peak: int = 15,
    line_bad: bool | Sequence[bool] | None = None,
    fractional_bits: int = FRACTIONAL_BITS,
) -> tuple[RowResult, ...]:
    """Return one deterministic ROW result for every source image row.

    Rows outside the ROI's y range remain present as invalid records with an
    explicit ``roi_excluded`` sidecar flag.  This preserves frame coordinates
    and makes a complete 480-row Profile possible without inventing wire flags.
    """

    frame_width, frame_height, pixels = _frame_bytes(frame, width=width, height=height)
    x0, y0, x1, y1 = _roi_for_frame(frame_width, frame_height, roi)
    _validate_thresholds(threshold, min_width, max_width, min_sum, min_peak, fractional_bits)
    bad_values = _line_bad_values(line_bad, frame_height)
    results: list[RowResult] = []
    for y in range(frame_height):
        if y < y0 or y >= y1:
            results.append(
                RowResult(
                    row=y,
                    xc_fixed=0,
                    s0=0,
                    s1=0,
                    peak=0,
                    width=0,
                    candidate_count=0,
                    quality=0,
                    valid=False,
                    flags=0,
                    roi_excluded=True,
                )
            )
            continue
        offset = y * frame_width
        result = row_centroid(
            pixels[offset : offset + frame_width],
            row=y,
            x0=x0,
            x1=x1,
            threshold=threshold,
            min_width=min_width,
            max_width=max_width,
            min_sum=min_sum,
            min_peak=min_peak,
            line_bad=bad_values[y],
            fractional_bits=fractional_bits,
        )
        results.append(result)
    return tuple(results)


# Names used by early P1 callers; keep them as aliases to make the reference
# entry point discoverable without duplicating the arithmetic implementation.
compute_row_centroid = row_centroid
gray_row_reference = reference_rows


__all__ = [
    "FRACTIONAL_BITS", "S0_BITS", "S1_BITS", "XC_BITS", "S0_MAX", "S1_MAX", "XC_MAX",
    "FLAG_NO_SUPPORT", "FLAG_WEAK", "FLAG_WIDE", "FLAG_MULTIPLE", "FLAG_BAD_LINE", "FLAG_OVERFLOW",
    "GrayReferenceError", "RowResult", "row_centroid", "compute_row_centroid", "reference_rows",
    "gray_row_reference",
]
