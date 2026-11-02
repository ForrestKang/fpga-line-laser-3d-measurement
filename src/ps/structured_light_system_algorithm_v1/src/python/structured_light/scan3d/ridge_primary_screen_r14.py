"""Opt-in direct consumer of existing PL decisions for primary-only SLC2.

This changes no hardware, wire contract, quality status, or default consumer.
Multi-component/fault rows keep the admitted R13 consumer and Raw fallback.
"""
from dataclasses import replace

import numpy as np

from ..stripe_v1.selection_v4 import SelectionConfigV4
from ..stripe_v1.stripe_fast import StripeRows, supports
from .ridge_candidates import select_ridge_candidates
from .stream_ridge import RidgePacket


_CLASS_REASONS = np.asarray([next((reason for bit, reason in (
    (16, 'overflow'), (4, 'saturated_support'), (8, 'roi_truncated'),
    (1, 'candidate_width'), (2, 'insufficient_contrast')) if flags & bit), None)
    for flags in range(32)], dtype=object)
_CLASS_REASONS.setflags(write=False)


def select_ridge_primary_candidates(packet, gray, config, selection_v4):
    """Return exact R13 rows/primary mask; bypass general run machinery only.

    Identity and immutable-payload admission remain the caller's responsibility,
    exactly as for ``select_ridge_candidates``. Unsupported or fallback inputs
    delegate to that consumer, including its explicit configuration errors.
    """
    if (not isinstance(packet, RidgePacket)
            or not isinstance(gray, np.ndarray) or gray.dtype != np.uint8
            or gray.shape != (packet.configuration.height, packet.configuration.width)
            or not isinstance(selection_v4, SelectionConfigV4)
            or selection_v4.min_contrast_dn != 20 or selection_v4.min_edge_drop_dn != 8
            or not supports(config, selection_v4.legacy)
            or not packet.configuration.matches(replace(config, saturation_level=255))):
        return select_ridge_candidates(packet, gray, config, selection_v4)
    center, flags, _, fallback, _, first, last, count, _, rawpeak, _, _ = packet.columns
    if np.any(fallback) or np.any(count > 1):
        return select_ridge_candidates(packet, gray, config, selection_v4)

    height = gray.shape[0]
    _, y0, _, y1 = config.roi
    primary = np.zeros(height, bool)
    primary[y0:y1] = True
    reasons = np.full(height, 'roi_excluded', dtype=object)
    # PSF1 width/contrast/ROI/overflow are already computed in PL. Its DN255
    # saturation bit is adapted to the requested policy using admitted rawpeak.
    classified = (flags.astype(np.int64) & ~4) | ((rawpeak >= config.saturation_level).astype(np.int64) << 2)
    present = count == 1
    local_reasons = _CLASS_REASONS[classified]
    local_reasons[~present] = 'no_support'
    eligible = present & (classified == 0)
    kept = np.flatnonzero(eligible)
    policy = selection_v4.legacy.selection
    if len(kept):
        if policy.association_enabled:
            breaks = ((np.diff(kept) > policy.association_max_row_gap)
                | (np.abs(np.diff(center[kept].astype(np.int64))) > (policy.association_max_distance_px << 12)))
            segment = np.concatenate(([0], np.cumsum(breaks))).astype(np.intp)
        else:
            segment = np.arange(len(kept), dtype=np.intp)
        if policy.minimum_segment_rows is not None:
            sizes = np.diff(np.concatenate(([0], np.flatnonzero(np.diff(segment))+1, [len(kept)])))
            short = sizes[segment] < policy.minimum_segment_rows
            local_reasons[kept[short]] = 'insufficient_segment_support'
            kept, segment = kept[~short], segment[~short]
        if len(kept):
            ridge = packet.ridge_flags[kept]
            local_reasons[kept[(ridge & 4) == 0]] = 'ridge_shape'
            local_reasons[kept[(ridge & 2) == 0]] = 'ridge_contrast'
            local_reasons[kept[(ridge & 1) == 0]] = 'background_window_truncated'
            limit = selection_v4.max_segment_width_span_px
            passing = local_reasons[kept] == None  # noqa: E711 - object array
            if limit is not None and np.any(passing):
                segments = segment[passing]
                widths = last[kept[passing]].astype(np.int64)-first[kept[passing]]+1
                low = np.full(segment[-1]+1, np.iinfo(np.int64).max, dtype=np.int64)
                high = np.zeros(segment[-1]+1, dtype=np.int64)
                np.minimum.at(low, segments, widths)
                np.maximum.at(high, segments, widths)
                local_reasons[kept[passing][(high-low > limit)[segments]]] = 'segment_width_span'
    reasons[y0:y1] = local_reasons
    centers = np.zeros(height, np.int64)
    start = np.full(height, -1, np.int64)
    end = np.full(height, -1, np.int64)
    centers[y0+kept] = center[kept]
    start[y0+kept] = first[kept]
    end[y0+kept] = last[kept]
    retained = start >= 0
    accepted = retained & np.asarray([reason is None for reason in reasons])
    return StripeRows(centers, start, end, accepted, retained, tuple(reasons.tolist())), primary


__all__ = ['select_ridge_primary_candidates']
