"""Conservative candidate selection with optional bounded two-anchor association.

This module is an independent offline candidate selector. It preserves source
coordinates and Q12 centroids from extraction; it does not interpolate,
smooth, score brightness, or claim that a selected candidate is an optical
surface measurement.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json

from .types import Candidate, CandidateFrame, CandidateRow, StripeConfig


Q12 = 1 << 12
S0_MAX = (1 << 18) - 1
S1_MAX = (1 << 27) - 1
XC_MAX = (1 << 22) - 1
_VERSION = 2
_REASONS = {
    "candidate_width",
    "insufficient_contrast",
    "saturated_support",
    "roi_truncated",
    "overflow",
}
_REASON_PRIORITY = (
    "overflow",
    "saturated_support",
    "roi_truncated",
    "candidate_width",
    "insufficient_contrast",
)


class SelectionV2Error(ValueError):
    """Raised when a frame or selector configuration violates the v2 contract."""


@dataclass(frozen=True, slots=True)
class SelectionConfigV2:
    """Immutable v2 policy; association and segment support are opt-in."""

    version: int = _VERSION
    association_max_distance_px: int | None = None
    association_max_row_gap: int | None = None
    minimum_segment_rows: int | None = None

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != _VERSION:
            raise SelectionV2Error(f"selection version must be {_VERSION}")
        distance = self.association_max_distance_px
        row_gap = self.association_max_row_gap
        if (distance is None) != (row_gap is None):
            raise SelectionV2Error("association distance and row gap must be configured together")
        if distance is not None and (type(distance) is not int or distance < 0):
            raise SelectionV2Error("association_max_distance_px must be a non-negative integer")
        if row_gap is not None and (type(row_gap) is not int or row_gap < 1):
            raise SelectionV2Error("association_max_row_gap must be a positive integer")
        if self.minimum_segment_rows is not None:
            if distance is None or row_gap is None:
                raise SelectionV2Error("minimum_segment_rows requires explicit association bounds")
            if type(self.minimum_segment_rows) is not int or self.minimum_segment_rows < 2:
                raise SelectionV2Error("minimum_segment_rows must be an integer >= 2")

    @property
    def association_enabled(self) -> bool:
        return self.association_max_distance_px is not None

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SelectedRowV2:
    row: int
    candidate: Candidate | None
    reason: str | None
    candidate_indices: tuple[int, ...]
    eligible_indices: tuple[int, ...]
    competitor_indices: tuple[int, ...]
    segment_id: int | None
    selection_method: str | None


@dataclass(frozen=True, slots=True)
class SelectedProfileV2:
    width: int
    height: int
    rows: tuple[SelectedRowV2, ...]
    version: int
    selection_fingerprint: str


def _need_int(name: str, value: object, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise SelectionV2Error(f"{name} must be an integer >= {minimum}")
    return value


def _validate_selection_config(selection: object) -> SelectionConfigV2:
    if not isinstance(selection, SelectionConfigV2):
        raise SelectionV2Error("selection_config must be a SelectionConfigV2")
    if type(selection.version) is not int or selection.version != _VERSION:
        raise SelectionV2Error(f"selection version must be {_VERSION}")
    distance = selection.association_max_distance_px
    row_gap = selection.association_max_row_gap
    if (distance is None) != (row_gap is None):
        raise SelectionV2Error("association distance and row gap must be configured together")
    if distance is not None:
        _need_int("association_max_distance_px", distance, 0)
        _need_int("association_max_row_gap", row_gap, 1)
    if selection.minimum_segment_rows is not None:
        if distance is None or row_gap is None:
            raise SelectionV2Error("minimum_segment_rows requires explicit association bounds")
        _need_int("minimum_segment_rows", selection.minimum_segment_rows, 2)
    return selection


def _validate_candidate(candidate: object, row_index: int, index: int, frame: CandidateFrame,
                        config: StripeConfig) -> Candidate:
    if not isinstance(candidate, Candidate):
        raise SelectionV2Error(f"candidate row {row_index} contains a non-Candidate value")
    if _need_int("candidate.row", candidate.row, 0) != row_index:
        raise SelectionV2Error("candidate row does not match its containing source row")
    if _need_int("candidate.index", candidate.index, 0) != index:
        raise SelectionV2Error("candidate indices must be contiguous and retained in source order")

    start = _need_int("candidate.start", candidate.start, 0)
    end = _need_int("candidate.end", candidate.end, 0)
    width = _need_int("candidate.width", candidate.width, 1)
    if end < start or width != end - start + 1:
        raise SelectionV2Error("candidate source span and width are inconsistent")
    x0, y0, x1, y1 = config.roi or (0, 0, frame.width, frame.height)
    if row_index < y0 or row_index >= y1 or start < x0 or end >= x1 or end >= frame.width:
        raise SelectionV2Error("candidate lies outside the configured source-coordinate ROI")

    for name in ("s0", "s1", "center_q12", "peak", "saturated_count", "contrast"):
        _need_int(f"candidate.{name}", getattr(candidate, name), 0)
    if candidate.peak > 255 or candidate.contrast > 255:
        raise SelectionV2Error("candidate peak and contrast must be gray8 values")
    if candidate.saturated_count > width:
        raise SelectionV2Error("candidate saturated_count cannot exceed candidate width")
    if not (start * Q12 <= candidate.center_q12 <= (end + 1) * Q12 - 1):
        raise SelectionV2Error("candidate Q12 center must stay within its source span")
    for name in ("background", "noise_mad"):
        value = getattr(candidate, name)
        if value is not None and (type(value) is not int or value < 0 or value > 255):
            raise SelectionV2Error(f"candidate.{name} must be None or a gray8 integer")
    if type(candidate.reasons) is not tuple or any(
        type(reason) is not str or reason not in _REASONS for reason in candidate.reasons
    ):
        raise SelectionV2Error("candidate reasons contain an unknown or malformed rejection code")
    if len(candidate.reasons) != len(set(candidate.reasons)):
        raise SelectionV2Error("candidate reasons must not contain duplicates")
    return candidate


def _validate_frame(frame: object, config: object) -> tuple[CandidateFrame, StripeConfig]:
    if not isinstance(frame, CandidateFrame):
        raise SelectionV2Error("frame must be a CandidateFrame")
    if not isinstance(config, StripeConfig):
        raise SelectionV2Error("config must be a StripeConfig")
    width = _need_int("frame.width", frame.width, 1)
    height = _need_int("frame.height", frame.height, 1)
    if type(frame.threshold_mode) is not str or frame.threshold_mode != config.threshold_mode:
        raise SelectionV2Error("threshold mode does not match the extraction configuration")
    if type(frame.config_fingerprint) is not str or frame.config_fingerprint != config.fingerprint:
        raise SelectionV2Error("configuration fingerprint does not match extracted candidates")
    if type(frame.rows) is not tuple or len(frame.rows) != height:
        raise SelectionV2Error("candidate rows must cover the frame in source row order")

    x0, y0, x1, y1 = config.roi or (0, 0, width, height)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise SelectionV2Error("configured ROI must lie within the candidate frame")
    for y, row in enumerate(frame.rows):
        if not isinstance(row, CandidateRow) or type(row.row) is not int or row.row != y:
            raise SelectionV2Error("candidate rows must cover the frame in source row order")
        if type(row.overflow) is not bool or type(row.excluded) is not bool:
            raise SelectionV2Error("candidate row overflow and excluded flags must be booleans")
        if type(row.candidates) is not tuple:
            raise SelectionV2Error("candidate lists must be immutable tuples")
        expected_excluded = y < y0 or y >= y1
        if row.excluded != expected_excluded:
            raise SelectionV2Error("candidate row exclusion does not match the configured y ROI")
        if expected_excluded and (row.candidates or row.overflow):
            raise SelectionV2Error("excluded ROI rows cannot contain candidates or overflow")
        for index, candidate in enumerate(row.candidates):
            _validate_candidate(candidate, y, index, frame, config)
    return frame, config


def _candidate_reasons(candidate: Candidate, config: StripeConfig, x0: int, x1: int) -> set[str]:
    reasons = set(candidate.reasons)
    if candidate.width < config.min_width or candidate.width > config.max_width:
        reasons.add("candidate_width")
    if candidate.s0 < max(1, config.min_sum) or candidate.peak < max(1, config.min_peak):
        reasons.add("insufficient_contrast")
    if candidate.saturated_count > 0:
        reasons.add("saturated_support")
    if candidate.start == x0 or candidate.end == x1 - 1:
        reasons.add("roi_truncated")
    if candidate.s0 > S0_MAX or candidate.s1 > S1_MAX or candidate.center_q12 > XC_MAX:
        reasons.add("overflow")
    return reasons


def _first_reason(reason_sets: tuple[set[str], ...]) -> str:
    for reason in _REASON_PRIORITY:
        if any(reason in reasons for reasons in reason_sets):
            return reason
    return "insufficient_contrast"


def _select_rows(frame: CandidateFrame, config: StripeConfig,
                 selection: SelectionConfigV2) -> list[SelectedRowV2]:
    x0, _y0, x1, _y1 = config.roi or (0, 0, frame.width, frame.height)
    rows: list[SelectedRowV2] = []

    for source_row in frame.rows:
        all_indices = tuple(candidate.index for candidate in source_row.candidates)
        if source_row.excluded:
            rows.append(SelectedRowV2(source_row.row, None, "roi_excluded", all_indices,
                                      (), all_indices, None, None))
            continue
        if source_row.overflow or len(source_row.candidates) > config.max_candidates_per_row:
            rows.append(SelectedRowV2(source_row.row, None, "candidate_overflow", all_indices,
                                      (), all_indices, None, None))
            continue
        if not source_row.candidates:
            rows.append(SelectedRowV2(source_row.row, None, "no_support", (), (), (), None, None))
            continue

        reasons = tuple(
            _candidate_reasons(candidate, config, x0, x1)
            for candidate in source_row.candidates
        )
        eligible = tuple(
            candidate
            for candidate, candidate_reasons in zip(source_row.candidates, reasons)
            if not candidate_reasons
        )
        saturated_competitors = tuple(
            candidate
            for candidate, candidate_reasons in zip(source_row.candidates, reasons)
            if "saturated_support" in candidate_reasons
            and not (candidate_reasons - {"saturated_support"})
        )
        eligible_indices = tuple(candidate.index for candidate in eligible)

        if not eligible:
            rows.append(SelectedRowV2(source_row.row, None, _first_reason(reasons), all_indices,
                                      (), all_indices, None, None))
        elif saturated_competitors:
            rows.append(SelectedRowV2(source_row.row, None, "saturated_competitor", all_indices,
                                      eligible_indices, all_indices, None, None))
        elif len(eligible) > 1:
            rows.append(SelectedRowV2(source_row.row, None, "ambiguous_candidates", all_indices,
                                      eligible_indices, all_indices, None, None))
        else:
            candidate = eligible[0]
            competitors = tuple(index for index in all_indices if index != candidate.index)
            rows.append(SelectedRowV2(source_row.row, candidate, None, all_indices,
                                      eligible_indices, competitors, None, "unique"))

    if not selection.association_enabled:
        return rows

    max_distance_px = selection.association_max_distance_px
    row_gap = selection.association_max_row_gap
    if max_distance_px is None or row_gap is None:
        raise SelectionV2Error("association requires both explicit bounds")
    max_distance_q12 = max_distance_px * Q12
    anchors = {
        row.row: row.candidate
        for row in rows
        if row.selection_method == "unique" and row.candidate is not None
    }
    for index, row in enumerate(rows):
        if row.reason != "ambiguous_candidates" or len(row.eligible_indices) < 2:
            continue
        left_y = next(
            (candidate_y for candidate_y in range(row.row - 1, max(-1, row.row - row_gap - 1), -1)
             if candidate_y in anchors),
            None,
        )
        right_y = next(
            (candidate_y for candidate_y in range(row.row + 1, min(frame.height, row.row + row_gap + 1))
             if candidate_y in anchors),
            None,
        )
        if left_y is None or right_y is None:
            continue
        left_anchor = anchors[left_y]
        right_anchor = anchors[right_y]
        candidates = {
            candidate.index: candidate
            for candidate in frame.rows[row.row].candidates
            if candidate.index in row.eligible_indices
        }
        matches = tuple(
            candidate
            for candidate in candidates.values()
            if abs(candidate.center_q12 - left_anchor.center_q12) <= max_distance_q12
            and abs(candidate.center_q12 - right_anchor.center_q12) <= max_distance_q12
        )
        if len(matches) != 1:
            continue
        candidate = matches[0]
        competitors = tuple(index for index in row.candidate_indices if index != candidate.index)
        rows[index] = replace(row, candidate=candidate, reason=None,
                              competitor_indices=competitors, selection_method="association")
    return rows


def _assign_segments(rows: list[SelectedRowV2], selection: SelectionConfigV2) -> list[SelectedRowV2]:
    segment_id = 0
    previous: SelectedRowV2 | None = None
    output: list[SelectedRowV2] = []
    max_distance_px = selection.association_max_distance_px
    max_row_gap = selection.association_max_row_gap
    for row in rows:
        if row.candidate is None:
            output.append(row)
            continue
        can_continue = False
        if selection.association_enabled and previous is not None:
            if max_distance_px is None or max_row_gap is None:
                raise SelectionV2Error("association requires both explicit bounds")
            can_continue = (
                row.row - previous.row <= max_row_gap
                and abs(row.candidate.center_q12 - previous.candidate.center_q12)
                <= max_distance_px * Q12
            )
        if not can_continue:
            segment_id += 1
        updated = replace(row, segment_id=segment_id)
        output.append(updated)
        previous = updated
    return output


def _apply_minimum_segment_support(rows: list[SelectedRowV2],
                                   minimum_rows: int | None) -> list[SelectedRowV2]:
    if minimum_rows is None:
        return rows
    segment_sizes: dict[int, int] = {}
    for row in rows:
        if row.candidate is not None and row.segment_id is not None:
            segment_sizes[row.segment_id] = segment_sizes.get(row.segment_id, 0) + 1

    return [
        replace(row, candidate=None, reason="insufficient_segment_support", selection_method=None)
        if row.candidate is not None
        and row.segment_id is not None
        and segment_sizes[row.segment_id] < minimum_rows
        else row
        for row in rows
    ]


def select_candidates_v2(frame: CandidateFrame, config: StripeConfig,
                         selection_config: SelectionConfigV2) -> SelectedProfileV2:
    """Select conservative row candidates with optional two-sided association.

    Ambiguous rows remain absent unless exactly one eligible candidate lies
    within both configured distance bounds of the nearest clean singleton
    anchors on its left and right. Each such anchor is an independently
    eligible unsaturated singleton without a qualifying saturated competitor.
    When configured, minimum segment support removes selected rows from short
    segments after association and segment assignment; it never fills gaps.
    """

    _validate_selection_config(selection_config)
    frame, config = _validate_frame(frame, config)
    rows = _select_rows(frame, config, selection_config)
    rows = _assign_segments(rows, selection_config)
    rows = _apply_minimum_segment_support(rows, selection_config.minimum_segment_rows)
    return SelectedProfileV2(
        frame.width,
        frame.height,
        tuple(rows),
        selection_config.version,
        selection_config.fingerprint,
    )


__all__ = [
    "SelectedProfileV2",
    "SelectedRowV2",
    "SelectionConfigV2",
    "SelectionV2Error",
    "select_candidates_v2",
]
