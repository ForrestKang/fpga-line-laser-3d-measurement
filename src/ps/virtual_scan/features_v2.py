"""Version 2 geometry discovery derived from the unchanged frozen v1 source.

Derivation: virtual_scan/features.py, SHA-256
AC28594D91B380FA9C93F47D2A74DF5347C6E64E4D2D453565CBE1BF65645949.
V2 emits every supported parallel-plane pair and intersects independently
observed rectangle face-interior height supports to establish segment ownership.
Frozen v1, its estimator, calibration, evidence and holdout definitions are not
modified. Existing holdout01 data may be used only as development regressions.

No mesh, fixture identifier, number of levels, nominal dimensions, or evaluator
truth is accepted. Global numerical support/residual policies are explicit.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
import math
from pathlib import Path
import sys
from typing import Any, Sequence

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
for _source in ("structured_light_system_algorithm_v1", "structured_light_system_geometry_v1"):
    _path = str(_ROOT / _source / "src" / "python")
    if _path not in sys.path:
        sys.path.insert(0, _path)

from structured_light.scan3d.continuous_measurement import (
    MeasurementValidity, fit_circle_diameter, segment_height, section_step)


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
    """Connected occupied XY cells; never interpolate empty cells."""
    grid = np.floor(points / cell).astype(np.int64)
    cells: dict[tuple[int, int], list[int]] = {}
    for index, xy in enumerate(grid):
        cells.setdefault((int(xy[0]), int(xy[1])), []).append(index)
    groups = []
    remaining = set(cells)
    while remaining:
        pending = [min(remaining)]
        remaining.remove(pending[0])
        indices = []
        while pending:
            x, y = pending.pop()
            indices.extend(cells[(x, y)])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    key = (x + dx, y + dy)
                    if key in remaining:
                        remaining.remove(key)
                        pending.append(key)
        groups.append(np.asarray(indices, int))
    return groups


def _hull(points: np.ndarray) -> np.ndarray:
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


def _rectangle(points: np.ndarray, policy: FeaturePolicy) -> dict[str, Any] | None:
    hull = _hull(points)
    if len(hull) < 4:
        return None
    edges = np.roll(hull, -1, axis=0) - hull
    angles = np.unique(np.round(np.arctan2(edges[:, 1], edges[:, 0]) % (math.pi/2), 6))
    best = None
    for angle in angles:
        c, s = math.cos(angle), math.sin(angle)
        basis = np.asarray([[c, -s], [s, c]])
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
        # All four sides need distributed, measured support. An L-shaped arc
        # does not authorize an unseen opposite side or an inferred dimension.
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


def _primitive(points: np.ndarray, validity: MeasurementValidity, policy: FeaturePolicy) -> dict[str, Any] | None:
    if len(points) < policy.minimum_points:
        return None
    # Deterministic thinning bounds the numeric fit cost, preserves angular order.
    if len(points) > 700:
        points = points[np.linspace(0, len(points)-1, 700).astype(int)]
    rectangle = _rectangle(points, policy)
    circle = fit_circle_diameter(points, validity=validity, min_points=policy.minimum_points,
                                 max_residual_mm=policy.residual_mm,
                                 min_angular_coverage_rad=policy.minimum_angular_coverage_rad)
    fitted_circle = None
    if circle.status == "available" and circle.point_count >= .75 * len(points):
        # Fit center on points close to the formal robust circle hypothesis.
        model = _circle(points)
        if model is not None:
            center, radius = model
            residual = abs(np.linalg.norm(points-center, axis=1)-radius)
            if np.quantile(residual, .9) <= policy.residual_mm:
                fitted_circle = {"shape": "cylinder", "center": center,
                                 "sizes": np.asarray([circle.value_mm]),
                                 "residual": float(circle.residual_mm)}
    if rectangle is not None and (fitted_circle is None or rectangle["residual"] < fitted_circle["residual"]):
        return rectangle
    return fitted_circle


def _record(kind: str, value: float | None, *, ids: np.ndarray, points: np.ndarray,
            calibration_id: str, reference: str, direction: Sequence[float],
            region_id: str, residual: float | None = None, reason: str | None = None,
            **extra: Any) -> dict[str, Any]:
    observed = points[ids] if len(ids) else np.empty((0, 3))
    return {"type": kind, "feature_type": kind, "region_id": region_id,
            "value_mm": None if value is None else float(value), "unit": "mm", "units": "mm",
            "status": "available" if value is not None else "unavailable", "reason": reason,
            "support": {"point_count": len(ids), "point_indices": ids.tolist(),
                        "minimum_mm": observed.min(axis=0).tolist() if len(ids) else None,
                        "maximum_mm": observed.max(axis=0).tolist() if len(ids) else None},
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


def _solid_features(points, calibration_id, reference, validity, policy, planes=()):
    zmin, zmax = float(points[:, 2].min()), float(points[:, 2].max())
    bins = np.floor((points[:, 2]-zmin)/policy.section_height_mm).astype(int)
    tracks: list[dict[str, Any]] = []
    partial_tracks: list[dict[str, Any]] = []
    for level in np.unique(bins):
        ids = np.flatnonzero(bins == level)
        for cluster in _components(points[ids, :2], policy.component_cell_mm):
            support = ids[cluster]
            model = _primitive(points[support, :2], validity, policy)
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
        common = dict(ids=ids, points=points, calibration_id=calibration_id, reference=reference,
                      region_id=region_id, residual=residual, shape=track["shape"],
                      center_xy_mm=center.tolist(), observed_height_range_mm=[lo, hi])
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
    return output


def _vertical_planes(points: np.ndarray, policy: FeaturePolicy) -> list[dict[str, Any]]:
    """Discover distributed vertical surface planes with deterministic RANSAC.

    Normal direction is learned from registered XY points, rather than from a
    single laser-section projection. Height remains the calibrated axis Z.
    """
    remaining = np.arange(len(points))
    rng = np.random.default_rng(173)
    planes = []
    minimum_support = max(40, int(.01*len(points)))
    for _ in range(24):
        if len(remaining) < minimum_support:
            break
        sample_ids = remaining[np.linspace(0, len(remaining)-1, min(5000, len(remaining))).astype(int)]
        sample = points[sample_ids, :2]
        best = None
        for _ in range(128):
            pair = sample[rng.choice(len(sample), 2, replace=False)]
            tangent = pair[1]-pair[0]
            if np.linalg.norm(tangent) < 2.:
                continue
            normal = np.array([-tangent[1], tangent[0]])/np.linalg.norm(tangent)
            offset = float(pair[0] @ normal)
            residual = abs(sample @ normal-offset)
            inliers = residual <= policy.plane_residual_mm
            score = int(inliers.sum())
            if best is None or score > best[0]:
                best = (score, normal, offset)
        if best is None or best[0] < minimum_support*len(sample)/len(remaining):
            break
        _, normal, offset = best
        ids = remaining[abs(points[remaining, :2] @ normal-offset) <= policy.plane_residual_mm]
        for _ in range(3):
            xy = points[ids, :2]
            center = xy.mean(axis=0)
            _, vectors = np.linalg.eigh((xy-center).T @ (xy-center))
            normal = vectors[:, 0]
            offset = float(center @ normal)
            ids = remaining[abs(points[remaining, :2] @ normal-offset) <= policy.plane_residual_mm]
            if len(ids) < 3:
                break
        remaining = remaining[~np.isin(remaining, ids)]
        if len(ids) < minimum_support:
            continue
        if normal[np.argmax(abs(normal))] < 0:
            normal, offset = -normal, -offset
        tangent = np.array([-normal[1], normal[0]])
        tangential_support = np.unique(np.round((points[ids, :2] @ tangent)/.02)*.02)
        span = np.ptp(tangential_support)
        zspan = np.ptp(points[ids, 2])
        z_occupancy = len(np.unique(np.floor((points[ids, 2]-points[ids, 2].min())/policy.section_height_mm)))
        if (len(tangential_support) < 8 or span < max(2., 8*policy.residual_mm)
                or zspan < policy.minimum_segment_height_mm
                or z_occupancy < .4*max(1., zspan/policy.section_height_mm)):
            continue
        residual = float(np.sqrt(np.mean((points[ids, :2] @ normal-offset)**2)))
        if residual > policy.plane_residual_mm:
            continue
        transverse = points[ids, :2] @ tangent
        transverse -= np.mean(transverse)
        deviations = points[ids, :2] @ normal-offset
        quadratic = np.column_stack((transverse**2, transverse, np.ones(len(ids))))
        curvature, *_ = np.linalg.lstsq(quadratic, deviations, rcond=None)
        predicted = quadratic @ curvature
        unexplained = float(np.sqrt(np.mean((deviations-predicted)**2)))
        if np.ptp(predicted) > .04 and unexplained < .5*residual:
            # A short circular arc can meet a distance-to-plane tolerance.
            # Consistent resolved curvature forbids calling it a flat face.
            continue
        planes.append({"normal": normal, "offset": offset, "ids": ids, "residual": residual,
                       "tangent_range": [float((points[ids, :2] @ tangent).min()),
                                         float((points[ids, :2] @ tangent).max())]})
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
    for group in families:
        if len(group) < 2:
            continue
        normal = group[0]["normal"]
        # Project median observed supports onto the same learned unit normal;
        # this avoids mixing the offsets of slightly different PCA normals.
        ordered = sorted(group, key=lambda p: np.median(points[p["ids"], :2] @ normal))
        for left, right in combinations(ordered, 2):
            tangent = np.array([-normal[1], normal[0]])
            a, b = points[left["ids"], :2] @ tangent, points[right["ids"], :2] @ tangent
            overlap = min(a.max(), b.max())-max(a.min(), b.min())
            if overlap < 2.:
                continue
            delta = float(np.median(points[right["ids"], :2] @ normal)
                          - np.median(points[left["ids"], :2] @ normal))
            if delta <= 2*policy.residual_mm:
                continue
            ids = np.r_[left["ids"], right["ids"]]
            output.append(_record("parallel_plane_spacing", delta, ids=ids, points=points,
                                  calibration_id=calibration_id, reference=reference,
                                  direction=[*normal, 0.], region_id=left["region_id"]+"_to_"+right["region_id"],
                                  residual=max(left["residual"], right["residual"]),
                                  reference_regions=[left["region_id"], right["region_id"]],
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
    for index in np.unique(bins):
        ids = np.flatnonzero(bins == index)
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


def discover_features(points_axis_mm: object, *, calibration_id: str,
                      reference_basis: str | None = None, scan_angles_rad: Sequence[float] | None = None,
                      policy: FeaturePolicy = FeaturePolicy()) -> list[dict[str, Any]]:
    """Return JSON-compatible observed geometry, or explicit unavailable reason."""
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
        output = _solid_features(points, calibration_id, reference, validity, policy)
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
