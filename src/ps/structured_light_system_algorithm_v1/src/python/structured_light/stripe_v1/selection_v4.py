"""Explicit offline V4 ridge policy with a fixed-window integer reference.

V2 extraction/selection remains authoritative for candidate identity and safety
reasons. V3 outcomes and original Q12 centers are retained. This policy adds no
geometry authority: even accepted rows are pixel-preview candidates only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json

import numpy as np

from .selection_v2 import select_candidates_v2
from .selection_v3 import SelectionConfigV3, select_candidates_v3
from .types import Candidate, CandidateFrame, StripeConfig


BACKGROUND_WINDOW_TRUNCATED = 'background_window_truncated'
RIDGE_CONTRAST = 'ridge_contrast'
RIDGE_SHAPE = 'ridge_shape'
SEGMENT_WIDTH_SPAN = 'segment_width_span'
FLANK_INNER = 4
FLANK_OUTER = 11
FLANK_COUNT = 8


class SelectionV4Error(ValueError):
    """A V4 request violates the explicit policy contract."""


@dataclass(frozen=True, slots=True)
class SelectionConfigV4:
    version: int = 4
    legacy: SelectionConfigV3 = SelectionConfigV3()
    min_contrast_dn: int = 20
    min_edge_drop_dn: int = 8
    max_segment_width_span_px: int | None = 12

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 4:
            raise SelectionV4Error('selection version must be 4')
        if not isinstance(self.legacy, SelectionConfigV3):
            raise SelectionV4Error('legacy must be a SelectionConfigV3')
        for name in ('min_contrast_dn', 'min_edge_drop_dn'):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 255:
                raise SelectionV4Error(f'{name} must be an integer in [0, 255]')
        limit = self.max_segment_width_span_px
        if limit is not None and (type(limit) is not int or limit < 0):
            raise SelectionV4Error('max_segment_width_span_px must be None or a non-negative integer')

    @property
    def fingerprint(self) -> str:
        payload = {'policy': asdict(self), 'flank_inner': FLANK_INNER,
                   'flank_outer': FLANK_OUTER, 'center_policy': 'original_q12',
                   'background_model': 'two_fixed_8px_mean_integer_v1'}
        return hashlib.sha256(json.dumps(payload, sort_keys=True,
            separators=(',', ':')).encode('utf-8')).hexdigest()


@dataclass(frozen=True, slots=True)
class RidgeIntegerValues:
    window_complete: bool
    left_sum: int | None
    right_sum: int | None
    contrast8: int | None
    left_drop_dn: int | None
    right_drop_dn: int | None
    corrected_center_q12: int | None
    reason: str | None


def ridge_integer_values(row: np.ndarray, start: int, end: int, x0: int, x1: int,
                         min_contrast_dn: int = 20,
                         min_edge_drop_dn: int = 8) -> RidgeIntegerValues:
    """Bit-exact integer reference; all divisions floor non-negative values.

    L=[start-11,start-4], R=[end+4,end+11], inclusive. Both windows
    must fit the ROI; pixels outside it never contribute. Background-corrected
    center is an uncalibrated diagnostic and is never substituted for Q12.
    """
    if not isinstance(row, np.ndarray) or row.ndim != 1 or row.dtype != np.uint8:
        raise SelectionV4Error('row must be a one-dimensional uint8 array')
    if any(type(v) is not int for v in (start, end, x0, x1)):
        raise SelectionV4Error('support and ROI coordinates must be integers')
    if not (0 <= x0 <= start <= end < x1 <= row.size):
        raise SelectionV4Error('support must be within the row ROI')
    for value in (min_contrast_dn, min_edge_drop_dn):
        if type(value) is not int or not 0 <= value <= 255:
            raise SelectionV4Error('ridge thresholds must be integers in [0, 255]')
    if start - FLANK_OUTER < x0 or end + FLANK_OUTER >= x1:
        return RidgeIntegerValues(False, None, None, None, None, None, None,
                                  BACKGROUND_WINDOW_TRUNCATED)
    left = sum(int(v) for v in row[start-11:start-3])
    right = sum(int(v) for v in row[end+4:end+12])
    peak = int(row[start:end+1].max())
    contrast8 = 8 * peak - max(left, right)
    left_drop, right_drop = peak-int(row[start]), peak-int(row[end])
    reason = None
    if contrast8 < 8 * min_contrast_dn:
        reason = RIDGE_CONTRAST
    elif (end-start+1 < 3 or left_drop < min_edge_drop_dn
          or right_drop < min_edge_drop_dn
          or int(row[start+1:end].max(initial=0)) != peak):
        reason = RIDGE_SHAPE

    # Window center coordinates multiplied by two avoid half-pixel arithmetic.
    # The common 8*distance scale cancels in the centroid ratio.
    left_x2, right_x2 = 2*start-15, 2*end+15
    distance2 = right_x2-left_x2
    weight_sum, moment_sum = 0, 0
    for x in range(start, end+1):
        background_num = left*(right_x2-2*x) + right*(2*x-left_x2)
        weight = max(0, 8*distance2*int(row[x])-background_num)
        weight_sum += weight
        moment_sum += x*weight
    corrected = (moment_sum << 12)//weight_sum if weight_sum else None
    return RidgeIntegerValues(True, left, right, contrast8, left_drop, right_drop,
                              corrected, reason)


@dataclass(frozen=True, slots=True)
class SelectedRowV4:
    row: int
    candidate: Candidate | None
    reason: str | None
    candidate_indices: tuple[int, ...]
    eligible_indices: tuple[int, ...]
    competitor_indices: tuple[int, ...]
    segment_id: int | None
    selection_method: str | None
    v2_reason: str | None
    v3_reason: str | None
    ridge: RidgeIntegerValues | None
    corrected_center_q12: int | None
    center_shift_q12: int | None
    center_calibration_status: str = 'uncalibrated_diagnostic'


@dataclass(frozen=True, slots=True)
class SelectedProfileV4:
    width: int
    height: int
    rows: tuple[SelectedRowV4, ...]
    version: int
    selection_fingerprint: str
    v2_selection_fingerprint: str
    v3_selection_fingerprint: str
    measurement_eligible: bool = False
    center_policy: str = 'original_q12'


def select_candidates_v4(gray: np.ndarray, frame: CandidateFrame, config: StripeConfig,
                         selection_config: SelectionConfigV4) -> SelectedProfileV4:
    """Run V2 unchanged, add V4 ridge gates, and retain full V3 comparison."""
    if not isinstance(selection_config, SelectionConfigV4):
        raise SelectionV4Error('selection_config must be a SelectionConfigV4')
    image = np.asarray(gray)
    if image.dtype != np.uint8 or image.shape != (frame.height, frame.width):
        raise SelectionV4Error('gray must be uint8 and match candidate-frame geometry')
    base = select_candidates_v2(frame, config, selection_config.legacy.selection)
    legacy = select_candidates_v3(image, frame, config, selection_config.legacy)
    x0, _, x1, _ = config.roi or (0, 0, frame.width, frame.height)
    rows = []
    for source, old in zip(base.rows, legacy.rows):
        candidate = source.candidate
        values, corrected, shift = None, None, None
        reason = source.reason
        if candidate is not None:
            values = ridge_integer_values(image[source.row], candidate.start, candidate.end,
                x0, x1, selection_config.min_contrast_dn, selection_config.min_edge_drop_dn)
            corrected = values.corrected_center_q12
            shift = corrected-candidate.center_q12 if corrected is not None else None
            if reason is None:
                reason = values.reason
        rows.append(SelectedRowV4(source.row, candidate, reason, source.candidate_indices,
            source.eligible_indices, source.competitor_indices, source.segment_id,
            source.selection_method if reason is None else None, source.reason, old.reason,
            values, corrected, shift))
    limit = selection_config.max_segment_width_span_px
    if limit is not None:
        spans = {}
        for row in rows:
            if row.candidate is not None and row.reason is None and row.segment_id is not None:
                width = row.candidate.width
                lo, hi = spans.get(row.segment_id, (width, width))
                spans[row.segment_id] = min(lo, width), max(hi, width)
        rejected = {segment for segment, (lo, hi) in spans.items() if hi-lo > limit}
        rows = [replace(r, reason=SEGMENT_WIDTH_SPAN, selection_method=None)
                if r.reason is None and r.segment_id in rejected else r for r in rows]
    return SelectedProfileV4(frame.width, frame.height, tuple(rows), 4,
        selection_config.fingerprint, base.selection_fingerprint, legacy.selection_fingerprint)


__all__ = ['SelectionConfigV4', 'SelectionV4Error', 'SelectedProfileV4', 'SelectedRowV4',
           'RidgeIntegerValues', 'ridge_integer_values', 'select_candidates_v4']
