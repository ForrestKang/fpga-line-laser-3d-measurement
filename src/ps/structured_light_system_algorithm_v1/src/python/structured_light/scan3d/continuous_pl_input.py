"""Same-frame PL Q12 validation and conservative v3 row selection.

This first integration deliberately requires raw-v3 parity per frame. It is a
feature validation path, not a proven raw-free PL replacement or ARM speedup.
Normal matching one-component rows use PL Q12; all other rows retain raw-v3
decisions, including association, background and segment gates.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..stripe_v1.selection_v3 import SelectionConfigV3
from ..stripe_v1.types import StripeConfig
from .continuous_timing import FrameMotionMetadata
from .pipeline import select_rows


@dataclass(frozen=True)
class PLFrameRows:
    metadata: FrameMotionMetadata
    center_q12: np.ndarray
    accepted: np.ndarray
    candidate_counts: np.ndarray
    config_fingerprint: str
    feature_payload: bytes = b''

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, FrameMotionMetadata):
            raise ValueError("PL features need exact CSM1 frame metadata")
        self.metadata.__post_init__()
        arrays = (np.asarray(self.center_q12), np.asarray(self.accepted), np.asarray(self.candidate_counts))
        centers, valid, counts = arrays
        if (centers.ndim != 1 or len(centers) == 0 or valid.shape != centers.shape
                or counts.shape != centers.shape or centers.dtype.kind not in "iu"
                or counts.dtype.kind not in "iu" or valid.dtype.kind != "b"
                or np.any(centers < 0) or np.any(centers > (1 << 22) - 1) or np.any(counts < 0)):
            raise ValueError("PL centers/counts must be nonnegative integer vectors and valid a bool vector")
        if not isinstance(self.config_fingerprint, str) or not self.config_fingerprint:
            raise ValueError("PL feature configuration fingerprint required")
        for name, value in zip(("center_q12", "accepted", "candidate_counts"), arrays):
            snapshot = value.copy()
            snapshot.setflags(write=False)
            object.__setattr__(self, name, snapshot)
        object.__setattr__(self, 'feature_payload', bytes(self.feature_payload))


@dataclass(frozen=True)
class PLSelection:
    center_q12: np.ndarray
    accepted: np.ndarray
    rejected: np.ndarray
    reasons: tuple[str | None, ...]
    pl_primary: np.ndarray
    raw_fallback: np.ndarray
    parity_validated: bool
    mismatch_rows: tuple[int, ...]


def select_pl_rows(pl: PLFrameRows, *, raw: np.ndarray | None,
                   raw_metadata: FrameMotionMetadata | None, stripe_config: StripeConfig,
                   selection: SelectionConfigV3) -> PLSelection:
    if pl.config_fingerprint != stripe_config.fingerprint:
        raise ValueError("PL and raw stripe configuration mismatch")
    n = len(pl.center_q12)
    if raw is None:
        return PLSelection(pl.center_q12.copy(), np.zeros(n, bool), np.zeros(n, bool),
                           ("pl_parity_unvalidated",) * n, np.zeros(n, bool), np.zeros(n, bool), False, ())
    if raw_metadata != pl.metadata:
        raise ValueError("PL features and raw frame identity/epoch/timing/exposure mismatch")
    gray = np.asarray(raw)
    if gray.ndim != 2 or gray.shape[0] != n or gray.dtype != np.uint8 or gray.size == 0:
        raise ValueError("PL/raw frame dimensions or gray8 type mismatch")
    if np.any(pl.accepted & (pl.center_q12 >= gray.shape[1] << 12)):
        raise ValueError("PL Q12 center outside frame width")
    if stripe_config.roi is not None:
        _, _, x1, y1 = stripe_config.roi
        if x1 > gray.shape[1] or y1 > gray.shape[0]:
            raise ValueError("stripe ROI outside frame")
    if pl.feature_payload:
        from .stream_features import decode_slf1
        from .feature_candidates import select_feature_candidates
        packet = decode_slf1(pl.feature_payload, frame_width=gray.shape[1], frame_height=n,
                              request_id=pl.metadata.request_id, capture_epoch=pl.metadata.capture_epoch)
        if (not np.array_equal(pl.center_q12, [r.xc_fixed for r in packet.rows])
                or not np.array_equal(pl.accepted, [r.valid and r.flags == 0 for r in packet.rows])
                or not np.array_equal(pl.candidate_counts, [r.candidate_count for r in packet.rows])):
            raise ValueError('PL attributes differ from SLF1 wire evidence')
        if packet.profile.error_flags:
            raise ValueError('SLF1 capture reports frame errors')
        (centers, accepted, rejected, reasons), primary = select_feature_candidates(packet, gray, stripe_config, selection)
        fallback = np.array([r != 'roi_excluded' for r in reasons]) & ~primary
        # This path has exact wire statistics, but did not run a raw parity QA.
        return PLSelection(centers, accepted, rejected, reasons, primary, fallback, False, ())
    # The vectorized implementation has the same row-decision contract as
    # the retained scalar reference. Keep per-frame conservative parity, but
    # avoid reintroducing the old scalar extraction cost into production.
    centers, accepted, rejected, reasons = select_rows(gray, stripe_config, selection, reference=False)
    # v3 acceptance is mandatory even for normal single-component PL rows.
    normal = pl.accepted & (pl.candidate_counts == 1)
    agree = normal & accepted & (pl.center_q12 == centers)
    mismatch = normal & (~accepted | (pl.center_q12 != centers))
    output = centers.copy()
    output[agree] = pl.center_q12[agree]
    fallback = np.array([r != "roi_excluded" for r in reasons]) & ~agree
    return PLSelection(output, accepted.copy(), rejected.copy(), reasons, agree, fallback,
                       True, tuple(np.flatnonzero(mismatch).tolist()))


def strict_geometry_mask(valid: object, outside_training: object,
                         points_camera_mm: object) -> tuple[np.ndarray, tuple[str | None, ...]]:
    """Continuous mode rejects extrapolation rather than displaying it as valid.

    The decision order is the frozen scalar contract: not-valid wins over
    outside-bounds, which wins over non-finite.  The labels are applied with
    boolean masks (rather than one Python branch per row), but the resulting
    tuple keeps the exact per-row order and objects.
    """
    v, outside = np.asarray(valid), np.asarray(outside_training)
    points = np.asarray(points_camera_mm, dtype=float)
    if (v.ndim != 1 or v.dtype.kind != "b" or outside.shape != v.shape
            or outside.dtype.kind != "b" or points.shape != (len(v), 3)):
        raise ValueError("geometry masks/XYZ shapes mismatch")
    finite = np.isfinite(points).all(axis=1)
    accepted = v & ~outside & finite
    labels = np.full(len(v), None, dtype=object)
    if len(v):
        labels[~v] = "geometry_rejected"
        labels[v & outside] = "outside_training_bounds"
        labels[v & ~outside & ~finite] = "nonfinite_geometry"
    return accepted, tuple(labels.tolist())
