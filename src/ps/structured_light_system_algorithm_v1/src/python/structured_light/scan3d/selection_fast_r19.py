"""R19 exact fallback batching; PL facts and original Q12 remain authoritative.

Derived from unchanged local_segments_r16. Only unneeded per-row corrected-center
computation is removed from runtime: this consumer uses the integer ridge reason,
not its diagnostic corrected centroid. Unsupported policies use legacy semantics.
Whole-frame segment fallback still reuses valid PL RPG row facts; it does NOT
mean every Raw pixel is processed on PS.
"""
import numpy as np
from ..stripe_v1.stripe_fast import (StripeRows,extract_fixed_run_features,WIDTH,CONTRAST,SATURATED,TRUNCATED,OVERFLOW,S0_MAX,S1_MAX,XC_MAX)
from .local_segments_r16 import _supported,_PRIORITY
from .ridge_candidates import select_ridge_candidates
from .segment_candidates_r15 import select_segment_candidates
from .stream_segments_r15 import SegmentPacket
from .stream_selected_runs_r17 import SelectedRunsPacket,select_selected_run_candidates


def batch_ridge_reasons(gray,rows,starts,ends,x0,x1):
    """Exact V4 reason on retained <=32-pixel runs, without diagnostic centroid."""
    rows=np.asarray(rows,dtype=np.intp);starts=np.asarray(starts,dtype=np.intp);ends=np.asarray(ends,dtype=np.intp)
    if gray.dtype!=np.uint8 or gray.ndim!=2 or rows.ndim!=1 or starts.shape!=rows.shape or ends.shape!=rows.shape:
        raise ValueError('gray8 and equal row/support vectors required')
    n=len(rows)
    if not n:return np.empty(0,dtype=object)
    if (not 0<=x0<x1<=gray.shape[1] or np.any(rows<0) or np.any(rows>=gray.shape[0]) or np.any(starts<x0) or np.any(ends>=x1) or np.any(ends<starts) or np.any(ends-starts>=32)):
        raise ValueError('retained support must be within ROI and at most32 pixels')
    reasons=np.full(n,None,dtype=object)
    complete=(starts-11>=x0)&(ends+11<x1)
    ix=np.flatnonzero(complete)
    reasons[~complete]='background_window_truncated'
    if not len(ix):return reasons
    rr,ss,ee=rows[ix],starts[ix],ends[ix]
    offsets=np.arange(32,dtype=np.intp);cols=np.minimum(ss[:,None]+offsets,x1-1)
    pixels=gray[rr[:,None],cols].astype(np.int16)
    inside=cols<=ee[:,None];peak=np.where(inside,pixels,0).max(axis=1)
    interior=inside&(cols>ss[:,None])&(cols<ee[:,None])
    inner_peak=np.where(interior,pixels,0).max(axis=1)
    left=gray[rr[:,None],ss[:,None]+np.arange(-11,-3)].sum(axis=1,dtype=np.int64)
    right=gray[rr[:,None],ee[:,None]+np.arange(4,12)].sum(axis=1,dtype=np.int64)
    contrast=8*peak.astype(np.int64)-np.maximum(left,right)
    shape=(ee-ss+1<3)|(peak-gray[rr,ss].astype(np.int16)<8)|(peak-gray[rr,ee].astype(np.int16)<8)|(inner_peak!=peak)
    reasons[ix[shape]]='ridge_shape'
    reasons[ix[contrast<160]]='ridge_contrast'
    return reasons


def _select_local_fast(packet, gray, config, selection, *, stats=None):
    """Same two-result interface as R15, with optional caller-owned evidence.

    Packet identity/wire admission belongs to the existing decoder/capture.
    Unsupported policies retain the original consumer including its errors.
    """
    if not isinstance(packet, SegmentPacket):
        raise ValueError('local R16 selection requires an admitted SLC3 SegmentPacket')
    if not packet.whole_frame_fallback:
        result = select_segment_candidates(packet, gray, config, selection)
        if stats is not None:
            stats.update(path='received_PL_final_decisions',
                pl_final_decision_rows=packet.configuration.roi[3]-packet.configuration.roi[1],
                raw_extracted_rows=0, cpu_segment_rows=0)
        return result
    if not _supported(packet, config, selection):
        result = select_ridge_candidates(packet.ridge_packet, gray, config, selection)
        if stats is not None:
            stats.update(path='unchanged_generic_policy_fallback', pl_final_decision_rows=0)
        return result
    if (not isinstance(gray, np.ndarray) or gray.dtype != np.uint8
            or gray.shape != (packet.configuration.height, packet.configuration.width)):
        raise ValueError('SLC3/raw uint8 geometry mismatch')
    x0, y0, x1, y1 = config.roi
    height = gray.shape[0]
    n = y1-y0
    center, flags, _, fallback, _, first, last, count, *_ = packet.columns
    primary = ~fallback
    centers = center.astype(np.int64).copy()
    starts = first.astype(np.int64).copy()
    ends = last.astype(np.int64).copy()
    chosen = primary & (count == 1) & (flags == 0)
    reasons = np.full(n, None, dtype=object)
    reasons[primary & (count == 0)] = 'no_support'
    rejected = primary & (count == 1) & (flags != 0)
    for bit, reason in reversed(_PRIORITY):
        reasons[rejected & ((flags & bit) != 0)] = reason

    raw_rows = np.flatnonzero(fallback)
    if len(raw_rows):
        # The only Raw extraction is these rows, in original ROI coordinates.
        rr, rs, re, s0, s1, peak, saturated, rc = extract_fixed_run_features(
            gray[y0+raw_rows, x0:x1], config, x0=x0)
        width = re-rs+1
        rf = ((width < config.min_width) | (width > config.max_width)).astype(np.int64)*WIDTH
        rf |= ((s0 < max(1, config.min_sum)) | (peak < max(1, config.min_peak))).astype(np.int64)*CONTRAST
        rf |= (saturated > 0).astype(np.int64)*SATURATED
        rf |= ((rs == x0) | (re == x1-1)).astype(np.int64)*TRUNCATED
        rf |= ((s0 > S0_MAX) | (s1 > S1_MAX) | (rc > XC_MAX)).astype(np.int64)*OVERFLOW
        raw_count = np.bincount(rr, minlength=len(raw_rows))
        eligible = rf == 0
        eligible_count = np.bincount(rr, weights=eligible, minlength=len(raw_rows)).astype(np.int64)
        sat_comp = np.bincount(rr, weights=rf == SATURATED, minlength=len(raw_rows)).astype(np.int64)
        row_flags = np.zeros(len(raw_rows), np.int64)
        np.bitwise_or.at(row_flags, rr, rf)
        raw_reasons = np.full(len(raw_rows), None, dtype=object)
        raw_reasons[raw_count == 0] = 'no_support'
        overflow = raw_count > config.max_candidates_per_row
        raw_reasons[overflow] = 'candidate_overflow'
        open_rows = (raw_count > 0) & ~overflow
        none = open_rows & (eligible_count == 0)
        for bit, reason in reversed(_PRIORITY):
            raw_reasons[none & ((row_flags & bit) != 0)] = reason
        raw_reasons[none & (row_flags == 0)] = 'insufficient_contrast'
        has = open_rows & (eligible_count > 0)
        raw_reasons[has & (sat_comp > 0)] = 'saturated_competitor'
        ambiguous_raw = has & (sat_comp == 0) & (eligible_count > 1)
        raw_reasons[ambiguous_raw] = 'ambiguous_candidates'
        unique = has & (sat_comp == 0) & (eligible_count == 1)
        eligible_runs = np.flatnonzero(eligible)
        unique_runs = eligible_runs[unique[rr[eligible_runs]]]
        local = raw_rows[rr[unique_runs]]
        chosen[local] = True
        centers[local], starts[local], ends[local] = rc[unique_runs], rs[unique_runs], re[unique_runs]
        reasons[raw_rows] = raw_reasons
        # Both anchors must be ORIGINAL unique rows, not rows just resolved by
        # association. Neighboring ambiguities cannot bootstrap one another.
        anchors = chosen.copy()
        left, right = np.zeros(n, bool), np.zeros(n, bool)
        left[1:], right[:-1] = anchors[:-1], anchors[1:]
        ambiguous = np.zeros(n, bool)
        ambiguous[raw_rows] = ambiguous_raw
        possible = ambiguous & left & right
        eligible_local = raw_rows[rr[eligible_runs]]
        candidates = eligible_runs[possible[eligible_local]]
        local = raw_rows[rr[candidates]]
        matches = ((np.abs(rc[candidates]-centers[local-1]) <= 12288)
            & (np.abs(rc[candidates]-centers[local+1]) <= 12288))
        matched = candidates[matches]
        local = raw_rows[rr[matched]]
        match_count = np.bincount(local, minlength=n)
        singles = matched[match_count[local] == 1]
        local = raw_rows[rr[singles]]
        chosen[local] = True
        centers[local], starts[local], ends[local] = rc[singles], rs[singles], re[singles]
        reasons[local] = None

    #0x80 supplied no final PL masks, so every remaining segment decision is
    #explicitly PS work. Association can influence an entire connected run.
    selected = np.flatnonzero(chosen)
    segment = np.full(n, -1, np.intp)
    if len(selected):
        breaks = (np.diff(selected) > 1) | (np.abs(np.diff(centers[selected])) > 12288)
        ids = np.concatenate(([0], np.cumsum(breaks))).astype(np.intp)
        segment[selected] = ids
        sizes = np.bincount(ids)
        short = selected[sizes[ids] < 3]
        chosen[short] = False
        reasons[short] = 'insufficient_segment_support'
    retained = np.flatnonzero(chosen)
    pl = retained[primary[retained]]
    ridge = packet.ridge_flags[pl]
    reasons[pl[(ridge & 4) == 0]] = 'ridge_shape'
    reasons[pl[(ridge & 2) == 0]] = 'ridge_contrast'
    reasons[pl[(ridge & 1) == 0]] = 'background_window_truncated'
    raw_ridge = retained[~primary[retained]]
    reasons[raw_ridge] = batch_ridge_reasons(gray, y0+raw_ridge, starts[raw_ridge], ends[raw_ridge], x0, x1)
    passing = retained[reasons[retained] == None]  # noqa: E711
    if len(passing):
        ids = segment[passing]
        low = np.full(int(ids.max())+1, np.iinfo(np.int64).max, np.int64)
        high = np.zeros(len(low), np.int64)
        widths = ends[passing]-starts[passing]+1
        np.minimum.at(low, ids, widths)
        np.maximum.at(high, ids, widths)
        reasons[passing[(high-low > 12)[ids]]] = 'segment_width_span'
    full_center = np.zeros(height, np.int64)
    full_start, full_end = np.full(height, -1, np.int64), np.full(height, -1, np.int64)
    full_retained, full_primary = np.zeros(height, bool), np.zeros(height, bool)
    full_reason = np.full(height, 'roi_excluded', dtype=object)
    full_center[y0+retained], full_start[y0+retained], full_end[y0+retained] = centers[retained], starts[retained], ends[retained]
    full_retained[y0+retained] = True
    full_primary[y0:y1], full_reason[y0:y1] = primary, reasons
    accepted = full_retained & (full_reason == None)  # noqa: E711
    if stats is not None:
        stats.update(path='PS_local_Raw_and_exact_global_segments', pl_final_decision_rows=0,
            pl_fact_rows=int(primary.sum()), raw_extracted_rows=len(raw_rows),
            raw_source_rows=(raw_rows+y0).tolist(), cpu_segment_rows=len(selected),
            ps_segment_count=int(segment.max())+1)
    return StripeRows(full_center, full_start, full_end, accepted, full_retained,
        tuple(full_reason.tolist())), full_primary


def select_selected_run_candidates_fast(packet,gray,config,selection,*,stats=None):
    """Return exact legacy StripeRows and per-row PL fact provenance."""
    if not isinstance(packet,SelectedRunsPacket):
        return select_selected_run_candidates(packet,gray,config,selection)
    if not packet.whole_frame_fallback:
        result=select_selected_run_candidates(packet,gray,config,selection)
        if stats is not None:stats.update(path='received_PL_final_decisions',raw_extracted_rows=0,pl_final_decision_rows=packet.configuration.roi[3]-packet.configuration.roi[1])
        return result
    return _select_local_fast(packet.fallback_segment_packet,gray,config,selection,stats=stats)
