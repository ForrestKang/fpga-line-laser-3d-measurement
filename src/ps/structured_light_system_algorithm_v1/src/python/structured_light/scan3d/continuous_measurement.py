"""Offline millimetre measurements with explicit validity and as-built truth.

The 0.5 mm value is a target. Synthetic fit accuracy is not a physical accuracy
claim. Reference surfaces/sections must already be calibrated and selected by
the caller; this module never substitutes CAD dimensions for missing evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from .continuous_timing import _finite

TARGET_ABSOLUTE_ERROR_MM = 0.5


@dataclass(frozen=True)
class MeasurementValidity:
    axis_valid: bool
    timing_valid: bool
    volume_valid: bool
    reference_basis: str
    units: str = "mm"

    def __post_init__(self) -> None:
        if any(type(x) is not bool for x in (self.axis_valid, self.timing_valid, self.volume_valid)):
            raise ValueError("measurement validity flags must be bool")
        if self.units != "mm":
            raise ValueError("calibrated millimetre input is required")
        if not isinstance(self.reference_basis, str) or not self.reference_basis.strip():
            raise ValueError("explicit calibrated reference basis is required")

    @property
    def valid(self) -> bool:
        return self.axis_valid and self.timing_valid and self.volume_valid


@dataclass(frozen=True)
class MeasurementResult:
    value_mm: float | None
    status: str
    reason: str | None
    point_count: int
    reference_basis: str
    units: str = "mm"
    residual_mm: float | None = None


@dataclass(frozen=True)
class TruthComparison:
    status: str
    error_mm: float | None
    tolerance_mm: float | None
    truth_mm: float | None
    truth_reference: str | None
    reason: str | None
    target_mm: float = TARGET_ABSOLUTE_ERROR_MM


def _unavailable(reason: str, validity: MeasurementValidity, count: int = 0) -> MeasurementResult:
    return MeasurementResult(None, "unavailable", reason, count, validity.reference_basis)


def _points(values: object, columns: int) -> np.ndarray | None:
    try:
        points = np.asarray(values, dtype=float)
    except (ValueError, TypeError):
        return None
    if points.ndim != 2 or points.shape[1] != columns or not np.isfinite(points).all():
        return None
    return points


def _circle(points: np.ndarray) -> tuple[np.ndarray, float] | None:
    origin = points.mean(axis=0)
    shifted = points - origin
    matrix = np.column_stack((2 * shifted, np.ones(len(points))))
    if np.linalg.matrix_rank(matrix) < 3:
        return None
    fit, *_ = np.linalg.lstsq(matrix, np.sum(shifted ** 2, axis=1), rcond=None)
    radius2 = fit[2] + np.sum(fit[:2] ** 2)
    if not np.isfinite(radius2) or radius2 <= 0:
        return None
    return origin + fit[:2], float(np.sqrt(radius2))


def _circle_hypotheses(points: np.ndarray, triples: np.ndarray) -> list:
    """Batched triplet circles with an exact per-triplet fallback.

    Only the arithmetic is batched; the accept/reject rule is _circle()'s.
    Triplets are recomputed by _circle() whenever the batched determinant is
    within the ambiguity band, so borderline cases cannot change behaviour.
    """
    subset = points[triples]
    origin = subset.mean(axis=1)
    shifted = subset - origin[:, None, :]
    matrix = np.concatenate((2 * shifted, np.ones((len(subset), 3, 1))), axis=2)
    rhs = np.sum(shifted ** 2, axis=2)
    determinant = np.linalg.det(matrix)
    band = 1e-9 * np.maximum(np.abs(matrix).max(axis=(1, 2)), 1.0) ** 3
    unambiguous = np.abs(determinant) > band
    out: list = [None] * len(subset)
    if unambiguous.any():
        solution = np.linalg.solve(matrix[unambiguous], rhs[unambiguous][..., None])[..., 0]
        radius2 = solution[:, 2] + np.sum(solution[:, :2] ** 2, axis=1)
        ok = np.isfinite(radius2) & (radius2 > 0)
        centers = origin[unambiguous] + solution[:, :2]
        radii = np.sqrt(np.where(ok, radius2, 1.0))
        for spot, center, radius, good in zip(np.flatnonzero(unambiguous), centers, radii, ok):
            out[int(spot)] = (center, float(radius)) if good else None
    for spot in np.flatnonzero(~unambiguous):
        out[int(spot)] = _circle(points[triples[spot]])
    return out


def fit_circle_diameter(points_xy_mm: object, *, validity: MeasurementValidity,
                        min_points: int = 12, max_residual_mm: float = 0.25,
                        min_angular_coverage_rad: float = 1.5 * math.pi) -> MeasurementResult:
    """Deterministic bounded consensus fit; occluded arcs remain unavailable.

    Uses up to 128 triplet hypotheses, then least squares on the consensus.
    The residual gate and required angular support are explicit policy values.
    No points are filled, extrapolated into occlusions, or silently de-NaNed.
    """
    validity.__post_init__()
    _finite("max_residual_mm", max_residual_mm)
    _finite("min_angular_coverage_rad", min_angular_coverage_rad)
    if (type(min_points) is not int or min_points < 3 or max_residual_mm <= 0
            or not 0 < min_angular_coverage_rad <= math.tau):
        raise ValueError("invalid circle fit limits")
    if not validity.valid:
        return _unavailable("axis_timing_or_volume_unvalidated", validity)
    points = _points(points_xy_mm, 2)
    if points is None:
        return _unavailable("invalid_or_nonfinite_points", validity)
    if len(points) < min_points:
        return _unavailable("insufficient_points", validity, len(points))
    rng = np.random.default_rng(0)
    # Same draw sequence as the former per-iteration choice(), but the 128
    # triplet circles are solved as one batched system. Triplets whose 3x3
    # system is near-singular (the case where matrix_rank/lstsq and a direct
    # solve can disagree) fall back to the original _circle(), so the accepted
    # hypotheses, inlier sets and results are unchanged.
    triples = np.empty((128, 3), np.int64)
    for index in range(128):
        triples[index] = rng.choice(len(points), 3, replace=False)
    hypotheses = _circle_hypotheses(points, triples)
    best = np.zeros(len(points), bool)
    best_error = math.inf
    for hypothesis in hypotheses:
        if hypothesis is None:
            continue
        center, radius = hypothesis
        residual = np.abs(np.linalg.norm(points - center, axis=1) - radius)
        inliers = residual <= max_residual_mm
        n = int(inliers.sum())
        error = float(np.median(residual[inliers])) if n else math.inf
        if n > best.sum() or (n == best.sum() and error < best_error):
            best, best_error = inliers, error
    if best.sum() < min_points:
        return _unavailable("insufficient_circle_consensus", validity, int(best.sum()))
    fit = _circle(points[best])
    if fit is None:
        return _unavailable("degenerate_circle", validity, int(best.sum()))
    center, radius = fit
    residual = np.abs(np.linalg.norm(points - center, axis=1) - radius)
    best &= residual <= max_residual_mm
    if best.sum() < min_points:
        return _unavailable("insufficient_circle_consensus", validity, int(best.sum()))
    angles = np.sort(np.arctan2(points[best, 1] - center[1], points[best, 0] - center[0]))
    largest_gap = np.max(np.diff(np.r_[angles, angles[0] + math.tau]))
    if math.tau - largest_gap < min_angular_coverage_rad:
        return _unavailable("occluded_angular_support", validity, int(best.sum()))
    rms = float(np.sqrt(np.mean(residual[best] ** 2)))
    return MeasurementResult(2 * radius, "available", None, int(best.sum()), validity.reference_basis,
                             residual_mm=rms)


def diameters_by_height(points_xyz_mm: object, *, height_bins_mm: tuple[tuple[float, float], ...],
                        validity: MeasurementValidity, **fit_options: object) -> tuple[MeasurementResult, ...]:
    """Half-open calibrated object-Z bins; final upper edge is included."""
    if not height_bins_mm:
        raise ValueError("height bins required")
    previous = -math.inf
    for lo, hi in height_bins_mm:
        _finite("height lower", lo)
        _finite("height upper", hi)
        if lo >= hi or lo < previous:
            raise ValueError("height bins must be ordered and nonoverlapping")
        previous = hi
    points = _points(points_xyz_mm, 3)
    if points is None:
        return tuple(_unavailable("invalid_or_nonfinite_points", validity) for _ in height_bins_mm)
    results = []
    for index, (lo, hi) in enumerate(height_bins_mm):
        mask = (points[:, 2] >= lo) & ((points[:, 2] <= hi) if index == len(height_bins_mm) - 1
                                      else (points[:, 2] < hi))
        results.append(fit_circle_diameter(points[mask, :2], validity=validity, **fit_options))
    return tuple(results)


def _surface_difference(lower: object, upper: object, *, columns: int, axis: int,
                        validity: MeasurementValidity, signed: bool) -> MeasurementResult:
    validity.__post_init__()
    if type(axis) is not int or not 0 <= axis < columns:
        raise ValueError("invalid calibrated measurement axis")
    if not validity.valid:
        return _unavailable("axis_timing_or_volume_unvalidated", validity)
    a, b = _points(lower, columns), _points(upper, columns)
    if a is None or b is None:
        return _unavailable("invalid_or_nonfinite_points", validity)
    if min(len(a), len(b)) < 3:
        return _unavailable("insufficient_surface_points", validity, len(a) + len(b))
    delta = float(np.median(b[:, axis]) - np.median(a[:, axis]))
    if not signed and delta <= 0:
        return _unavailable("surface_order_or_occlusion", validity, len(a) + len(b))
    return MeasurementResult(delta, "available", None, len(a) + len(b), validity.reference_basis)


def segment_height(bottom_points_xyz_mm: object, top_points_xyz_mm: object, *,
                   validity: MeasurementValidity, axis: int = 2) -> MeasurementResult:
    """Distance between explicitly observed bottom/top surfaces along object Z."""
    return _surface_difference(bottom_points_xyz_mm, top_points_xyz_mm, columns=3, axis=axis,
                               validity=validity, signed=False)


def section_step(section_xy_mm: object, *, left_range_mm: tuple[float, float],
                 right_range_mm: tuple[float, float], validity: MeasurementValidity) -> MeasurementResult:
    """Signed section-Y step between two explicit section-X support ranges."""
    for lo, hi in (left_range_mm, right_range_mm):
        _finite("range lower", lo)
        _finite("range upper", hi)
        if lo >= hi:
            raise ValueError("section support range must increase")
    points = _points(section_xy_mm, 2)
    if points is None:
        return _unavailable("invalid_or_nonfinite_points", validity)
    left = points[(points[:, 0] >= left_range_mm[0]) & (points[:, 0] <= left_range_mm[1])]
    right = points[(points[:, 0] >= right_range_mm[0]) & (points[:, 0] <= right_range_mm[1])]
    return _surface_difference(left, right, columns=2, axis=1, validity=validity, signed=True)


def section_gap(left_edge_xy_mm: object, right_edge_xy_mm: object, *,
                validity: MeasurementValidity, axis: int = 0) -> MeasurementResult:
    """Positive gap between observed edge samples in a calibrated section basis."""
    if type(axis) is not int or axis not in (0, 1):
        raise ValueError("invalid calibrated measurement axis")
    left, right = _points(left_edge_xy_mm, 2), _points(right_edge_xy_mm, 2)
    if left is not None and right is not None and len(left) and len(right):
        transverse = 1 - axis
        lo = max(left[:, transverse].min(), right[:, transverse].min())
        hi = min(left[:, transverse].max(), right[:, transverse].max())
        left = left[(left[:, transverse] >= lo) & (left[:, transverse] <= hi)]
        right = right[(right[:, transverse] >= lo) & (right[:, transverse] <= hi)]
        if min(len(left), len(right)) < 3:
            return _unavailable("occluded_common_section_support", validity)
    return _surface_difference(left if left is not None else left_edge_xy_mm,
                               right if right is not None else right_edge_xy_mm, columns=2, axis=axis,
                               validity=validity, signed=False)


def compare_as_built(result: MeasurementResult, *, truth_mm: float | None,
                     tolerance_mm: float | None, truth_reference: str | None,
                     validity: MeasurementValidity, truth_source: str = "as_built") -> TruthComparison:
    validity.__post_init__()
    reason = None
    if not validity.valid:
        reason = "axis_timing_or_volume_unvalidated"
    elif result.status != "available" or result.value_mm is None or not math.isfinite(result.value_mm):
        reason = "measurement_unavailable"
    elif result.reference_basis != validity.reference_basis or result.units != "mm":
        reason = "reference_basis_mismatch"
    elif truth_source != "as_built":
        reason = "as_built_truth_required"
    elif truth_mm is None or not isinstance(truth_reference, str) or not truth_reference.strip():
        reason = "as_built_truth_missing"
    elif tolerance_mm is None:
        reason = "tolerance_missing"
    if reason is not None:
        return TruthComparison("unavailable", None, tolerance_mm, truth_mm, truth_reference, reason)
    truth = _finite("truth_mm", truth_mm)
    tolerance = _finite("tolerance_mm", tolerance_mm)
    if tolerance < 0:
        raise ValueError("tolerance must be nonnegative")
    error = float(result.value_mm - truth)
    numerical_epsilon = 8 * np.spacing(max(abs(truth), abs(result.value_mm), 1))
    status = "PASS" if abs(error) <= tolerance + numerical_epsilon else "FAIL"
    return TruthComparison(status, error, tolerance, truth, truth_reference, None)
