"""Small arithmetic checks for the ROW-centroid interface contract.

These helpers do not implement image processing. They make the fixed-point
division and accumulator-width assumptions independently testable in Python.
"""

from __future__ import annotations

from dataclasses import dataclass


from .generated import XC_FRACTION_BITS, S0_BITS, S1_BITS, XC_BITS

FRACTIONAL_BITS = XC_FRACTION_BITS


@dataclass(frozen=True, slots=True)
class AccumulatorBounds:
    frame_width: int
    max_s0: int
    s0_bits: int
    max_s1: int
    s1_bits: int
    max_q12_numerator: int
    q12_numerator_bits: int


def _positive_int(value: int, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bits_for_max(value: int) -> int:
    return max(1, value.bit_length())


def row_accumulator_bounds(frame_width: int) -> AccumulatorBounds:
    """Return unsigned worst-case ROW-centroid widths for a gray8 row.

    A row has x coordinates ``0..frame_width-1`` and each weight is at most
    255. The Q12 division numerator is ``S1 << 12``.
    """
    width = _positive_int(frame_width, "frame_width")
    max_s0 = width * 255
    max_s1 = 255 * ((width - 1) * width // 2)
    max_q12 = max_s1 << FRACTIONAL_BITS
    return AccumulatorBounds(
        frame_width=width,
        max_s0=max_s0,
        s0_bits=_bits_for_max(max_s0),
        max_s1=max_s1,
        s1_bits=_bits_for_max(max_s1),
        max_q12_numerator=max_q12,
        q12_numerator_bits=_bits_for_max(max_q12),
    )


def centroid_fixed(s0: int, s1: int) -> int:
    """Return floor((S1 << 12) / S0), the unsigned Q10.12 column coordinate.

    ``s0 == 0`` has no defined centroid and is rejected for the caller to mark
    the row invalid. This mirrors integer hardware truncation.
    """
    if type(s0) is not int or type(s1) is not int:
        raise ValueError("S0 and S1 must be integers")
    if s0 <= 0:
        raise ValueError("S0 must be positive for a valid centroid")
    if s1 < 0:
        raise ValueError("S1 cannot be negative for nonnegative pixel weights")
    if s0 >= (1 << S0_BITS) or s1 >= (1 << S1_BITS):
        raise ValueError("S0/S1 exceeds candidate accumulator bit width")
    result = (s1 << FRACTIONAL_BITS) // s0
    if result >= (1 << XC_BITS):
        raise ValueError("centroid coordinate exceeds Q10.12")
    return result
