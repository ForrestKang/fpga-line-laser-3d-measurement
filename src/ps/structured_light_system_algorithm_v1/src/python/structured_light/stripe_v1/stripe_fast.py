"""Vectorized stripe extraction + v2/v3 selection for the frozen board configuration.

Same result as ``extract_candidates`` -> ``select_candidates_v3`` (fixed
threshold, two-anchor association with row gap 1), computed with whole-array
NumPy operations instead of per-row Python objects. ``supports`` says whether
a configuration is covered; callers fall back to the reference otherwise.
The reference modules remain the specification; tests compare both paths.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .selection_v3 import SelectionConfigV3
from .types import StripeConfig

S0_MAX = (1 << 18) - 1
S1_MAX = (1 << 27) - 1
XC_MAX = (1 << 22) - 1
WIDTH, CONTRAST, SATURATED, TRUNCATED, OVERFLOW = 1, 2, 4, 8, 16
# Priority used when no candidate in a row is eligible (selection_v2._REASON_PRIORITY).
_PRIORITY = ((OVERFLOW, "overflow"), (SATURATED, "saturated_support"), (TRUNCATED, "roi_truncated"),
             (WIDTH, "candidate_width"), (CONTRAST, "insufficient_contrast"))


@dataclass(frozen=True)
class StripeRows:
    """One entry per image row (all frame rows, source order)."""

    center_q12: np.ndarray   # int64; 0 where the row keeps no candidate
    start: np.ndarray        # int64 source column of the kept candidate, -1 if none
    end: np.ndarray          # int64
    accepted: np.ndarray     # bool: final stripe point
    retained: np.ndarray     # bool: candidate kept (accepted, or rejected by a v3 gate)
    reasons: tuple           # str | None per row


def supports(config: StripeConfig, selection: SelectionConfigV3) -> bool:
    policy = selection.selection
    return (config.threshold_mode == "fixed" and config.roi is not None
            and policy.association_max_row_gap in (None, 1)
            and (policy.minimum_segment_rows is None or policy.association_enabled))


def select_stripe(gray: np.ndarray, config: StripeConfig, selection: SelectionConfigV3) -> StripeRows:
    if not supports(config, selection):
        raise ValueError("configuration is outside the vectorized fast path")
    x0, y0, x1, y1 = config.roi
    vectors = extract_fixed_run_features(gray[y0:y1, x0:x1], config, x0=x0)
    return select_run_features(gray, config, selection, *vectors)


def extract_fixed_run_features(sub: np.ndarray, config: StripeConfig, *, x0: int):
    """Exact fixed-threshold run vectors for a batch of source ROI rows.

    Row indices address this compact batch; a caller gathering fallback rows
    restores their original row mapping before applying selection policies.
    """
    rows_n, cols = sub.shape
    weight = sub.astype(np.int64) - config.fixed_threshold
    np.maximum(weight, 0, out=weight)

    # Runs of positive weight per row (row-major order).
    edges = np.diff(np.pad(weight > 0, ((0, 0), (1, 1))).astype(np.int8), axis=1)
    run_row, run_start = np.nonzero(edges == 1)
    _, run_stop = np.nonzero(edges == -1)          # exclusive end, same order as starts
    run_end = run_stop - 1
    zero = np.zeros((rows_n, 1), np.int64)
    columns = np.arange(x0, x0 + cols, dtype=np.int64)
    c_s0 = np.hstack((zero, np.cumsum(weight, axis=1)))
    c_s1 = np.hstack((zero, np.cumsum(weight * columns, axis=1)))
    c_sat = np.hstack((zero, np.cumsum(sub >= config.saturation_level, axis=1)))
    s0 = c_s0[run_row, run_stop] - c_s0[run_row, run_start]
    s1 = c_s1[run_row, run_stop] - c_s1[run_row, run_start]
    saturated = c_sat[run_row, run_stop] - c_sat[run_row, run_start]
    peak = (np.maximum.reduceat(weight.ravel(), run_row * cols + run_start)
            if len(run_row) else np.zeros(0, np.int64))
    center = (s1 << 12) // np.maximum(s0, 1)

    return run_row, run_start + x0, run_end + x0, s0, s1, peak, saturated, center


def select_run_features(gray, config, selection, run_row, start_abs, end_abs,
                        s0, s1, peak, saturated, center, *, classified_flags=None):
    """Apply unchanged fast v2/v3 gates to exact extracted run statistics."""
    if not supports(config, selection):
        raise ValueError("configuration is outside the vectorized fast path")
    height, width_px = gray.shape
    x0, y0, x1, y1 = config.roi
    rows_n, cols = y1 - y0, x1 - x0
    run_start, run_end = start_abs - x0, end_abs - x0
    run_width = run_end - run_start + 1
    if classified_flags is None:
        flags = np.zeros(len(run_row), np.int64)
        flags[(run_width < config.min_width) | (run_width > config.max_width)] |= WIDTH
        flags[(s0 < max(1, config.min_sum)) | (peak < max(1, config.min_peak))] |= CONTRAST
        flags[saturated > 0] |= SATURATED
        flags[(run_start == 0) | (run_end == cols - 1)] |= TRUNCATED
        flags[(s0 > S0_MAX) | (s1 > S1_MAX) | (center > XC_MAX)] |= OVERFLOW
    else:
        flags = np.asarray(classified_flags,dtype=np.int64)
        if flags.shape != run_row.shape or np.any(flags < 0) or np.any(flags > 31):
            raise ValueError('invalid preclassified run flags')

    count = np.bincount(run_row, minlength=rows_n)
    eligible = flags == 0
    n_eligible = np.bincount(run_row, weights=eligible, minlength=rows_n).astype(np.int64)
    n_sat_comp = np.bincount(run_row, weights=flags == SATURATED, minlength=rows_n).astype(np.int64)
    row_flags = np.zeros(rows_n, np.int64)
    np.bitwise_or.at(row_flags, run_row, flags)

    reason = np.full(rows_n, None, dtype=object)
    reason[count == 0] = "no_support"
    overflow_row = count > config.max_candidates_per_row
    reason[overflow_row] = "candidate_overflow"
    open_rows = (count > 0) & ~overflow_row
    none_eligible = open_rows & (n_eligible == 0)
    for bit, name in reversed(_PRIORITY):  # highest priority written last
        reason[none_eligible & ((row_flags & bit) != 0)] = name
    reason[none_eligible & (row_flags == 0)] = "insufficient_contrast"
    has_eligible = open_rows & (n_eligible > 0)
    reason[has_eligible & (n_sat_comp > 0)] = "saturated_competitor"
    ambiguous = has_eligible & (n_sat_comp == 0) & (n_eligible > 1)
    reason[ambiguous] = "ambiguous_candidates"
    unique = has_eligible & (n_sat_comp == 0) & (n_eligible == 1)

    chosen = np.full(rows_n, -1, np.int64)           # run index kept per row
    eligible_runs = np.flatnonzero(eligible)
    unique_runs = eligible_runs[unique[run_row[eligible_runs]]]
    chosen[run_row[unique_runs]] = unique_runs

    policy = selection.selection
    if policy.association_enabled:
        limit = policy.association_max_distance_px << 12
        anchor = unique.copy()
        left_ok = np.zeros(rows_n, bool)
        right_ok = np.zeros(rows_n, bool)
        left_ok[1:] = anchor[:-1]
        right_ok[:-1] = anchor[1:]
        candidate_rows = ambiguous & left_ok & right_ok
        runs = eligible_runs[candidate_rows[run_row[eligible_runs]]]
        if len(runs):
            r = run_row[runs]
            left_c = center[chosen[r - 1]]
            right_c = center[chosen[r + 1]]
            match = (np.abs(center[runs] - left_c) <= limit) & (np.abs(center[runs] - right_c) <= limit)
            matched = runs[match]
            n_match = np.bincount(run_row[matched], minlength=rows_n)
            single = matched[n_match[run_row[matched]] == 1]
            chosen[run_row[single]] = single
            reason[run_row[single]] = None

    accepted_rows = np.flatnonzero(chosen >= 0)
    # Segment IDs index rows, so use NumPy's native index width. On ARMv7,
    # bincount requires intp (32 bit); Q12 values and sums stay int64.
    segment = np.full(rows_n, -1, np.intp)
    if len(accepted_rows):
        if policy.association_enabled:
            c = center[chosen[accepted_rows]]
            breaks = ((np.diff(accepted_rows) > policy.association_max_row_gap)
                      | (np.abs(np.diff(c)) > (policy.association_max_distance_px << 12)))
            segment[accepted_rows] = np.concatenate(([0], np.cumsum(breaks)))
        else:
            segment[accepted_rows] = np.arange(len(accepted_rows))
        if policy.minimum_segment_rows is not None:
            sizes = np.bincount(segment[accepted_rows])
            short = accepted_rows[sizes[segment[accepted_rows]] < policy.minimum_segment_rows]
            chosen[short] = -1
            reason[short] = "insufficient_segment_support"

    kept = np.flatnonzero(chosen >= 0)
    start = np.full(height, -1, np.int64)
    end = np.full(height, -1, np.int64)
    centers = np.zeros(height, np.int64)
    if len(kept):
        start_abs = x0 + run_start[chosen[kept]]
        end_abs = x0 + run_end[chosen[kept]]
        if selection.ridge_asym_max_dn is not None:
            asym = _ridge_asymmetry(gray, y0 + kept, start_abs, end_abs, selection.ridge_flank_px)
            bad = np.isfinite(asym) & (asym > selection.ridge_asym_max_dn)
            reason[kept[bad]] = "background_asymmetry"
        if selection.max_segment_width_span_px is not None:
            ok = reason[kept] == None  # noqa: E711 - element-wise on an object array
            seg = segment[kept[ok]]
            widths = run_width[chosen[kept[ok]]]
            if len(seg):
                span = np.zeros(seg.max() + 1, np.int64)
                low = np.full(seg.max() + 1, np.iinfo(np.int64).max)
                np.maximum.at(span, seg, widths)
                np.minimum.at(low, seg, widths)
                wide = (span - low) > selection.max_segment_width_span_px
                reason[kept[ok][wide[seg]]] = "segment_width_span"
        start[y0 + kept] = start_abs
        end[y0 + kept] = end_abs
        centers[y0 + kept] = center[chosen[kept]]

    full_reason = np.full(height, "roi_excluded", dtype=object)
    full_reason[y0:y1] = reason
    retained = start >= 0
    accepted = retained & np.array([r is None for r in full_reason])
    return StripeRows(centers, start, end, accepted, retained, tuple(full_reason.tolist()))


def _ridge_asymmetry(gray: np.ndarray, rows: np.ndarray, start: np.ndarray, end: np.ndarray,
                     flank: tuple[int, int]) -> np.ndarray:
    """|median(left flank) - median(right flank)| rounded half-to-even, NaN if a flank is empty."""

    inner, outer = flank
    width = gray.shape[1]
    lo_l, hi_l = np.maximum(0, start - outer), np.maximum(0, start - inner)
    lo_r, hi_r = np.minimum(width, end + 1 + inner), np.minimum(width, end + 1 + outer)
    span = outer - inner
    result = np.full(len(rows), np.nan)
    full = (hi_l - lo_l == span) & (hi_r - lo_r == span)
    if full.any():
        offsets = np.arange(span)
        left = gray[rows[full, None], lo_l[full, None] + offsets]
        right = gray[rows[full, None], lo_r[full, None] + offsets]
        result[full] = np.round(np.abs(np.median(left, axis=1) - np.median(right, axis=1)))
    for i in np.flatnonzero(~full):  # flanks clipped by the image border
        if hi_l[i] > lo_l[i] and hi_r[i] > lo_r[i]:
            diff = abs(float(np.median(gray[rows[i], lo_l[i]:hi_l[i]]))
                       - float(np.median(gray[rows[i], lo_r[i]:hi_r[i]])))
            result[i] = round(diff)
    return result


__all__ = ["StripeRows", "select_stripe", "select_run_features", "extract_fixed_run_features", "supports"]
