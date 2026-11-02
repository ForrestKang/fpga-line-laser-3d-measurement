"""Direct PL RSG1 decisions; unchanged RPG1 consumer for whole-frame fallback."""
import numpy as np

from ..stripe_v1.selection_v2 import SelectionConfigV2
from ..stripe_v1.selection_v3 import SelectionConfigV3
from ..stripe_v1.selection_v4 import SelectionConfigV4
from ..stripe_v1.stripe_fast import StripeRows
from .ridge_candidates import select_ridge_candidates
from .stream_segments_r15 import SegmentPacket, SEGMENT_REASONS, segment_configuration_supported


def fixed_segment_policy():
    return SelectionConfigV4(legacy=SelectionConfigV3(selection=SelectionConfigV2(
        association_max_distance_px=3, association_max_row_gap=1, minimum_segment_rows=3)))


def select_segment_candidates(packet, gray, config, selection_v4):
    """Expand admitted PL masks/original Q12, without PS segmentation/span work.

    Wire and capture identity admission remain the caller's responsibility.
    Fallback preserves the unchanged consumer's explicit configuration errors.
    """
    if not isinstance(packet, SegmentPacket):
        raise ValueError('RSG1 selection requires an admitted SLC3 SegmentPacket')
    if packet.whole_frame_fallback:
        return select_ridge_candidates(packet.ridge_packet, gray, config, selection_v4)
    if (not isinstance(gray, np.ndarray) or gray.dtype != np.uint8
            or gray.shape != (packet.configuration.height, packet.configuration.width)):
        raise ValueError('SLC3/raw uint8 geometry mismatch')
    if (not segment_configuration_supported(config)
            or not packet.configuration.matches(config)
            or not isinstance(selection_v4, SelectionConfigV4)
            or selection_v4.legacy.selection != fixed_segment_policy().legacy.selection
            or (selection_v4.min_contrast_dn, selection_v4.min_edge_drop_dn,
                selection_v4.max_segment_width_span_px) != (20, 8, 12)):
        raise ValueError('RSG1 selection requires the fixed configuration and segment policy')
    height = packet.configuration.height
    _, y0, _, y1 = packet.configuration.roi
    decisions = packet.decisions
    local_retained = (decisions & 2) != 0
    center, _, _, _, _, first, last, *_ = packet.columns
    centers = np.zeros(height, np.int64)
    start = np.full(height, -1, np.int64)
    end = np.full(height, -1, np.int64)
    accepted = np.zeros(height, bool)
    retained = np.zeros(height, bool)
    primary = np.zeros(height, bool)
    reasons = np.full(height, 'roi_excluded', dtype=object)
    centers[y0:y1] = np.where(local_retained, center, 0)
    start[y0:y1] = np.where(local_retained, first.astype(np.int64), -1)
    end[y0:y1] = np.where(local_retained, last.astype(np.int64), -1)
    accepted[y0:y1] = (decisions & 1) != 0
    retained[y0:y1] = local_retained
    primary[y0:y1] = True
    reasons[y0:y1] = np.asarray(SEGMENT_REASONS, dtype=object)[(decisions >> 2) & 15]
    return StripeRows(centers, start, end, accepted, retained, tuple(reasons.tolist())), primary


__all__ = ['select_segment_candidates', 'fixed_segment_policy']

