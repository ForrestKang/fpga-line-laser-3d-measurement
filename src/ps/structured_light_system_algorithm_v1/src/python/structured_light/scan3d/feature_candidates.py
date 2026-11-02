"""Exact SLF1 candidate reconstruction with row-local raw fallback.

Selection still runs the retained v2/v3 policies, including association,
ridge flanks, segment gates, and unsupported-policy scalar fallback.
"""
from dataclasses import replace
import numpy as np

from ..stripe_v1.candidates import extract_candidates
from ..stripe_v1.types import Candidate, CandidateFrame, CandidateRow
from ..stripe_v1.selection_v3 import select_candidates_v3
from ..stripe_v1.stripe_fast import supports, select_run_features, extract_fixed_run_features


def candidates_from_packet(packet, gray, config):
    """Return exact candidates and the rows whose extraction came from PL.

    The caller must parse/admit the wire payload and same-frame identity first.
    PL validity is not a selection decision: rejected single components also
    reconstruct their full candidate statistics and rejection reasons.
    """
    height, width = gray.shape
    if (height, width) != (packet.configuration.height, packet.configuration.width):
        raise ValueError('SLF1/raw geometry mismatch')
    if not packet.configuration.matches(config):
        return extract_candidates(gray, config), np.zeros(height, bool)
    x0, y0, x1, y1 = config.roi or (0, 0, width, height)
    rows, primary = [], np.zeros(height, bool)
    for y, (row, support) in enumerate(zip(packet.rows, packet.support)):
        if not y0 <= y < y1:
            rows.append(CandidateRow(y, (), False, True))
            continue
        # BAD/OVERRUN transport faults cannot establish exact raw candidates.
        if row.candidate_count > 1 or row.flags & 0x30:
            local_config = replace(config, roi=(x0, 0, x1, 1))
            source = extract_candidates(gray[y:y + 1], local_config).rows[0]
            rows.append(CandidateRow(y, tuple(replace(c, row=y) for c in source.candidates), source.overflow))
            continue
        primary[y] = True
        if row.candidate_count == 0:
            rows.append(CandidateRow(y, (), False))
            continue
        start, end, saturated = support
        # Invalid legacy PL rows publish zero XC; sums still define their exact
        # candidate. Valid rows already carry the Q12 integer division result.
        center = row.xc_fixed if row.valid else (row.s1 << 12) // row.s0
        reasons = []
        if row.width < config.min_width or row.width > config.max_width:
            reasons.append('candidate_width')
        if row.s0 < config.min_sum or row.peak < config.min_peak:
            reasons.append('insufficient_contrast')
        if saturated:
            reasons.append('saturated_support')
        if start == x0 or end == x1 - 1:
            reasons.append('roi_truncated')
        candidate = Candidate(y, 0, start, end, row.s0, row.s1, center, row.peak,
                              row.width, saturated, None, None, row.peak, tuple(reasons))
        rows.append(CandidateRow(y, (candidate,), False))
    return CandidateFrame(width, height, config.threshold_mode, config.fingerprint, tuple(rows)), primary


def _packet_run_vectors(packet, gray, config):
    """Use admitted SLF1 statistics directly; batch only raw fallback rows."""
    height, width = gray.shape
    if (height, width) != (packet.configuration.height, packet.configuration.width):
        raise ValueError('SLF1/raw geometry mismatch')
    x0, y0, x1, y1 = config.roi
    # The caller has strictly decoded/admitted the immutable payload. Read
    # these wire fields rather than rebuilding or trusting Candidate objects.
    values = np.frombuffer(packet.payload, dtype='<u4', offset=80).reshape(height, 11)[y0:y1].astype(np.int64)
    packed, components = values[:, 7], values[:, 6]
    fallback = (components > 1) | (((packed >> 8) & 0x30) != 0)
    primary = np.zeros(height, bool)
    primary[y0:y1] = ~fallback
    single = np.flatnonzero((components == 1) & ~fallback)
    records = values[single]
    meta = records[:, 10]
    center = records[:, 1].copy()
    inactive = (records[:, 7] & (1 << 23)) == 0
    center[inactive] = (records[inactive, 3] << 12) // records[inactive, 2]
    vectors = [single, meta & 1023, (meta >> 10) & 1023,
               records[:, 2], records[:, 3], records[:, 4], meta >> 20, center]
    fallback_rows = np.flatnonzero(fallback)
    if len(fallback_rows):
        raw = list(extract_fixed_run_features(gray[y0:y1, x0:x1][fallback], config, x0=x0))
        raw[0] = fallback_rows[raw[0]]
        vectors = [np.concatenate((pl, reference)) for pl, reference in zip(vectors, raw)]
        order = np.argsort(vectors[0], kind='stable')
        vectors = [value[order] for value in vectors]
    # np.bincount accepts native index integers. Zynq ARMv7 has 32-bit
    # intp; keep sums/Q12 in int64 and narrow only bounded row indices.
    vectors[0] = vectors[0].astype(np.intp, copy=False)
    return vectors, primary


def select_feature_candidates(packet, gray, config, selection):
    if supports(config, selection) and packet.configuration.matches(config):
        vectors, primary = _packet_run_vectors(packet, gray, config)
        selected = select_run_features(gray, config, selection, *vectors)
        result = (selected.center_q12, selected.accepted, selected.retained & ~selected.accepted, selected.reasons)
    else:
        frame, primary = candidates_from_packet(packet, gray, config)
        selected = select_candidates_v3(gray, frame, config, selection)
        centers = np.array([r.candidate.center_q12 if r.candidate else 0 for r in selected.rows], np.int64)
        accepted = np.array([r.candidate is not None and r.reason is None for r in selected.rows])
        rejected = np.array([r.candidate is not None and r.reason is not None for r in selected.rows])
        result = centers, accepted, rejected, tuple(r.reason for r in selected.rows)
    return result, primary


def select_compact_candidates(packet, gray, config, selection):
    """PSF1 flags replace only classification; all v2/v3 policies stay shared."""
    if gray.shape != (packet.configuration.height,packet.configuration.width):
        raise ValueError('SLC1/raw geometry mismatch')
    # Hardware PSF1 records DN255 support. For a unique component its maximum
    # Raw DN is the ROI maximum, so a caller's support-saturation policy can be
    # adapted exactly without rescanning pixels or changing the wire contract.
    if not supports(config, selection) or not packet.configuration.matches(replace(config,saturation_level=255)):
        from .pipeline import select_rows
        return select_rows(gray,config,selection,reference=False), np.zeros(gray.shape[0],bool)
    vectors, classified, primary = _compact_run_vectors(packet, gray, config)
    selected = select_run_features(gray,config,selection,*vectors,classified_flags=classified)
    return (selected.center_q12,selected.accepted,selected.retained & ~selected.accepted,selected.reasons),primary


def _compact_run_vectors(packet, gray, config):
    """Shared admitted compact vectors; Raw extraction is fallback-row local."""
    x0,y0,x1,y1 = config.roi
    center,flags,_,fallback,_,first,last,count,_,rawpeak,peak,sat = packet.columns
    single = np.flatnonzero((count == 1) & ~fallback)
    zeros = np.zeros(len(single),np.int64)
    policy_sat = rawpeak[single] >= config.saturation_level
    vectors = [single.astype(np.intp),first[single],last[single],zeros,zeros,
               peak[single],policy_sat,center[single]]
    classified = (flags[single].astype(np.int64) & ~4) | (policy_sat.astype(np.int64) << 2)
    fallback_rows = np.flatnonzero(fallback)
    if len(fallback_rows):
        from ..stripe_v1.stripe_fast import WIDTH,CONTRAST,SATURATED,TRUNCATED,OVERFLOW,S0_MAX,S1_MAX,XC_MAX
        raw = list(extract_fixed_run_features(gray[y0:y1,x0:x1][fallback],config,x0=x0))
        raw[0] = fallback_rows[raw[0]]
        rw = raw[2]-raw[1]+1
        rf = ((rw < config.min_width)|(rw > config.max_width)).astype(np.int64)*WIDTH
        rf |= ((raw[3] < max(1,config.min_sum))|(raw[5] < max(1,config.min_peak))).astype(np.int64)*CONTRAST
        rf |= (raw[6]>0).astype(np.int64)*SATURATED
        rf |= ((raw[1]==x0)|(raw[2]==x1-1)).astype(np.int64)*TRUNCATED
        rf |= ((raw[3]>S0_MAX)|(raw[4]>S1_MAX)|(raw[7]>XC_MAX)).astype(np.int64)*OVERFLOW
        vectors = [np.concatenate((pl,r)) for pl,r in zip(vectors,raw)]
        classified = np.concatenate((classified,rf))
        order = np.argsort(vectors[0],kind='stable')
        vectors = [v[order] for v in vectors]
        classified = classified[order]
    vectors = [np.asarray(v,dtype=np.int64) for v in vectors]
    vectors[0] = vectors[0].astype(np.intp,copy=False)
    primary = np.zeros(gray.shape[0],bool);primary[y0:y1] = ~fallback
    return vectors, classified, primary
