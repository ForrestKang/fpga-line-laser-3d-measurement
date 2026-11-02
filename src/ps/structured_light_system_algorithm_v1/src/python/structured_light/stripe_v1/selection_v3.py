"""v3 selector: v2 row selection plus an opt-in ridge-isolation gate.

A laser stripe is a narrow ridge whose two horizontal flanks fall back to the
same local background. An object edge that happens to be bright enough to pass
the brightness/width rules usually has a very different intensity on the two
sides of its silhouette: one side faces dark background while the other faces
the lit body of the object, or a sloped surface that keeps brightening.

The gate compares the median gray level in a near flank on each side of the
source span and rejects the row when the two flank medians disagree by more
than an explicit, caller-supplied bound. It reads the raw gray frame; it never
rewrites the candidate, its source span, or its Q12 center. Every v2 row and
reason is preserved for comparison, and rows rejected here keep the original
candidate so the evidence of what was measured stays intact.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

import numpy as np

from .selection_v2 import (
    SelectionConfigV2,
    SelectedProfileV2,
    select_candidates_v2,
)
from .types import Candidate, CandidateFrame, StripeConfig


_VERSION = 3
BACKGROUND_ASYMMETRY = "background_asymmetry"
SEGMENT_WIDTH_SPAN = "segment_width_span"


class SelectionV3Error(ValueError):
    """Raised when a v3 request violates its contract."""


@dataclass(frozen=True, slots=True)
class SelectionConfigV3:
    """Immutable v3 policy wrapping a complete v2 policy.

    ``ridge_flank_px`` is the (inner, outer) horizontal offset from the source
    span where each flank median is measured. ``ridge_asym_max_dn`` enables the
    gate when set; ``None`` disables it and makes v3 identical to v2 except for
    the recorded ridge statistics.

    ``max_segment_width_span_px`` enables a segment-level width stationarity
    check: after the ridge gate, any segment whose accepted source widths span
    more than this many pixels is rejected wholesale. A laser stripe keeps a
    near-constant width along a smooth surface, while a straight object edge
    crossing image rows widens or narrows monotonically. This is a shape
    continuity prior, not a laser-identity proof; it is off by default.
    """

    version: int = _VERSION
    selection: SelectionConfigV2 = SelectionConfigV2()
    ridge_flank_px: tuple[int, int] = (4, 16)
    ridge_asym_max_dn: int | None = None
    max_segment_width_span_px: int | None = None

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != _VERSION:
            raise SelectionV3Error(f"selection version must be {_VERSION}")
        if not isinstance(self.selection, SelectionConfigV2):
            raise SelectionV3Error("selection must be a SelectionConfigV2")
        inner, outer = self.ridge_flank_px
        if not (type(inner) is int and type(outer) is int):
            raise SelectionV3Error("ridge_flank_px must contain two integers")
        if not (1 <= inner < outer):
            raise SelectionV3Error("ridge_flank_px must satisfy 1 <= inner < outer")
        if self.ridge_asym_max_dn is not None:
            if type(self.ridge_asym_max_dn) is not int or self.ridge_asym_max_dn < 0:
                raise SelectionV3Error("ridge_asym_max_dn must be a non-negative integer")
        if self.max_segment_width_span_px is not None:
            if type(self.max_segment_width_span_px) is not int or self.max_segment_width_span_px < 0:
                raise SelectionV3Error("max_segment_width_span_px must be a non-negative integer")
            if not self.selection.association_enabled:
                raise SelectionV3Error(
                    "max_segment_width_span_px requires association bounds so that segments exist"
                )

    @property
    def gate_enabled(self) -> bool:
        return self.ridge_asym_max_dn is not None

    @property
    def segment_gate_enabled(self) -> bool:
        return self.max_segment_width_span_px is not None

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SelectedRowV3:
    row: int
    candidate: Candidate | None
    reason: str | None
    candidate_indices: tuple[int, ...]
    eligible_indices: tuple[int, ...]
    competitor_indices: tuple[int, ...]
    segment_id: int | None
    selection_method: str | None
    ridge_asym: int | None
    ridge_crest: int | None
    v2_reason: str | None


@dataclass(frozen=True, slots=True)
class SelectedProfileV3:
    width: int
    height: int
    rows: tuple[SelectedRowV3, ...]
    version: int
    selection_fingerprint: str
    v2_selection_fingerprint: str


def _flank_median(row: np.ndarray, start: int, end: int, inner: int, outer: int,
                  side: str) -> float | None:
    if side == "left":
        lo, hi = max(0, start - outer), max(0, start - inner)
    else:
        lo, hi = min(row.size, end + 1 + inner), min(row.size, end + 1 + outer)
    if hi <= lo:
        return None
    return float(np.median(row[lo:hi]))


def _ridge_values(row: np.ndarray, start: int, end: int, inner: int, outer: int) -> tuple[int | None, int | None]:
    left = _flank_median(row, start, end, inner, outer, "left")
    right = _flank_median(row, start, end, inner, outer, "right")
    if left is None or right is None:
        return None, None
    peak = int(row[start : end + 1].max())
    asym = int(round(abs(left - right)))
    crest = int(max(0, peak - max(left, right)))
    return asym, crest


def _validate_gray(gray: object, frame: CandidateFrame) -> np.ndarray:
    image = np.asarray(gray)
    if image.ndim != 2 or image.shape != (frame.height, frame.width):
        raise SelectionV3Error("gray must be a 2D array matching the candidate frame")
    if image.dtype != np.uint8:
        raise SelectionV3Error("gray dtype must be uint8")
    return image


def select_candidates_v3(gray: np.ndarray, frame: CandidateFrame, config: StripeConfig,
                         selection_config: SelectionConfigV3) -> SelectedProfileV3:
    """Run v2 unchanged, then apply the optional ridge and segment-width gates."""

    if not isinstance(selection_config, SelectionConfigV3):
        raise SelectionV3Error("selection_config must be a SelectionConfigV3")
    image = _validate_gray(gray, frame)
    base: SelectedProfileV2 = select_candidates_v2(frame, config, selection_config.selection)
    inner, outer = selection_config.ridge_flank_px
    limit = selection_config.ridge_asym_max_dn

    rows: list[SelectedRowV3] = []
    for source in base.rows:
        asym: int | None = None
        crest: int | None = None
        candidate = source.candidate
        reason = source.reason
        method = source.selection_method
        if candidate is not None:
            asym, crest = _ridge_values(image[source.row], candidate.start, candidate.end, inner, outer)
            if limit is not None and asym is not None and asym > limit:
                reason = BACKGROUND_ASYMMETRY
                method = None
        rows.append(SelectedRowV3(source.row, candidate, reason, source.candidate_indices,
                                  source.eligible_indices, source.competitor_indices,
                                  source.segment_id, method, asym, crest, source.reason))

    span_limit = selection_config.max_segment_width_span_px
    if span_limit is not None:
        spans: dict[int, int] = {}
        for row in rows:
            if row.candidate is None or row.reason is not None or row.segment_id is None:
                continue
            width = row.candidate.width
            low, high = spans.get(row.segment_id, (width, width))
            spans[row.segment_id] = (min(low, width), max(high, width))
        rejected = {
            segment_id for segment_id, (low, high) in spans.items() if high - low > span_limit
        }
        rows = [
            SelectedRowV3(row.row, row.candidate, SEGMENT_WIDTH_SPAN, row.candidate_indices,
                          row.eligible_indices, row.competitor_indices, row.segment_id, None,
                          row.ridge_asym, row.ridge_crest, row.v2_reason)
            if row.candidate is not None and row.reason is None and row.segment_id in rejected
            else row
            for row in rows
        ]

    return SelectedProfileV3(
        frame.width,
        frame.height,
        tuple(rows),
        selection_config.version,
        selection_config.fingerprint,
        base.selection_fingerprint,
    )


__all__ = [
    "BACKGROUND_ASYMMETRY",
    "SEGMENT_WIDTH_SPAN",
    "SelectedProfileV3",
    "SelectedRowV3",
    "SelectionConfigV3",
    "SelectionV3Error",
    "select_candidates_v3",
]
