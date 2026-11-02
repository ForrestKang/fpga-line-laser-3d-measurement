"""Consume admitted SLC2/RPG1 ridge facts with unchanged V2 association.

PL facts replace only fixed V4 ridge window/contrast/shape checks. Fallback
components retain row-local Raw extraction and the integer ridge reference.
No model/Raw QA oracle is called on the per-frame production path. Selected
Q12 centers stay original; selected pixels confer no metrology eligibility.
"""
from dataclasses import replace

import numpy as np

from ..stripe_v1.selection_v4 import (SelectionConfigV4, ridge_integer_values,
    BACKGROUND_WINDOW_TRUNCATED, RIDGE_CONTRAST, RIDGE_SHAPE, SEGMENT_WIDTH_SPAN)
from ..stripe_v1.stripe_fast import StripeRows, select_run_features, supports
from .feature_candidates import _compact_run_vectors
from .stream_ridge import RidgePacket


def select_ridge_candidates(packet, gray, config, selection_v4):
    """Return ``(StripeRows, primary[height])`` for the fixed RPG1 contract.

    ``packet`` must already have passed strict SLC2 admission and same-capture
    identity checks. Mismatched policies/configurations are explicit errors;
    this entry never silently substitutes a Raw or legacy selection policy.
    """
    if not isinstance(packet, RidgePacket):
        raise ValueError('RPG1 selection requires an admitted SLC2 RidgePacket')
    if (not isinstance(gray, np.ndarray) or gray.dtype != np.uint8
            or gray.shape != (packet.configuration.height, packet.configuration.width)):
        raise ValueError('SLC2/raw uint8 geometry mismatch')
    if not isinstance(selection_v4, SelectionConfigV4):
        raise ValueError('RPG1 selection requires SelectionConfigV4')
    if selection_v4.min_contrast_dn != 20 or selection_v4.min_edge_drop_dn != 8:
        raise ValueError('RPG1 requires contrast >=20 DN and end drops >=8 DN')
    if (not supports(config, selection_v4.legacy)
            or not packet.configuration.matches(replace(config, saturation_level=255))):
        raise ValueError('RPG1 configuration is outside the admitted hardware/fast V2 contract')
    # V4 reasons are V2 + integer ridge, not the diagnostic V3 comparison.
    base_policy = replace(selection_v4.legacy, ridge_asym_max_dn=None,
                          max_segment_width_span_px=None)
    vectors, classified, primary = _compact_run_vectors(packet, gray, config)
    base = select_run_features(gray, config, base_policy, *vectors, classified_flags=classified)
    reasons = np.asarray(base.reasons, dtype=object).copy()
    retained = np.flatnonzero(base.retained)
    x0, y0, x1, y1 = config.roi
    local_flags = packet.ridge_flags
    pl_rows = retained[primary[retained]]
    flags = local_flags[pl_rows-y0]
    # Write lower-priority failures first so a missing window remains first.
    reasons[pl_rows[(flags & 4) == 0]] = RIDGE_SHAPE
    reasons[pl_rows[(flags & 2) == 0]] = RIDGE_CONTRAST
    reasons[pl_rows[(flags & 1) == 0]] = BACKGROUND_WINDOW_TRUNCATED
    for y in retained[~primary[retained]]:
        values = ridge_integer_values(gray[y], int(base.start[y]), int(base.end[y]), x0, x1)
        reasons[y] = values.reason

    limit = selection_v4.max_segment_width_span_px
    if limit is not None and len(retained):
        # Rebuild the V2 segments before ridge rejection. The supported row-gap
        # is 1, so removing whole short V2 segments cannot merge survivors.
        policy = selection_v4.legacy.selection
        if policy.association_enabled:
            breaks = ((np.diff(retained) > policy.association_max_row_gap)
                | (np.abs(np.diff(base.center_q12[retained])) > (policy.association_max_distance_px << 12)))
            segment = np.concatenate(([0], np.cumsum(breaks))).astype(np.intp)
        else:
            segment = np.arange(len(retained), dtype=np.intp)
        passing = reasons[retained] == None  # noqa: E711 - object-array comparison
        kept_segment = segment[passing]
        if len(kept_segment):
            widths = base.end[retained[passing]]-base.start[retained[passing]]+1
            low = np.full(segment[-1]+1, np.iinfo(np.int64).max, dtype=np.int64)
            high = np.zeros(segment[-1]+1, dtype=np.int64)
            np.minimum.at(low, kept_segment, widths)
            np.maximum.at(high, kept_segment, widths)
            wide = high-low > limit
            reasons[retained[passing][wide[kept_segment]]] = SEGMENT_WIDTH_SPAN
    accepted = base.retained & np.asarray([reason is None for reason in reasons])
    return StripeRows(base.center_q12, base.start, base.end, accepted,
                      base.retained, tuple(reasons.tolist())), primary


__all__ = ['select_ridge_candidates']
