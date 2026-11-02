"""Same-frame Python oracle and field-by-field Profile comparison."""

from __future__ import annotations

from collections.abc import Iterable

from ..frame_io import GrayFrame
from ..gray_reference import RowResult, reference_rows
from ..profile import Profile, ProfileError, decode_profile
from .errors import ProtocolError
from .types import DiagnosticRow, DiagnosticsSnapshot, ReferenceComparison, ReferenceMismatch


def _mismatch(
    output: list[ReferenceMismatch], row: int | None, field: str, expected: object, actual: object,
    *, max_items: int,
) -> None:
    if expected != actual and len(output) < max_items:
        output.append(ReferenceMismatch(row=row, field=field, expected=expected, actual=actual))


def compare_rows(
    expected: Iterable[RowResult],
    actual: Iterable[DiagnosticRow],
    *,
    max_mismatches: int = 128,
) -> ReferenceComparison:
    """Compare every diagnostic field exposed by the P2 row table."""

    expected_rows, actual_rows = tuple(expected), tuple(actual)
    mismatches: list[ReferenceMismatch] = []
    fields = ("row", "xc_fixed", "s0", "s1", "peak", "width", "candidate_count",
              "flags", "quality", "valid")
    if len(expected_rows) != len(actual_rows):
        _mismatch(mismatches, None, "row_count", len(expected_rows), len(actual_rows), max_items=max_mismatches)
    for index, (left, right) in enumerate(zip(expected_rows, actual_rows)):
        for field in fields:
            _mismatch(mismatches, index, field, getattr(left, field), getattr(right, field), max_items=max_mismatches)
        expected_diag = int(left.too_narrow) | (int(left.roi_excluded) << 1) | (int(left.line_bad) << 2) | (int(left.overflow) << 3)
        _mismatch(mismatches, index, "diagnostics", expected_diag, right.diagnostics, max_items=max_mismatches)
    return ReferenceComparison(not mismatches and len(expected_rows) == len(actual_rows), min(len(expected_rows), len(actual_rows)), tuple(mismatches), fields + ("diagnostics",))


def compare_profile(
    expected: Iterable[RowResult],
    profile: Profile,
    *,
    max_mismatches: int = 128,
) -> ReferenceComparison:
    """Compare SLP2 fields against rows computed from the same gray frame."""

    expected_rows = tuple(expected)
    actual_points = profile.points
    mismatches: list[ReferenceMismatch] = []
    fields = ("row", "xc_fixed", "quality", "valid", "flags")
    if len(expected_rows) != len(actual_points):
        _mismatch(mismatches, None, "point_count", len(expected_rows), len(actual_points), max_items=max_mismatches)
    for index, (left, right) in enumerate(zip(expected_rows, actual_points)):
        for field in fields:
            _mismatch(mismatches, index, field, getattr(left, field), getattr(right, field), max_items=max_mismatches)
    return ReferenceComparison(not mismatches and len(expected_rows) == len(actual_points), min(len(expected_rows), len(actual_points)), tuple(mismatches), fields)


def compare_same_frame(
    frame: GrayFrame,
    profile: Profile | bytes | bytearray | memoryview,
    *,
    diagnostics: DiagnosticsSnapshot | Iterable[DiagnosticRow] | None = None,
    threshold: int = 30,
    roi: tuple[int, int, int, int] | None = None,
    min_width: int = 1,
    max_width: int = 32,
    min_sum: int = 30,
    min_peak: int = 15,
    max_mismatches: int = 128,
) -> ReferenceComparison:
    """Recompute the integer ROW oracle over *frame* and compare one Profile.

    ``profile`` and the optional diagnostic rows must originate from the same
    capture.  This function deliberately does not infer or repair frame IDs.
    """

    if isinstance(profile, (bytes, bytearray, memoryview)):
        try:
            decoded = decode_profile(profile, frame_width=frame.width, frame_height=frame.height)
        except ProfileError as exc:
            raise ProtocolError(f"cannot decode Profile for same-frame comparison: {exc}") from exc
    elif isinstance(profile, Profile):
        decoded = profile
    else:
        raise ProtocolError("profile must be a Profile or SLP2 bytes")
    rows = reference_rows(
        frame, threshold=threshold, roi=roi, min_width=min_width,
        max_width=max_width, min_sum=min_sum, min_peak=min_peak,
    )
    profile_check = compare_profile(rows, decoded, max_mismatches=max_mismatches)
    if diagnostics is None:
        return profile_check
    diag_rows = diagnostics.rows if isinstance(diagnostics, DiagnosticsSnapshot) else tuple(diagnostics)
    diag_check = compare_rows(rows, diag_rows, max_mismatches=max_mismatches)
    merged = tuple(profile_check.mismatches) + tuple(diag_check.mismatches)
    return ReferenceComparison(
        passed=profile_check.passed and diag_check.passed,
        checked_rows=min(profile_check.checked_rows, diag_check.checked_rows),
        mismatches=merged[:max_mismatches],
        compared_fields=tuple(dict.fromkeys(profile_check.compared_fields + diag_check.compared_fields)),
    )


__all__ = ["compare_rows", "compare_profile", "compare_same_frame"]
