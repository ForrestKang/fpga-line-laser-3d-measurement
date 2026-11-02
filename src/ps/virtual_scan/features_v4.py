"""V4 observed geometry: support circular ownership before Cartesian splitting.

Derived from frozen features_v3.py; v1/v2/v3 stay unchanged. Inputs are observed
XYZ, calibration identity and scan angles only. No mesh, target name, nominal
size, level count or evaluator tolerance enters discovery. All former datasets
are development regressions. Boundary interpolation is explicitly uncertain;
missing faces, open gaps and unsupported endpoints are never synthesized.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import combinations
import math
import os
from pathlib import Path
import sys
from typing import Any, Sequence

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
for _source in ("structured_light_system_algorithm_v1", "structured_light_system_geometry_v1"):
    _path = str(_ROOT / _source / "src" / "python")
    if _path not in sys.path:
        sys.path.insert(0, _path)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from structured_light.scan3d.continuous_measurement import (
    MeasurementValidity, fit_circle_diameter, segment_height, section_step)
from candidate.feature_fast import bin_groups, connected_cells
from candidate.feature_association import AssociationPolicy, associate_features
from virtual_scan import feature_parallel_r26


@dataclass(frozen=True)
class FeaturePolicy:
    section_height_mm: float = 0.5
    residual_mm: float = 0.25
    component_cell_mm: float = 1.0
    minimum_points: int = 12
    minimum_segment_height_mm: float = 1.0
    minimum_angular_coverage_rad: float = 1.5 * math.pi
    plateau_depth_tolerance_mm: float = 0.25
    section_sample_mm: float = 0.15
    plane_residual_mm: float = 0.1


@dataclass(frozen=True)
class FeaturePolicyV5(FeaturePolicy):
    """Observed co-height support gate; V4 numerical policy stays unchanged."""
    minimum_plane_z_overlap_mm: float = 1.0
    cylinder_endpoint_sector_count: int = 48
    cylinder_endpoint_minimum_points: int = 3


def _circle(points: np.ndarray) -> tuple[np.ndarray, float] | None:
    origin = points.mean(axis=0)
    xy = points - origin
    matrix = np.column_stack((2 * xy, np.ones(len(xy))))
    if np.linalg.matrix_rank(matrix) < 3:
        return None
    fit, *_ = np.linalg.lstsq(matrix, np.sum(xy * xy, axis=1), rcond=None)
    radius2 = fit[2] + fit[:2] @ fit[:2]
    return (origin + fit[:2], float(np.sqrt(radius2))) if radius2 > 0 else None


def _components(points: np.ndarray, cell: float) -> list[np.ndarray]:
    """Connected occupied XY cells; never interpolate empty cells.

    The integer-key implementation preserves the historical group order, DFS
    neighbour order and within-cell point order; see candidate.feature_fast.
    """
    return connected_cells(points, cell)


def _hull_reference(points: np.ndarray) -> np.ndarray:
    """Frozen pre-optimization hull, retained for legacy/A-B verification only."""
    rows = sorted(set(map(tuple, points)))
    if len(rows) < 3:
        return np.asarray(rows)
    def cross(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])
    lower, upper = [], []
    for row in rows:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], row) <= 0:
            lower.pop()
        lower.append(row)
    for row in reversed(rows):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], row) <= 0:
            upper.pop()
        upper.append(row)
    return np.asarray(lower[:-1] + upper[:-1])


def _hull(points: np.ndarray) -> np.ndarray:
    """Monotone-chain hull in the historical lexicographic vertex order.

    ``sorted(set(map(tuple, points)))`` is replaced by one ``lexsort`` plus an
    adjacent-row de-duplication and a 5-vertex Quickhull interior pre-filter.
    For finite input both produce the same sorted unique exterior row sequence,
    so the monotone chain visits the same hull vertices in the same order.
    """
    pts = np.asarray(points, dtype=float)
    if not len(pts):
        return np.asarray([])
    order = np.lexsort((pts[:, 1], pts[:, 0]))
    ordered = pts[order]
    if len(ordered) > 1:
        keep = np.empty(len(ordered), dtype=bool)
        keep[0] = True
        keep[1:] = (ordered[1:, 0] != ordered[:-1, 0]) | (ordered[1:, 1] != ordered[:-1, 1])
        ordered = ordered[keep]
    n_ord = len(ordered)
    if n_ord < 3:
        return np.asarray(ordered.tolist())
    if n_ord >= 32:
        x = ordered[:, 0]
        y = ordered[:, 1]
        p0x, p0y = float(x[0]), float(y[0])
        p1x, p1y = float(x[-1]), float(y[-1])
        idx_ymin = int(np.argmin(y))
        pbx, pby = float(x[idx_ymin]), float(y[idx_ymin])
        idx_ymax = int(np.argmax(y))
        ptx, pty = float(x[idx_ymax]), float(y[idx_ymax])

        c_p0_p1 = (p1x - p0x) * (y - p0y) - (p1y - p0y) * (x - p0x)
        low_mask = c_p0_p1 <= 1e-7
        if 0 < idx_ymin < n_ord - 1:
            c_p0_pb = (pbx - p0x) * (y[:idx_ymin + 1] - p0y) - (pby - p0y) * (x[:idx_ymin + 1] - p0x)
            idx_l1 = int(np.argmin(c_p0_pb))
            pl1x, pl1y = float(x[idx_l1]), float(y[idx_l1])
            m1 = np.empty(idx_ymin + 1, dtype=bool)
            if 0 < idx_l1 < idx_ymin:
                m1[:idx_l1 + 1] = ((pl1x - p0x) * (y[:idx_l1 + 1] - p0y) - (pl1y - p0y) * (x[:idx_l1 + 1] - p0x)) <= 1e-7
                m1[idx_l1 + 1:] = ((pbx - pl1x) * (y[idx_l1 + 1:idx_ymin + 1] - pl1y) - (pby - pl1y) * (x[idx_l1 + 1:idx_ymin + 1] - pl1x)) <= 1e-7
                m1[idx_l1] = True
            else:
                m1[:] = c_p0_pb <= 1e-7

            c_pb_p1 = (p1x - pbx) * (y[idx_ymin:] - pby) - (p1y - pby) * (x[idx_ymin:] - pbx)
            idx_l2_rel = int(np.argmin(c_pb_p1))
            idx_l2 = idx_ymin + idx_l2_rel
            pl2x, pl2y = float(x[idx_l2]), float(y[idx_l2])
            m2 = np.empty(n_ord - idx_ymin, dtype=bool)
            if idx_ymin < idx_l2 < n_ord - 1:
                m2[:idx_l2_rel + 1] = ((pl2x - pbx) * (y[idx_ymin:idx_l2 + 1] - pby) - (pl2y - pby) * (x[idx_ymin:idx_l2 + 1] - pbx)) <= 1e-7
                m2[idx_l2_rel + 1:] = ((p1x - pl2x) * (y[idx_l2 + 1:] - pl2y) - (p1y - pl2y) * (x[idx_l2 + 1:] - pl2x)) <= 1e-7
                m2[idx_l2_rel] = True
            else:
                m2[:] = c_pb_p1 <= 1e-7

            low_mask[:idx_ymin + 1] &= m1
            low_mask[idx_ymin:] &= m2
        low_mask[0] = True
        low_mask[-1] = True
        low_mask[idx_ymin] = True
        lower_rows = ordered[low_mask].tolist()

        up_mask = c_p0_p1 >= -1e-7
        if 0 < idx_ymax < n_ord - 1:
            c_p0_pt = (ptx - p0x) * (y[:idx_ymax + 1] - p0y) - (pty - p0y) * (x[:idx_ymax + 1] - p0x)
            idx_u1 = int(np.argmax(c_p0_pt))
            pu1x, pu1y = float(x[idx_u1]), float(y[idx_u1])
            u1 = np.empty(idx_ymax + 1, dtype=bool)
            if 0 < idx_u1 < idx_ymax:
                u1[:idx_u1 + 1] = ((pu1x - p0x) * (y[:idx_u1 + 1] - p0y) - (pu1y - p0y) * (x[:idx_u1 + 1] - p0x)) >= -1e-7
                u1[idx_u1 + 1:] = ((ptx - pu1x) * (y[idx_u1 + 1:idx_ymax + 1] - pu1y) - (pty - pu1y) * (x[idx_u1 + 1:idx_ymax + 1] - pu1x)) >= -1e-7
                u1[idx_u1] = True
            else:
                u1[:] = c_p0_pt >= -1e-7

            c_pt_p1 = (p1x - ptx) * (y[idx_ymax:] - pty) - (p1y - pty) * (x[idx_ymax:] - ptx)
            idx_u2_rel = int(np.argmax(c_pt_p1))
            idx_u2 = idx_ymax + idx_u2_rel
            pu2x, pu2y = float(x[idx_u2]), float(y[idx_u2])
            u2 = np.empty(n_ord - idx_ymax, dtype=bool)
            if idx_ymax < idx_u2 < n_ord - 1:
                u2[:idx_u2_rel + 1] = ((pu2x - ptx) * (y[idx_ymax:idx_u2 + 1] - pty) - (pu2y - pty) * (x[idx_ymax:idx_u2 + 1] - ptx)) >= -1e-7
                u2[idx_u2_rel + 1:] = ((p1x - pu2x) * (y[idx_u2 + 1:] - pu2y) - (p1y - pu2y) * (x[idx_u2 + 1:] - pu2x)) >= -1e-7
                u2[idx_u2_rel] = True
            else:
                u2[:] = c_pt_p1 >= -1e-7

            up_mask[:idx_ymax + 1] &= u1
            up_mask[idx_ymax:] &= u2
        up_mask[0] = True
        up_mask[-1] = True
        up_mask[idx_ymax] = True
        upper_rows = ordered[up_mask][::-1].tolist()
    else:
        rows = ordered.tolist()
        lower_rows = rows
        upper_rows = list(reversed(rows))

    lower, upper = [], []
    for row in lower_rows:
        rx, ry = row[0], row[1]
        while len(lower) >= 2:
            a = lower[-2]
            b = lower[-1]
            if (b[0] - a[0]) * (ry - a[1]) - (b[1] - a[1]) * (rx - a[0]) <= 0:
                lower.pop()
            else:
                break
        lower.append(row)
    for row in upper_rows:
        rx, ry = row[0], row[1]
        while len(upper) >= 2:
            a = upper[-2]
            b = upper[-1]
            if (b[0] - a[0]) * (ry - a[1]) - (b[1] - a[1]) * (rx - a[0]) <= 0:
                upper.pop()
            else:
                break
        upper.append(row)
    return np.asarray(lower[:-1] + upper[:-1], dtype=float)


def _fast_quantiles_002_998_axis1(stacked: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Exact match to np.quantile(stacked, [.002, .998], axis=1) for 3D (M, N, 2) finite float64."""
    n = stacked.shape[1]
    work = np.ascontiguousarray(np.swapaxes(stacked, 1, 2))
    v0 = 0.002 * (n - 1)
    v1 = 0.998 * (n - 1)
    i0 = int(math.floor(v0))
    j0 = i0 + 1 if i0 + 1 < n else n - 1
    g0 = v0 - i0
    i1 = int(math.floor(v1))
    j1 = i1 + 1 if i1 + 1 < n else n - 1
    g1 = v1 - i1
    if i0 == 0 and j0 == 1 and i1 == n - 2 and j1 == n - 1:
        work.partition((1, n - 2), axis=-1)
        a0 = work[:, :, 0]
        b0 = work[:, :, 1]
        a1 = work[:, :, n - 2]
        b1 = work[:, :, n - 1]
    elif i0 == 1 and j0 == 2 and i1 == n - 3 and j1 == n - 2:
        work.partition((2, n - 3), axis=-1)
        a0 = np.maximum(work[:, :, 0], work[:, :, 1])
        b0 = work[:, :, 2]
        a1 = work[:, :, n - 3]
        b1 = np.minimum(work[:, :, n - 2], work[:, :, n - 1])
    else:
        work.partition((i0, j0, i1, j1), axis=-1)
        a0 = work[:, :, i0]
        b0 = work[:, :, j0]
        a1 = work[:, :, i1]
        b1 = work[:, :, j1]
    lo = (b0 - (b0 - a0) * (1.0 - g0)) if g0 >= 0.5 else (a0 + (b0 - a0) * g0)
    hi = (b1 - (b1 - a1) * (1.0 - g1)) if g1 >= 0.5 else (a1 + (b1 - a1) * g1)
    return lo, hi


def _rectangle_impl(points: np.ndarray, policy: FeaturePolicy,
                    hull_function, *, batch_quantiles=True) -> dict[str, Any] | None:
    hull = hull_function(points)
    if len(hull) < 4:
        return None
    edges = np.roll(hull, -1, axis=0) - hull
    angles = np.unique(np.round(np.arctan2(edges[:, 1], edges[:, 0]) % (math.pi/2), 6))
    best = None
    if not batch_quantiles:
        for angle in angles:
            c, sine = math.cos(angle), math.sin(angle)
            basis = np.asarray([[c, -sine], [sine, c]])
            local = points @ basis
            lo, hi = np.quantile(local, [.002, .998], axis=0)
            extent = hi - lo
            if min(extent) <= policy.residual_mm * 2:
                continue
            distances = np.column_stack((abs(local[:, 0]-lo[0]), abs(local[:, 0]-hi[0]),
                                         abs(local[:, 1]-lo[1]), abs(local[:, 1]-hi[1])))
            residual = distances.min(axis=1)
            rms = float(np.sqrt(np.mean(residual ** 2)))
            if rms > policy.residual_mm:
                continue
            supported = True
            for side in range(4):
                subset = local[distances[:, side] <= policy.residual_mm]
                transverse = 1 if side < 2 else 0
                if len(subset) < 3 or np.ptp(subset[:, transverse]) < .5 * extent[transverse]:
                    supported = False
                    break
            if not supported:
                continue
            score = rms
            if best is None or score < best["residual"]:
                center = ((lo+hi)/2) @ basis.T
                if extent[0] > extent[1]:
                    extent = extent[::-1]
                    basis = basis[:, ::-1]
                best = {"shape": "rectangular_prism", "center": center,
                        "sizes": extent, "basis": basis, "residual": rms}
        return best
    # Keep each 2D point@basis multiplication identical to the frozen expression;
    # evaluate quantiles and RMS gates vectorized across each 32-angle block.
    chunk = 32
    res_limit = policy.residual_mm
    min_ext = res_limit * 2
    n_pts = len(points)
    for start in range(0, len(angles), chunk):
        ang_chunk = angles[start:start+chunk]
        m = len(ang_chunk)
        bases = np.empty((m, 2, 2), dtype=float)
        stacked = np.empty((m, n_pts, 2), dtype=float)
        for i, angle in enumerate(ang_chunk):
            c, sine = math.cos(angle), math.sin(angle)
            basis = bases[i]
            basis[0, 0] = c
            basis[0, 1] = -sine
            basis[1, 0] = sine
            basis[1, 1] = c
            np.matmul(points, basis, out=stacked[i])
        lo_all, hi_all = _fast_quantiles_002_998_axis1(stacked)
        ext_all = hi_all - lo_all
        valid_ext = np.minimum(ext_all[:, 0], ext_all[:, 1]) > min_ext
        if not valid_ext.any():
            continue
        ux = stacked[:, :, 0]
        uy = stacked[:, :, 1]
        # Preserve the frozen min grouping while reusing three work arrays.
        # The former expression materialized four side-distance arrays and
        # three intermediate minimum arrays for every 32-angle block.
        dx_min = np.empty_like(ux)
        dy_min = np.empty_like(uy)
        side_scratch = np.empty_like(ux)
        np.subtract(ux, lo_all[:, None, 0], out=dx_min)
        np.abs(dx_min, out=dx_min)
        np.subtract(ux, hi_all[:, None, 0], out=side_scratch)
        np.abs(side_scratch, out=side_scratch)
        np.minimum(dx_min, side_scratch, out=dx_min)
        np.subtract(uy, lo_all[:, None, 1], out=dy_min)
        np.abs(dy_min, out=dy_min)
        np.subtract(uy, hi_all[:, None, 1], out=side_scratch)
        np.abs(side_scratch, out=side_scratch)
        np.minimum(dy_min, side_scratch, out=dy_min)
        np.minimum(dx_min, dy_min, out=dx_min)
        dx_min *= dx_min
        rms_all = np.sqrt(dx_min.mean(axis=1))
        candidates = np.flatnonzero(valid_ext & (rms_all <= res_limit))
        for index in candidates:
            idx = int(index)
            rms = float(rms_all[idx])
            if best is not None and rms >= best["residual"]:
                continue
            local = stacked[idx]
            extent = ext_all[idx].copy()
            basis = bases[idx].copy()
            lo = lo_all[idx]
            hi = hi_all[idx]
            dx0 = np.abs(ux[idx] - lo[0])
            dx1 = np.abs(ux[idx] - hi[0])
            dy0 = np.abs(uy[idx] - lo[1])
            dy1 = np.abs(uy[idx] - hi[1])
            d_sides = (dx0, dx1, dy0, dy1)
            supported = True
            for side in range(4):
                subset = local[d_sides[side] <= res_limit]
                transverse = 1 if side < 2 else 0
                if len(subset) < 3 or np.ptp(subset[:, transverse]) < .5 * extent[transverse]:
                    supported = False
                    break
            if not supported:
                continue
            center = ((lo+hi)/2) @ basis.T
            if extent[0] > extent[1]:
                extent = extent[::-1]
                basis = basis[:, ::-1]
            best = {"shape": "rectangular_prism", "center": center,
                    "sizes": extent, "basis": basis, "residual": rms}
    return best


def _rectangle(points: np.ndarray, policy: FeaturePolicy) -> dict[str, Any] | None:
    return _rectangle_impl(points, policy, _hull)


def _rectangle_reference(points: np.ndarray, policy: FeaturePolicy) -> dict[str, Any] | None:
    """Frozen pre-optimization rectangle pass, retained for A-B verification."""
    return _rectangle_impl(points, policy, _hull_reference, batch_quantiles=False)


def _primitive_impl(points: np.ndarray, validity: MeasurementValidity, policy: FeaturePolicy,
                    rectangle_function, circle_function) -> dict[str, Any] | None:
    if len(points) < policy.minimum_points:
        return None
    # Deterministic thinning bounds the numeric fit cost, preserves angular order.
    if len(points) > 700:
        points = points[np.linspace(0, len(points)-1, 700).astype(int)]
    rectangle = rectangle_function(points, policy)
    fitted_circle = None
    # Check the algebraic circle gate before the 128-hypothesis RANSAC circle fit:
    # fitted_circle is only accepted when _circle(points) succeeds and its 90th
    # percentile residual is within policy.residual_mm.
    model = _circle(points)
    if model is not None:
        center, radius = model
        residual = abs(np.linalg.norm(points-center, axis=1)-radius)
        n_pts = len(points)
        if (int((residual <= policy.residual_mm).sum()) > int(math.floor(0.9 * (n_pts - 1)))
                and np.quantile(residual, .9) <= policy.residual_mm):
            circle = circle_function(points, validity=validity, min_points=policy.minimum_points,
                                     max_residual_mm=policy.residual_mm,
                                     min_angular_coverage_rad=policy.minimum_angular_coverage_rad)
            if circle.status == "available" and circle.point_count >= .75 * n_pts:
                fitted_circle = {"shape": "cylinder", "center": center,
                                 "sizes": np.asarray([circle.value_mm]),
                                 "residual": float(circle.residual_mm)}
    if rectangle is not None and (fitted_circle is None or rectangle["residual"] < fitted_circle["residual"]):
        return rectangle
    return fitted_circle


_ORIGINAL_TOKENS = frozenset({"0", "original", "frozen", "off", "legacy"})


def _rectangle_choice():
    """Optimized hull by default; exact frozen hull is an operational escape hatch."""
    if os.environ.get("R26_FEATURE_HULL", "").strip().lower() in _ORIGINAL_TOKENS:
        return _rectangle_reference
    return _rectangle


def _circle_choice():
    """Batched exact circle by default; frozen scorer is an escape hatch."""
    if os.environ.get("R26_FEATURE_CIRCLE", "").strip().lower() in _ORIGINAL_TOKENS:
        return fit_circle_diameter
    return feature_parallel_r26.fit_circle_diameter_batched


def _primitive(points: np.ndarray, validity: MeasurementValidity, policy: FeaturePolicy) -> dict[str, Any] | None:
    return _primitive_impl(points, validity, policy, _rectangle_choice(), _circle_choice())


def _primitive_reference(points: np.ndarray, validity: MeasurementValidity,
                         policy: FeaturePolicy) -> dict[str, Any] | None:
    """Frozen pre-optimization primitive pass, retained for A-B verification."""
    return _primitive_impl(points, validity, policy, _rectangle_reference, fit_circle_diameter)


def _record(kind: str, value: float | None, *, ids: np.ndarray, points: np.ndarray,
            calibration_id: str, reference: str, direction: Sequence[float],
            region_id: str, residual: float | None = None, reason: str | None = None,
            support_bounds: tuple[list[float], list[float]] | None = None,
            point_indices: list[int] | None = None,
            **extra: Any) -> dict[str, Any]:
    # Support bounds are precomputed once per solid track.  Only fall back to a
    # fresh observed-support copy when the caller did not supply them.
    if support_bounds is None and len(ids):
        observed = points[ids]
        minimum = observed.min(axis=0).tolist()
        maximum = observed.max(axis=0).tolist()
    elif support_bounds is not None:
        minimum, maximum = support_bounds
    else:
        minimum = maximum = None
    # ``point_indices`` is precomputed once per solid track.  It is only a cache
    # of ``ids.tolist()``: values and list order are byte-identical, while the
    # four/five records that share a large support avoid repeated conversion.
    if point_indices is None:
        point_indices = ids.tolist()
    return {"type": kind, "feature_type": kind, "region_id": region_id,
            "value_mm": None if value is None else float(value), "unit": "mm", "units": "mm",
            "status": "available" if value is not None else "unavailable", "reason": reason,
            "support": {"point_count": len(ids), "point_indices": point_indices,
                        "minimum_mm": minimum, "maximum_mm": maximum},
            "reference": reference, "reference_basis": reference,
            "direction": list(map(float, direction)), "residual_mm": residual,
            "calibration_id": calibration_id, "evidence_type": "SIMULATION_ONLY",
            "physical_validation": False, **extra}


def _rectangle_height_support(points: np.ndarray, candidate_ids: np.ndarray,
                              center: np.ndarray, sizes: np.ndarray, basis: np.ndarray,
                              policy: FeaturePolicy) -> tuple[np.ndarray, tuple[float, float] | None, list[dict[str, Any]]]:
    """Keep the Z interval independently supported by all four face interiors.

    Adjacent steps can share three side planes. A point close to any shared
    plane does not establish ownership by the current cross-section segment.
    Face interiors exclude crossings with a different face at rectangle corners;
    the intersection of four measured Z supports bounds this specific section.
    No unseen endpoint, missing face or cross-section is extrapolated.
    """
    local = (points[candidate_ids, :2]-center) @ basis
    contained = np.all(abs(local) <= sizes/2+policy.residual_mm, axis=1)
    edge_distance = abs(abs(local)-sizes/2).min(axis=1)
    side_ids = candidate_ids[contained & (edge_distance <= policy.residual_mm)]
    face_supports = []
    ranges = []
    for axis in range(2):
        transverse = 1-axis
        # Relative trimming is independent of target dimensions. It rejects
        # neighboring face/plane intersections as support for this face.
        interior = abs(local[:, transverse]) <= .4*sizes[transverse]
        for sign in (-1., 1.):
            supported = (contained & interior
                         & (abs(local[:, axis]-sign*sizes[axis]/2) <= policy.residual_mm))
            ids = candidate_ids[supported]
            limits = None
            active_ids = []
            if len(ids) >= 3:
                # Pixel rows at different transverse locations need not have
                # the same Z sampling phase. Independently bound four face
                # interior patches, rather than demanding their simultaneous
                # presence in a thin horizontal slice. A narrow neighboring
                # rail cannot establish endpoints in the central patches.
                coordinate = local[supported, transverse]
                edges = np.linspace(-.4*sizes[transverse], .4*sizes[transverse], 5)
                patch_ranges = []
                for lower, upper in zip(edges, edges[1:]):
                    selected = (coordinate >= lower) & (coordinate <= upper)
                    transverse_values = np.unique(coordinate[selected])
                    if (len(transverse_values) >= 3
                            and np.ptp(transverse_values) >= .25*(upper-lower)):
                        active_ids.append(ids[selected])
                        patch_ranges.append([float(points[ids[selected], 2].min()),
                                             float(points[ids[selected], 2].max())])
                if len(patch_ranges) == 4:
                    limits = [max(r[0] for r in patch_ranges), min(r[1] for r in patch_ranges)]
            if limits is not None and limits[1] > limits[0]:
                active_ids = np.concatenate(active_ids)
                ranges.append(limits)
            else:
                limits = None
            face_supports.append({"axis": axis, "side": int(sign), "point_count": len(ids),
                                  "height_range_mm": limits,
                                  "distributed_height_point_count": len(active_ids) if isinstance(active_ids, np.ndarray) else 0})
    if len(ranges) != 4:
        return side_ids, None, face_supports
    lo, hi = max(r[0] for r in ranges), min(r[1] for r in ranges)
    if hi <= lo:
        return side_ids, None, face_supports
    owned = (contained & (edge_distance <= policy.residual_mm)
             & (points[candidate_ids, 2] >= lo) & (points[candidate_ids, 2] <= hi))
    return candidate_ids[owned], (lo, hi), face_supports



def _cylinder_endpoint_consensus(points, ids, center, policy):
    """V5: endpoint evidence must repeat across the observed circumference.

    A single radially plausible outlier cannot enlarge a measured segment.
    Medians of per-sector extrema are estimates of observed side endpoints,
    never CAD endpoints; the observed spread remains explicit uncertainty.
    """
    count = policy.cylinder_endpoint_sector_count
    xy = points[ids, :2] - center
    sector = np.floor((np.arctan2(xy[:, 1], xy[:, 0]) + math.pi)
                      * count / math.tau).astype(int) % count
    bounds = []
    active = []
    for index in range(count):
        z = points[ids[sector == index], 2]
        if len(z) >= policy.cylinder_endpoint_minimum_points:
            bounds.append((float(z.min()), float(z.max())))
            active.append(index)
    if len(active) < math.ceil(count * policy.minimum_angular_coverage_rad / math.tau):
        return None
    gap = max(np.diff(np.r_[active, active[0] + count])) * math.tau / count
    if math.tau - gap < policy.minimum_angular_coverage_rad:
        return None
    values = np.asarray(bounds)
    endpoints = np.median(values, axis=0)
    if endpoints[1] - endpoints[0] < policy.minimum_segment_height_mm:
        return None
    return endpoints.tolist(), dict(
        method="median_of_distributed_observed_sector_extrema",
        sectors=active, sector_count=count,
        endpoint_spread_p10_p90_mm=np.quantile(values, [.1, .9], axis=0).tolist(),
        physical_accuracy_verified=False)


def _solid_features(points, calibration_id, reference, validity, policy, planes=(),
                    workers=None):
    zmin, zmax = float(points[:, 2].min()), float(points[:, 2].max())
    bins = np.floor((points[:, 2]-zmin)/policy.section_height_mm).astype(int)
    # The 275 Z levels are independent of the sequential track/partial-track
    # state.  ``run_slice_jobs`` evaluates the exact frozen per-level fits (with
    # the circle helper batched below) and returns them in ``bin_groups`` order;
    # the replay loop below is the historical loop with its local fit results
    # supplied instead of recomputed.  No model, gate, order or seed changes.
    levels = bin_groups(bins)
    workers, workers_reason = feature_parallel_r26.resolve_workers(
        workers, len(points), len(levels))
    sliced = feature_parallel_r26.run_slice_jobs(
        points, levels, validity, policy, _primitive, workers, workers_reason)
    if len(sliced) != len(levels):
        raise RuntimeError("slice executor returned %d/%d levels" % (len(sliced), len(levels)))
    zindex = feature_parallel_r26.ZIndex(points)
    tracks: list[dict[str, Any]] = []
    partial_tracks: list[dict[str, Any]] = []
    for (level, _ids), level_models in zip(levels, sliced):
        for support, model, observed_center in level_models:
            if model is None:
                if len(support) >= policy.minimum_points:
                    partial = next((t for t in partial_tracks
                                    if t["level"] >= level-2
                                    and np.linalg.norm(t["center"]-observed_center) < 2*policy.component_cell_mm), None)
                    if partial is None:
                        partial_tracks.append({"level": int(level), "center": observed_center,
                                               "ids": [support]})
                    else:
                        partial["level"] = int(level)
                        partial["ids"].append(support)
                continue
            matching = []
            for track in tracks:
                if track["level"] < level - 2 or track["shape"] != model["shape"]:
                    continue
                center_error = np.linalg.norm(track["center"]-model["center"])
                size_error = np.max(abs(track["sizes"]-model["sizes"]))
                if center_error < 2*policy.residual_mm and size_error < 3*policy.residual_mm:
                    matching.append((center_error+size_error, track))
            if matching:
                track = min(matching, key=lambda item: item[0])[1]
                track["ids"].append(support)
                track["models"].append(model)
                track["level"] = int(level)
            else:
                tracks.append({**model, "ids": [support], "models": [model], "level": int(level)})
    output = []
    tracks.sort(key=lambda t: (points[np.concatenate(t["ids"]), 2].min(), *t["center"]))
    for track in tracks:
        ids = np.concatenate(track["ids"])
        lo, hi = float(points[ids, 2].min()), float(points[ids, 2].max())
        if hi-lo < policy.minimum_segment_height_mm:
            continue
        models = track["models"]
        sizes = np.median([model["sizes"] for model in models], axis=0)
        center = np.median([model["center"] for model in models], axis=0)
        # Mixed boundary slices may fail the full-outline gate. Recover only
        # already observed side samples near the fitted track endpoints; this
        # does not interpolate a missing boundary or add any synthetic point.
        nearby = zindex.window(lo-policy.section_height_mm, hi+policy.section_height_mm)
        height_reason = None
        height_support = None
        common_face_range = None
        cylinder_endpoint_support = None
        xy = points[nearby, :2]-center
        if track["shape"] == "cylinder":
            side_residual = abs(np.linalg.norm(xy, axis=1)-sizes[0]/2)
            recovered = nearby[side_residual <= policy.residual_mm]
            ids = np.unique(np.r_[ids, recovered])
            lo, hi = float(points[ids, 2].min()), float(points[ids, 2].max())
            if isinstance(policy, FeaturePolicyV5):
                consensus = _cylinder_endpoint_consensus(points, ids, center, policy)
                if consensus is None:
                    height_reason = "insufficient_distributed_cylinder_endpoint_support"
                else:
                    (lo, hi), cylinder_endpoint_support = consensus
        else:
            original_ids = ids
            original_range = (lo, hi)
            ids, owned_range, height_support = _rectangle_height_support(
                points, nearby, center, sizes, track["basis"], policy)
            common_face_range = owned_range
            if owned_range is None:
                height_reason = "insufficient_independent_four_face_height_support"
                ids = original_ids
            elif (owned_range[0]-original_range[0] > 2*policy.section_height_mm
                  or original_range[1]-owned_range[1] > 2*policy.section_height_mm):
                # Intersecting visible face patches can produce a local common
                # view far inside a taller track. Its length is not that solid's
                # height. Only the two endpoint bins (mixed slice + recovery)
                # may be resolved by this ownership refinement.
                height_reason = "insufficient_distributed_face_support_at_segment_endpoints"
                ids = original_ids
            else:
                lo, hi = owned_range
                if hi-lo < policy.minimum_segment_height_mm:
                    height_reason = "insufficient_common_four_face_height_extent"
        if track["shape"] == "rectangular_prism" and planes:
            # A small disconnected corner patch is not a separate solid. Each
            # of its four proposed sides must also have a distributed global
            # observed-plane consensus at that actual position and orientation.
            supported_sides = 0
            for axis in range(2):
                normal = track["basis"][:, axis]
                for sign in (-1., 1.):
                    offset = float(center @ normal + sign*sizes[axis]/2)
                    if any(abs(normal @ plane["normal"]) > .995
                           and abs(offset-np.sign(normal @ plane["normal"])*plane["offset"]) <= 2*policy.residual_mm
                           for plane in planes):
                        supported_sides += 1
            if supported_sides < 4:
                continue
        residual = float(np.sqrt(np.mean([m["residual"]**2 for m in models])))
        region_id = f"solid_{len({r['region_id'] for r in output})}"
        observed = points[ids]
        bounds = (observed.min(axis=0).tolist(), observed.max(axis=0).tolist()) if len(ids) else (None, None)
        # One list conversion serves all four/five records of this track; the
        # emitted list values and order are identical to ids.tolist() per record.
        point_indices = ids.tolist()
        common = dict(ids=ids, points=points, calibration_id=calibration_id, reference=reference,
                      region_id=region_id, residual=residual, shape=track["shape"],
                      center_xy_mm=center.tolist(), observed_height_range_mm=[lo, hi],
                      support_bounds=bounds, point_indices=point_indices)
        if track["shape"] == "cylinder":
            output.append(_record("diameter", float(sizes[0]), direction=[1, 0, 0], **common))
            height_type = "segment_height"
        else:
            basis = track["basis"]
            output.append(_record("prism_width", float(sizes[0]), direction=[*basis[:, 0], 0], **common))
            output.append(_record("prism_depth", float(sizes[1]), direction=[*basis[:, 1], 0], **common))
            height_type = "prism_height"
        # Bounds are observed section limits; no nominal bottom/top substituted.
        bottom = points[ids][points[ids, 2] <= lo+policy.section_height_mm]
        top = points[ids][points[ids, 2] >= hi-policy.section_height_mm]
        measured = segment_height(bottom, top, validity=validity)
        # Slice center medians lose half a section at each end. Expose measured
        # support extrema for height; resolution uncertainty stays in the record.
        height = hi-lo if measured.status == "available" and height_reason is None else None
        output.append(_record(height_type, height, direction=[0, 0, 1],
                              reason=height_reason or measured.reason,
                              boundary_resolution_mm=policy.section_height_mm,
                              endpoint_method=("intersection_of_four_observed_face_interiors"
                                               if height_support is not None else "observed_side_extrema"),
                              face_height_support=height_support,
                              four_face_common_height_range_mm=common_face_range,
                              **common))
        if cylinder_endpoint_support is not None:
            output[-1]["cylinder_endpoint_support"] = cylinder_endpoint_support
            output[-1]["endpoint_method"] = cylinder_endpoint_support["method"]
        for axis, kind in enumerate(("center_x", "center_y")):
            output.append(_record(kind, float(center[axis]), direction=np.eye(3)[axis], **common))
    for track in partial_tracks:
        ids = np.concatenate(track["ids"])
        if np.ptp(points[ids, 2]) < policy.minimum_segment_height_mm:
            continue
        output.append(_record("unresolved_component", None, ids=ids, points=points,
                              calibration_id=calibration_id, reference=reference, direction=[0, 0, 1],
                              region_id=f"partial_{len(output)}",
                              reason="insufficient_circle_or_four_side_observed_support",
                              observed_centroid_mm=np.median(points[ids], axis=0).tolist()))
    return _refine_cylinder_boundaries(points, output, policy, zindex=zindex)


def _solid_features_legacy(points, calibration_id, reference, validity, policy, planes=()):
    """Frozen pre-optimization solid pass, retained for A-B verification only.

    It differs from :func:`_solid_features` intentionally: original per-level
    fit loop, original full-cloud Z scan, original ``fit_circle_diameter`` and
    original hull/rectangle.  ``discover_features*`` selects it only through the
    explicit ``legacy`` keyword / ``R26_FEATURE_LEGACY=1``.
    """
    zmin, zmax = float(points[:, 2].min()), float(points[:, 2].max())
    bins = np.floor((points[:, 2]-zmin)/policy.section_height_mm).astype(int)
    tracks: list[dict[str, Any]] = []
    partial_tracks: list[dict[str, Any]] = []
    for level, ids in bin_groups(bins):
        # One stable sort replaces one full-array scan per Z level; the indices
        # are ascending and therefore identical to np.flatnonzero(bins==level).
        whole = _primitive_reference(points[ids, :2], validity, policy)
        whole_indices = np.arange(len(ids))
        if whole is not None and whole["shape"] == "cylinder":
            radial = abs(np.linalg.norm(points[ids, :2]-whole["center"], axis=1)
                         - whole["sizes"][0]/2)
            owned = np.flatnonzero(radial <= policy.residual_mm)
            remainder = np.flatnonzero(radial > policy.residual_mm)
            clusters = [owned]
            # Circle consensus never consumes unrepresented points. A second
            # independently observed object still reaches ordinary components.
            clusters.extend(remainder[c] for c in _components(
                points[ids[remainder], :2], policy.component_cell_mm))
        else:
            clusters = _components(points[ids, :2], policy.component_cell_mm)
        for cluster in clusters:
            support = ids[cluster]
            # A slice that is one single component was already fitted above.
            # Reusing that exact result avoids a duplicate 128-hypothesis fit
            # without changing any input, order or gate.
            if len(cluster) == len(ids) and np.array_equal(cluster, whole_indices):
                model = whole
            else:
                model = _primitive_reference(points[support, :2], validity, policy)
            if model is None:
                if len(support) >= policy.minimum_points:
                    observed_center = np.median(points[support, :2], axis=0)
                    partial = next((t for t in partial_tracks
                                    if t["level"] >= level-2
                                    and np.linalg.norm(t["center"]-observed_center) < 2*policy.component_cell_mm), None)
                    if partial is None:
                        partial_tracks.append({"level": int(level), "center": observed_center,
                                               "ids": [support]})
                    else:
                        partial["level"] = int(level)
                        partial["ids"].append(support)
                continue
            matching = []
            for track in tracks:
                if track["level"] < level - 2 or track["shape"] != model["shape"]:
                    continue
                center_error = np.linalg.norm(track["center"]-model["center"])
                size_error = np.max(abs(track["sizes"]-model["sizes"]))
                if center_error < 2*policy.residual_mm and size_error < 3*policy.residual_mm:
                    matching.append((center_error+size_error, track))
            if matching:
                track = min(matching, key=lambda item: item[0])[1]
                track["ids"].append(support)
                track["models"].append(model)
                track["level"] = int(level)
            else:
                tracks.append({**model, "ids": [support], "models": [model], "level": int(level)})
    output = []
    tracks.sort(key=lambda t: (points[np.concatenate(t["ids"]), 2].min(), *t["center"]))
    for track in tracks:
        ids = np.concatenate(track["ids"])
        lo, hi = float(points[ids, 2].min()), float(points[ids, 2].max())
        if hi-lo < policy.minimum_segment_height_mm:
            continue
        models = track["models"]
        sizes = np.median([model["sizes"] for model in models], axis=0)
        center = np.median([model["center"] for model in models], axis=0)
        # Mixed boundary slices may fail the full-outline gate. Recover only
        # already observed side samples near the fitted track endpoints; this
        # does not interpolate a missing boundary or add any synthetic point.
        nearby = np.flatnonzero((points[:, 2] >= lo-policy.section_height_mm)
                                 & (points[:, 2] <= hi+policy.section_height_mm))
        height_reason = None
        height_support = None
        common_face_range = None
        xy = points[nearby, :2]-center
        if track["shape"] == "cylinder":
            side_residual = abs(np.linalg.norm(xy, axis=1)-sizes[0]/2)
            recovered = nearby[side_residual <= policy.residual_mm]
            ids = np.unique(np.r_[ids, recovered])
            lo, hi = float(points[ids, 2].min()), float(points[ids, 2].max())
        else:
            original_ids = ids
            original_range = (lo, hi)
            ids, owned_range, height_support = _rectangle_height_support(
                points, nearby, center, sizes, track["basis"], policy)
            common_face_range = owned_range
            if owned_range is None:
                height_reason = "insufficient_independent_four_face_height_support"
                ids = original_ids
            elif (owned_range[0]-original_range[0] > 2*policy.section_height_mm
                  or original_range[1]-owned_range[1] > 2*policy.section_height_mm):
                # Intersecting visible face patches can produce a local common
                # view far inside a taller track. Its length is not that solid's
                # height. Only the two endpoint bins (mixed slice + recovery)
                # may be resolved by this ownership refinement.
                height_reason = "insufficient_distributed_face_support_at_segment_endpoints"
                ids = original_ids
            else:
                lo, hi = owned_range
                if hi-lo < policy.minimum_segment_height_mm:
                    height_reason = "insufficient_common_four_face_height_extent"
        if track["shape"] == "rectangular_prism" and planes:
            # A small disconnected corner patch is not a separate solid. Each
            # of its four proposed sides must also have a distributed global
            # observed-plane consensus at that actual position and orientation.
            supported_sides = 0
            for axis in range(2):
                normal = track["basis"][:, axis]
                for sign in (-1., 1.):
                    offset = float(center @ normal + sign*sizes[axis]/2)
                    if any(abs(normal @ plane["normal"]) > .995
                           and abs(offset-np.sign(normal @ plane["normal"])*plane["offset"]) <= 2*policy.residual_mm
                           for plane in planes):
                        supported_sides += 1
            if supported_sides < 4:
                continue
        residual = float(np.sqrt(np.mean([m["residual"]**2 for m in models])))
        region_id = f"solid_{len({r['region_id'] for r in output})}"
        observed = points[ids]
        bounds = (observed.min(axis=0).tolist(), observed.max(axis=0).tolist()) if len(ids) else (None, None)
        common = dict(ids=ids, points=points, calibration_id=calibration_id, reference=reference,
                      region_id=region_id, residual=residual, shape=track["shape"],
                      center_xy_mm=center.tolist(), observed_height_range_mm=[lo, hi],
                      support_bounds=bounds)
        if track["shape"] == "cylinder":
            output.append(_record("diameter", float(sizes[0]), direction=[1, 0, 0], **common))
            height_type = "segment_height"
        else:
            basis = track["basis"]
            output.append(_record("prism_width", float(sizes[0]), direction=[*basis[:, 0], 0], **common))
            output.append(_record("prism_depth", float(sizes[1]), direction=[*basis[:, 1], 0], **common))
            height_type = "prism_height"
        # Bounds are observed section limits; no nominal bottom/top substituted.
        bottom = points[ids][points[ids, 2] <= lo+policy.section_height_mm]
        top = points[ids][points[ids, 2] >= hi-policy.section_height_mm]
        measured = segment_height(bottom, top, validity=validity)
        # Slice center medians lose half a section at each end. Expose measured
        # support extrema for height; resolution uncertainty stays in the record.
        height = hi-lo if measured.status == "available" and height_reason is None else None
        output.append(_record(height_type, height, direction=[0, 0, 1],
                              reason=height_reason or measured.reason,
                              boundary_resolution_mm=policy.section_height_mm,
                              endpoint_method=("intersection_of_four_observed_face_interiors"
                                               if height_support is not None else "observed_side_extrema"),
                              face_height_support=height_support,
                              four_face_common_height_range_mm=common_face_range,
                              **common))
        for axis, kind in enumerate(("center_x", "center_y")):
            output.append(_record(kind, float(center[axis]), direction=np.eye(3)[axis], **common))
    for track in partial_tracks:
        ids = np.concatenate(track["ids"])
        if np.ptp(points[ids, 2]) < policy.minimum_segment_height_mm:
            continue
        output.append(_record("unresolved_component", None, ids=ids, points=points,
                              calibration_id=calibration_id, reference=reference, direction=[0, 0, 1],
                              region_id=f"partial_{len(output)}",
                              reason="insufficient_circle_or_four_side_observed_support",
                              observed_centroid_mm=np.median(points[ids], axis=0).tolist()))
    return _refine_cylinder_boundaries(points, output, policy)


def _refine_cylinder_boundaries(points, features, policy, zindex=None):
    """Interpolate only transitions bracketed by two observed circular sides.

    Accepted section slices identify the sides but their endpoints are inward
    sampled limits. Require different radii, the same learned center, nearby
    endpoints, and independently measured lower/upper support over >=270 deg.
    Each angular sector supplies a closed observation bracket. Their common
    intersection bounds one transition; no common interval means no refinement.
    No extrapolation is made at free outer endpoints or across missing data.

    ``zindex`` limits each candidate transition to the closed Z interval that its
    two side masks can select.  That interval is a strict superset of both masks,
    so the candidate set/order is identical while the frozen full-cloud scan is
    retained when ``zindex`` is ``None`` (legacy/A-B path).
    """
    diameters = [f for f in features if f["type"] == "diameter"]
    heights = {f["region_id"]: f for f in features if f["type"] == "segment_height"}
    resolved = {}
    for lower, upper in combinations(diameters, 2):
        if lower["observed_height_range_mm"][0] > upper["observed_height_range_mm"][0]:
            lower, upper = upper, lower
        lo = lower["observed_height_range_mm"][1]
        hi = upper["observed_height_range_mm"][0]
        gap = hi-lo
        centers = [np.asarray(f["center_xy_mm"]) for f in (lower, upper)]
        if (gap <= 0. or gap > 2*policy.section_height_mm
                or np.linalg.norm(centers[0]-centers[1]) >= 2*policy.residual_mm
                or abs(lower["value_mm"]-upper["value_mm"]) <= 4*policy.residual_mm):
            continue
        # Avoid pairing nonadjacent surfaces through an intervening track.
        if any(f is not lower and f is not upper
               and f["observed_height_range_mm"][0] < hi
               and f["observed_height_range_mm"][1] > lo for f in diameters):
            continue
        center = (centers[0]+centers[1])/2
        sector_count = 48
        if zindex is None:
            xy = points[:, :2]-center
            radius = np.linalg.norm(xy, axis=1)
            sectors = np.floor((np.arctan2(xy[:, 1], xy[:, 0])+math.pi)
                               *sector_count/math.tau).astype(int) % sector_count
            side_ids = []
            side_sectors = []
            for feature, endpoint in ((lower, lo), (upper, hi)):
                mask = ((abs(radius-feature["value_mm"]/2) <= policy.plane_residual_mm)
                        & (abs(points[:, 2]-endpoint) <= policy.section_height_mm))
                side_ids.append(np.flatnonzero(mask))
                side_sectors.append(sectors[side_ids[-1]])
        else:
            window = zindex.window(lo-policy.section_height_mm, hi+policy.section_height_mm)
            if not len(window):
                continue
            xy = points[window, :2]-center
            radius = np.linalg.norm(xy, axis=1)
            sectors = np.floor((np.arctan2(xy[:, 1], xy[:, 0])+math.pi)
                               *sector_count/math.tau).astype(int) % sector_count
            side_ids = []
            side_sectors = []
            for feature, endpoint in ((lower, lo), (upper, hi)):
                mask = ((abs(radius-feature["value_mm"]/2) <= policy.plane_residual_mm)
                        & (abs(points[window, 2]-endpoint) <= policy.section_height_mm))
                side_ids.append(window[mask])
                side_sectors.append(sectors[mask])
        brackets, witnesses, active = [], [], []
        for sector in range(sector_count):
            a, b = [ids[values == sector] for ids, values in zip(side_ids, side_sectors)]
            if len(a) < 3 or len(b) < 3:
                continue
            ai = a[np.argmax(points[a, 2])]
            bi = b[np.argmin(points[b, 2])]
            za, zb = points[[ai, bi], 2]
            if 0. < zb-za <= 2*policy.section_height_mm:
                brackets.append((float(za), float(zb)))
                witnesses.extend((int(ai), int(bi)))
                active.append(sector)
        if len(active) < math.ceil(sector_count*policy.minimum_angular_coverage_rad/math.tau):
            continue
        # Sector count alone could be concentrated on an arc. Bound its largest
        # unobserved angular run as well, including the wrap at +/-pi.
        angular_gap = max(np.diff(np.r_[active, active[0]+sector_count]))*math.tau/sector_count
        if math.tau-angular_gap < policy.minimum_angular_coverage_rad:
            continue
        interval = [max(a for a, b in brackets), min(b for a, b in brackets)]
        if interval[0] >= interval[1]:
            continue
        estimate = sum(interval)/2
        record = {"method": "common_interval_of_observed_angular_side_transition_brackets",
                  "estimate_mm": estimate, "bracket_mm": interval,
                  "sampling_half_width_mm": (interval[1]-interval[0])/2,
                  "angular_sector_count": len(active), "angular_sectors_total": sector_count,
                  "support_point_indices": witnesses,
                  "reference_regions": [lower["region_id"], upper["region_id"]],
                  "boundary": "sampling bracket, not a calibrated physical confidence interval"}
        for feature, side in ((lower, 1), (upper, 0)):
            key = (feature["region_id"], side)
            # Ambiguous competing transitions never authorize an endpoint.
            resolved[key] = record if key not in resolved else None
    for region, height in heights.items():
        bounds = list(height["observed_height_range_mm"])
        evidence = []
        for side in (0, 1):
            record = resolved.get((region, side))
            if record is not None:
                bounds[side] = record["estimate_mm"]
                evidence.append(dict(endpoint="bottom" if side == 0 else "top", **record))
        if evidence and height["status"] == "available":
            height["value_mm"] = float(bounds[1]-bounds[0])
            height["estimated_height_range_mm"] = bounds
            height["endpoint_method"] = "observed_extrema_with_bracketed_side_transitions"
            height["boundary_interpolation"] = evidence
            height["interpolated_sampling_uncertainty_mm"] = sum(e["sampling_half_width_mm"] for e in evidence)
    return features


def _supported_plane_patch(points, patch, normal, offset, tangent, policy):
    """Apply the unchanged distribution/residual/occupancy/curvature policy."""
    tangential_support = np.unique(np.round((points[patch, :2] @ tangent)/.02)*.02)
    span = np.ptp(tangential_support)
    zspan = np.ptp(points[patch, 2])
    z_occupancy = len(np.unique(np.floor((points[patch, 2]-points[patch, 2].min())/policy.section_height_mm)))
    if (len(tangential_support) < 8 or span < max(2., 8*policy.residual_mm)
            or zspan < policy.minimum_segment_height_mm
            or z_occupancy < .4*max(1., zspan/policy.section_height_mm)):
        return False
    residual = float(np.sqrt(np.mean((points[patch, :2] @ normal-offset)**2)))
    if residual > policy.plane_residual_mm:
        return False
    transverse = points[patch, :2] @ tangent
    transverse -= np.mean(transverse)
    deviations = points[patch, :2] @ normal-offset
    quadratic = np.column_stack((transverse**2, transverse, np.ones(len(patch))))
    curvature, *_ = np.linalg.lstsq(quadratic, deviations, rcond=None)
    predicted = quadratic @ curvature
    unexplained = float(np.sqrt(np.mean((deviations-predicted)**2)))
    if np.ptp(predicted) > .04 and unexplained < .5*residual:
        return False
    return True


def _sequential_plane_best(sample, pairs, policy):
    """Historical 128-hypothesis loop, used when a batch score is ambiguous."""
    best = None
    for pair in pairs:
        tangent = sample[pair[1]]-sample[pair[0]]
        length = float(np.linalg.norm(tangent))
        if length < 2.:
            continue
        normal = np.array([-tangent[1], tangent[0]])/length
        offset = float(sample[pair[0]] @ normal)
        residual = abs(sample @ normal-offset)
        score = int((residual <= policy.plane_residual_mm).sum())
        if best is None or score > best[0]:
            best = (score, normal, offset)
    return best


def _batched_plane_best(sample, rng, policy, draws=128):
    """Score the same 128 random pairs as one matrix product.

    The random draw sequence is unchanged (one ``rng.choice`` per hypothesis).
    If any point of any hypothesis lies within 1e-9 mm of the inlier threshold,
    the exact historical per-hypothesis loop is used instead, so a floating-point
    rounding difference in the batched product cannot change an accepted plane.
    """
    pairs = np.empty((draws, 2), dtype=np.int64)
    for index in range(draws):
        pairs[index] = rng.choice(len(sample), 2, replace=False)
    deltas = sample[pairs[:, 1]] - sample[pairs[:, 0]]
    lengths = np.linalg.norm(deltas, axis=1)
    valid = lengths >= 2.
    if not valid.any():
        return None
    normals = np.zeros((draws, 2))
    normals[valid, 0] = -deltas[valid, 1]/lengths[valid]
    normals[valid, 1] = deltas[valid, 0]/lengths[valid]
    offsets = np.einsum('ij,ij->i', sample[pairs[:, 0]], normals)
    valid_normals = normals[valid]
    valid_offsets = offsets[valid]
    tolerance = policy.plane_residual_mm
    tol_hi = tolerance + 1e-9
    tol_lo = tolerance - 1e-9
    residual = valid_normals @ sample.T
    residual -= valid_offsets[:, None]
    np.abs(residual, out=residual)
    c_hi = (residual <= tol_hi).sum(axis=1)
    c_lo = (residual < tol_lo).sum(axis=1)
    if int(c_hi.sum()) != int(c_lo.sum()):
        return _sequential_plane_best(sample, pairs, policy)
    best_index = int(np.argmax(c_hi))
    return int(c_hi[best_index]), valid_normals[best_index], float(valid_offsets[best_index])


def _vertical_planes(points: np.ndarray, policy: FeaturePolicy) -> list[dict[str, Any]]:
    """Discover distributed vertical surface planes with deterministic RANSAC.

    Normal direction is learned from registered XY points, rather than from a
    single laser-section projection. Height remains the calibrated axis Z.
    The 128 hypotheses per iteration are scored with one bounded matrix product;
    support removal uses an alive mask instead of a repeatedly sorted ``np.isin``.
    """
    alive = np.ones(len(points), dtype=bool)
    remaining = np.arange(len(points))
    rng = np.random.default_rng(173)
    planes = []
    minimum_support = max(40, int(.01*len(points)))
    for _ in range(24):
        n_rem = len(remaining)
        if n_rem < minimum_support:
            break
        rem_pts = points[remaining]
        rem_xy = rem_pts[:, :2]
        sample_ids = remaining[np.linspace(0, n_rem-1, min(5000, n_rem)).astype(int)]
        sample = points[sample_ids, :2]
        best = _batched_plane_best(sample, rng, policy)
        if best is None or best[0] < minimum_support*len(sample)/n_rem:
            break
        _, normal, offset = best
        ids = remaining[abs(rem_xy @ normal-offset) <= policy.plane_residual_mm]
        for _ in range(3):
            xy = points[ids, :2]
            center = xy.mean(axis=0)
            _, vectors = np.linalg.eigh((xy-center).T @ (xy-center))
            normal = vectors[:, 0]
            offset = float(center @ normal)
            ids = remaining[abs(rem_xy @ normal-offset) <= policy.plane_residual_mm]
            if len(ids) < 3:
                break
        alive[ids] = False
        remaining = np.flatnonzero(alive)
        if len(ids) < minimum_support:
            continue
        if normal[np.argmax(abs(normal))] < 0:
            normal, offset = -normal, -offset
        tangent = np.array([-normal[1], normal[0]])
        # Coplanarity alone does not imply one connected surface. Isolated
        # side crossings/outliers must not enlarge a groove floor's Z domain.
        # Apply the original distribution, occupancy, residual and curvature
        # gates to each observed connected tangent/Z patch independently.
        projected = np.column_stack((points[ids, :2] @ tangent, points[ids, 2]))
        # Retain a hypothesis that already satisfies the original aggregate
        # gates. Some sparsely sampled valid faces comprise separate narrow
        # observed traces; requiring every trace to be a full face would lose
        # their independently distributed aggregate plane support.
        if _supported_plane_patch(points, ids, normal, offset, tangent, policy):
            patches = [ids]
        else:
            patches = []
            for component in _components(projected, policy.component_cell_mm):
                patch = ids[component]
                if (len(patch) >= 40
                        and _supported_plane_patch(points, patch, normal, offset, tangent, policy)):
                    patches.append(patch)
        if not patches or sum(map(len, patches)) < minimum_support:
            continue
        supported = np.concatenate(patches)
        sup_xy = points[supported, :2]
        residual = float(np.sqrt(np.mean((sup_xy @ normal-offset)**2)))
        sup_tan = sup_xy @ tangent
        planes.append({"normal": normal, "offset": offset, "ids": supported, "residual": residual,
                       "patches": patches,
                       "tangent_range": [float(sup_tan.min()),
                                         float(sup_tan.max())]})
    return planes


def _plane_features(points, planes, calibration_id, reference, policy):
    output = []
    families: list[list[dict[str, Any]]] = []
    for index, plane in enumerate(planes):
        plane["region_id"] = f"plane_{index}"
        output.append(_record("plane_offset", plane["offset"], ids=plane["ids"], points=points,
                              calibration_id=calibration_id, reference=reference,
                              direction=[*plane["normal"], 0.], region_id=plane["region_id"],
                              residual=plane["residual"], plane_normal=[*plane["normal"], 0.]))
        family = next((group for group in families if group[0]["normal"] @ plane["normal"] > .995), None)
        if family is None:
            families.append([plane])
        else:
            family.append(plane)
    # Cache support projections once per hypothesis.  The candidate gates and the
    # accepted pair set are unchanged: tangent overlap >= 2 mm and normal distance
    # > 2 * residual_mm remain the only conditions, exactly as before.
    support_xy = {plane["region_id"]: points[plane["ids"], :2] for plane in planes}
    for group in families:
        if len(group) < 2:
            continue
        normal = group[0]["normal"]
        # Project median observed supports onto the same learned unit normal;
        # this avoids mixing the offsets of slightly different PCA normals.
        normal_coordinate = {plane["region_id"]: support_xy[plane["region_id"]] @ normal
                             for plane in group}
        ordered = sorted(group, key=lambda p: np.median(normal_coordinate[p["region_id"]]))
        tangent = np.array([-normal[1], normal[0]])
        tangent_coordinate = {plane["region_id"]: support_xy[plane["region_id"]] @ tangent
                              for plane in group}
        for left, right in combinations(ordered, 2):
            left_id, right_id = left["region_id"], right["region_id"]
            a, b = tangent_coordinate[left_id], tangent_coordinate[right_id]
            overlap = min(a.max(), b.max())-max(a.min(), b.min())
            if overlap < 2.:
                continue
            # V5: two surfaces must share measured height support. Equal XY
            # projection at disjoint heights cannot establish a visible gap.
            z_gate = getattr(policy, "minimum_plane_z_overlap_mm", 0.0)
            if z_gate > 0:
                za = points[left["ids"], 2]
                zb = points[right["ids"], 2]
                if min(za.max(), zb.max()) - max(za.min(), zb.min()) < z_gate:
                    continue
            delta = float(np.median(normal_coordinate[right_id])
                          - np.median(normal_coordinate[left_id]))
            if delta <= 2*policy.residual_mm:
                continue
            ids = np.r_[left["ids"], right["ids"]]
            output.append(_record("parallel_plane_spacing", delta, ids=ids, points=points,
                                  calibration_id=calibration_id, reference=reference,
                                  direction=[*normal, 0.], region_id=left_id+"_to_"+right_id,
                                  residual=max(left["residual"], right["residual"]),
                                  reference_regions=[left_id, right_id],
                                  common_tangent_support_range_mm=[float(max(a.min(), b.min())),
                                                                   float(min(a.max(), b.max()))],
                                  measurement_kind="normal_distance_between_observed_parallel_planes"))
    return output


def _section_features(points, calibration_id, reference, validity, policy):
    # The calibrated fixed laser section is parameterized by axis Z and radial
    # Y. This supports arbitrary numbers/positions of stair and groove levels.
    s, depth = points[:, 2], points[:, 1]
    bins = np.floor((s-s.min())/policy.section_sample_mm).astype(int)
    samples = []
    for index, ids in bin_groups(bins):
        median = float(np.median(depth[ids]))
        # A bin straddling an edge may contain two surfaces. Keep only its
        # observed median-surface support; do not average across the edge.
        ids = ids[abs(depth[ids]-median) <= policy.plateau_depth_tolerance_mm]
        if len(ids):
            samples.append((float(np.median(s[ids])), median, ids))
    plateaus = []
    active = []
    for sample in samples:
        if active and (sample[0]-active[-1][0] > 3*policy.section_sample_mm
                       or abs(sample[1]-np.median([a[1] for a in active])) > policy.plateau_depth_tolerance_mm):
            plateaus.append(active)
            active = []
        active.append(sample)
    if active:
        plateaus.append(active)
    regions = []
    for plateau in plateaus:
        ids = np.concatenate([sample[2] for sample in plateau])
        if len(plateau) < 4 or np.ptp(s[ids]) < 0.7:
            continue
        median = float(np.median(depth[ids]))
        residual = float(np.sqrt(np.mean((depth[ids]-median)**2)))
        if residual <= policy.residual_mm:
            regions.append({"ids": ids, "depth": median, "lo": float(s[ids].min()),
                            "hi": float(s[ids].max()), "residual": residual})
    output = []
    for i, (left, right) in enumerate(zip(regions, regions[1:])):
        delta = right["depth"]-left["depth"]
        if abs(delta) <= 2*policy.plateau_depth_tolerance_mm:
            continue
        ids = np.r_[left["ids"], right["ids"]]
        section = points[ids][:, [2, 1]]
        measured = section_step(section, left_range_mm=(left["lo"], left["hi"]),
                                right_range_mm=(right["lo"], right["hi"]), validity=validity)
        output.append(_record("section_depth_step", abs(measured.value_mm) if measured.value_mm is not None else None,
                              ids=ids, points=points, calibration_id=calibration_id, reference=reference,
                              direction=[0, 1, 0], region_id=f"step_{i}", residual=max(left["residual"], right["residual"]),
                              reason=measured.reason, signed_value_mm=measured.value_mm,
                              edge_coordinate_mm=(left["hi"]+right["lo"])/2,
                              section_axis=[0, 0, 1]))
    for i in range(1, len(regions)-1):
        left, groove, right = regions[i-1:i+2]
        # A farther-away central plateau bounded by two shallower observed
        # surfaces is a visible recess, independent of nominal groove count.
        if (groove["depth"]-max(left["depth"], right["depth"]) <= 2*policy.plateau_depth_tolerance_mm
                or abs(left["depth"]-right["depth"]) > policy.plateau_depth_tolerance_mm*2):
            continue
        edge_lo = (left["hi"]+groove["lo"])/2
        edge_hi = (groove["hi"]+right["lo"])/2
        ids = np.r_[left["ids"][-3:], groove["ids"], right["ids"][:3]]
        output.append(_record("groove_width", edge_hi-edge_lo, ids=ids, points=points,
                              calibration_id=calibration_id, reference=reference, direction=[0, 0, 1],
                              region_id=f"groove_{i}", residual=groove["residual"],
                              edges_mm=[edge_lo, edge_hi], boundary_resolution_mm=policy.section_sample_mm))
    return output


def _legacy_enabled(legacy: bool | None) -> bool:
    if legacy is None:
        value = os.environ.get("R26_FEATURE_LEGACY", "")
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(legacy)


def _discover_features_raw(points_axis_mm: object, *, calibration_id: str,
                           reference_basis: str | None = None,
                           scan_angles_rad: Sequence[float] | None = None,
                           policy: FeaturePolicy = FeaturePolicy(),
                           workers: int | None = None,
                           legacy: bool = False) -> list[dict[str, Any]]:
    """Historical candidate generation, before any same-entity association."""
    points = np.asarray(points_axis_mm, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
        raise ValueError("finite Nx3 observed points required")
    reference = reference_basis or f"SIMULATION_ONLY axis XYZ mm; calibration={calibration_id}"
    if "SIMULATION_ONLY" not in reference:
        raise ValueError("features require explicit synthetic reference")
    if len(points) < policy.minimum_points:
        return [_record("geometry", None, ids=np.arange(len(points)), points=points,
                        calibration_id=calibration_id, reference=reference, direction=[0, 0, 1],
                        region_id="all", reason="insufficient_observed_points")]
    # These booleans authorize numerical helper calculations only within the
    # synthetic reference. No physical calibration/timing flag is generated.
    validity = MeasurementValidity(True, True, True, reference)
    angles = np.asarray(scan_angles_rad if scan_angles_rad is not None else [], float)
    fixed = len(angles) > 0 and np.ptp(angles) < 1e-8
    if not fixed and len(angles) == 0:
        fixed = np.linalg.matrix_rank(points-points.mean(axis=0), tol=1e-6) < 3
    if fixed:
        output = _section_features(points, calibration_id, reference, validity, policy)
    else:
        if legacy:
            feature_parallel_r26.mark_legacy(len(points))
            output = _solid_features_legacy(points, calibration_id, reference, validity, policy)
        else:
            output = _solid_features(points, calibration_id, reference, validity, policy,
                                     workers=workers)
        cylindrical = np.zeros(len(points), bool)
        for feature in output:
            if feature["type"] == "diameter":
                cylindrical[feature["support"]["point_indices"]] = True
        plane_ids = np.flatnonzero(~cylindrical)
        planes = _vertical_planes(points[plane_ids], policy) if len(plane_ids) >= policy.minimum_points else []
        for plane in planes:
            plane["ids"] = plane_ids[plane["ids"]]
        invalid_regions = set()
        for feature in output:
            if feature["type"] not in ("prism_width", "prism_depth"):
                continue
            normal = np.asarray(feature["direction"][:2])
            center = np.asarray(feature["center_xy_mm"])
            for sign in (-1., 1.):
                offset = float(center @ normal+sign*feature["value_mm"]/2)
                if not any(abs(normal @ plane["normal"]) > .995
                           and abs(offset-np.sign(normal @ plane["normal"])*plane["offset"]) <= 2*policy.residual_mm
                           for plane in planes):
                    invalid_regions.add(feature["region_id"])
        output = [feature for feature in output if feature["region_id"] not in invalid_regions]
        output.extend(_plane_features(points, planes, calibration_id, reference, policy))
    if not output:
        output.append(_record("geometry", None, ids=np.arange(len(points)), points=points,
                              calibration_id=calibration_id, reference=reference, direction=[0, 0, 1],
                              region_id="all", reason="insufficient_observed_geometric_support"))
    return output


def discover_features(points_axis_mm: object, *, calibration_id: str,
                      reference_basis: str | None = None, scan_angles_rad: Sequence[float] | None = None,
                      policy: FeaturePolicy = FeaturePolicy(),
                      association_policy: AssociationPolicy | None = None,
                      workers: int | None = None,
                      legacy: bool | None = None) -> list[dict[str, Any]]:
    """Return JSON-compatible observed geometry, with same-entity duplicates merged.

    ``association_policy=None`` uses :class:`AssociationPolicy` defaults.  Pass
    ``AssociationPolicy(enabled=False)`` to obtain the historical raw candidates.
    Use :func:`discover_features_with_audit` when the caller must persist both the
    raw candidates and the merge audit (all original records are preserved there).

    ``workers`` selects the independent Z-slice executor: ``None`` (default) reads
    ``R26_FEATURE_WORKERS`` or auto-selects a bounded POSIX ``fork`` pool on large
    clouds, ``1``/``0`` forces the frozen serial result path and ``>=2`` requests
    the pool (with an exact serial fallback).  ``legacy=True`` runs the verbatim
    pre-optimization implementation for A-B verification; it is never the default.
    """
    points = np.asarray(points_axis_mm, dtype=float)
    output = _discover_features_raw(points, calibration_id=calibration_id,
                                    reference_basis=reference_basis,
                                    scan_angles_rad=scan_angles_rad, policy=policy,
                                    workers=workers, legacy=_legacy_enabled(legacy))
    association_policy = association_policy or AssociationPolicy()
    if association_policy.enabled and len(output) > 1:
        output = associate_features(output, points, policy=association_policy)
    return output


def discover_features_with_audit(points_axis_mm: object, *, calibration_id: str,
                                 reference_basis: str | None = None,
                                 scan_angles_rad: Sequence[float] | None = None,
                                 policy: FeaturePolicy = FeaturePolicy(),
                                 association_policy: AssociationPolicy | None = None,
                                 workers: int | None = None,
                                 legacy: bool | None = None
                                 ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return ``(merged_features, association_audit)`` from one extraction pass.

    ``association_audit['original_features']`` is the untouched raw candidate list
    (the same order and point supports produced before association).  The audit
    also contains the per-group support/position/axis/range metrics and the
    canonical plane-region map.  Truth/CAD data is never accepted by this API.

    ``workers`` and ``legacy`` have the same meaning as in
    :func:`discover_features`; they change scheduling/verification only, never the
    returned feature records.  The audit schema is unchanged.
    """
    points = np.asarray(points_axis_mm, dtype=float)
    raw = _discover_features_raw(points, calibration_id=calibration_id,
                                 reference_basis=reference_basis,
                                 scan_angles_rad=scan_angles_rad, policy=policy,
                                 workers=workers, legacy=_legacy_enabled(legacy))
    association_policy = association_policy or AssociationPolicy()
    if len(raw) > 1:
        merged, audit = associate_features(raw, points, policy=association_policy,
                                           return_audit=True)
    else:
        merged, audit = raw, {"schema": "R26_FEATURE_ASSOCIATION_V1_AUDIT",
                              "policy": asdict(association_policy),
                              "original_count": len(raw), "merged_count": len(raw),
                              "dropped_count": 0, "groups": [],
                              "original_features": raw,
                              "note": "fewer than two candidates; association skipped"}
    audit["raw_feature_count"] = len(raw)
    audit["merged_feature_count"] = len(merged)
    return merged, audit


def discover_features_bounded(points_axis_mm: object, *, deadline_s: float = 120.0,
                              cancelled=None, result_directory=None,
                              minimum_available_bytes: int = 128 * 1024**2,
                              **kwargs):
    """Return the same (features, audit), or fail without publishing partial data.

    This is the production end-analysis entry; direct legacy APIs are retained
    for reference tests. Deadline includes startup, candidate generation,
    association and transferring the result. No CAD/truth input is accepted.
    """
    return feature_parallel_r26.run_bounded(
        discover_features_with_audit, (points_axis_mm,), kwargs,
        deadline_s=deadline_s, cancelled=cancelled,
        minimum_available_bytes=minimum_available_bytes,
        result_directory=result_directory)


def _discover_from_saved_cloud(cloud_path, **options):
    """Reopen persisted XYZ in the child rather than transferring its contents."""
    cloud = np.load(cloud_path, mmap_mode='r', allow_pickle=False)
    if cloud.ndim != 2 or cloud.shape[1] != 3 or cloud.dtype.kind != 'f':
        raise ValueError('feature cloud must be floating Nx3')
    return discover_features_with_audit(cloud, **options)


def discover_features_file_bounded(cloud_path, *, deadline_s=120.0,
                                    cancelled=None, result_directory=None,
                                    minimum_available_bytes=128 * 1024**2, **kwargs):
    """Analyze a completed, verified cloud.npy through a read-only mapping.

    The caller must finish persistence before invocation. This entry returns the
    same feature/audit records as the array entry, under the same safety limits.
    """
    return feature_parallel_r26.run_bounded(
        _discover_from_saved_cloud, (str(Path(cloud_path).resolve()),), kwargs,
        deadline_s=deadline_s, cancelled=cancelled,
        minimum_available_bytes=minimum_available_bytes,
        result_directory=result_directory)
