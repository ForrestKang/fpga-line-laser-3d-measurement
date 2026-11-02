"""Observed local geometry candidate for complex rotating scan point clouds.

Derived from immutable virtual_scan/features_v4.py. Inputs are finite observed
axis XYZ, synthetic calibration identity, scan angles and optional per-point
camera origins; no CAD, mesh, fixture identifier, nominal dimensions, part
coordinates or evaluator truth is loaded. All discovery is offline PS/Python,
not a PL implementation or a claim of physical metrology. Output preserves
observed support indices, fit residuals, sampling limitations and unavailable
reasons. Local opposing walls require common tangent/height support; naming a
gap or solid width additionally requires view-facing normals. Curved sphere
patches are compared with constant-section cylinders before reporting sizes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import combinations
import math
from pathlib import Path
import sys
from typing import Any, Sequence

import numpy as np

_RUNTIME = Path(__file__).resolve().parents[1]
# The immutable snapshot is preferred when present; the candidate runtime keeps
# the same package paths so this reference module stays importable in both trees.
for _root in (_RUNTIME / "frozen", _RUNTIME):
    for _source in ("structured_light_system_algorithm_v1", "structured_light_system_geometry_v1"):
        _path = str(_root / _source / "src" / "python")
        if _path not in sys.path:
            sys.path.insert(0, _path)
if str(_RUNTIME) not in sys.path:
    sys.path.insert(0, str(_RUNTIME))

from structured_light.scan3d.continuous_measurement import (
    MeasurementValidity, fit_circle_diameter, segment_height, section_step)
from candidate.feature_fast import bin_groups, connected_cells
from candidate.feature_association import AssociationPolicy, associate_features


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
    """Connected occupied XY cells; never interpolate empty cells.

    Delegates to the exact integer-key implementation shared with
    virtual_scan.features_v4 (same start-cell order, DFS order and point order).
    """
    return connected_cells(points, cell)


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
            # Endpoint crossings on two other faces are not an observed fourth
            # face. Require distributed interior support along the full side.
            side_cells = np.unique(np.floor((subset[:,transverse]-lo[transverse])
                                           / (extent[transverse]/20)).astype(int))
            interior = ((subset[:,transverse] > lo[transverse]+.25*extent[transverse])
                        & (subset[:,transverse] < hi[transverse]-.25*extent[transverse]))
            if len(side_cells) < 10 or int(interior.sum()) < 3:
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
            support_bounds: tuple[list[float], list[float]] | None = None,
            **extra: Any) -> dict[str, Any]:
    observed = points[ids] if len(ids) else np.empty((0, 3))
    if support_bounds is None and len(ids):
        minimum = observed.min(axis=0).tolist()
        maximum = observed.max(axis=0).tolist()
    elif support_bounds is not None:
        minimum, maximum = support_bounds
    else:
        minimum = maximum = None
    return {"type": kind, "feature_type": kind, "region_id": region_id,
            "value_mm": None if value is None else float(value), "unit": "mm", "units": "mm",
            "status": "available" if value is not None else "unavailable", "reason": reason,
            "support": {"point_count": len(ids), "point_indices": ids.tolist(),
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


def _solid_features(points, calibration_id, reference, validity, policy, planes=()):
    zmin, zmax = float(points[:, 2].min()), float(points[:, 2].max())
    bins = np.floor((points[:, 2]-zmin)/policy.section_height_mm).astype(int)
    tracks: list[dict[str, Any]] = []
    partial_tracks: list[dict[str, Any]] = []
    for level, ids in bin_groups(bins):
        # Same levels and ascending point indices as np.unique/flatnonzero, with
        # one stable sort instead of one full scan per level.
        whole = _primitive(points[ids, :2], validity, policy)
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
            # A single-component slice was already fitted above; reuse that
            # exact primitive instead of repeating the 128-hypothesis fit.
            if len(cluster) == len(ids) and np.array_equal(cluster, whole_indices):
                model = whole
            else:
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


def _refine_cylinder_boundaries(points, features, policy):
    """Interpolate only transitions bracketed by two observed circular sides.

    Accepted section slices identify the sides but their endpoints are inward
    sampled limits. Require different radii, the same learned center, nearby
    endpoints, and independently measured lower/upper support over >=270 deg.
    Each angular sector supplies a closed observation bracket. Their common
    intersection bounds one transition; no common interval means no refinement.
    No extrapolation is made at free outer endpoints or across missing data.
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
        xy = points[:, :2]-center
        radius = np.linalg.norm(xy, axis=1)
        sector_count = 48
        sectors = np.floor((np.arctan2(xy[:, 1], xy[:, 0])+math.pi)
                           *sector_count/math.tau).astype(int) % sector_count
        side_ids = []
        for feature, endpoint in ((lower, lo), (upper, hi)):
            side_ids.append(np.flatnonzero(
                (abs(radius-feature["value_mm"]/2) <= policy.plane_residual_mm)
                & (abs(points[:, 2]-endpoint) <= policy.section_height_mm)))
        brackets, witnesses, active = [], [], []
        for sector in range(sector_count):
            a, b = [ids[sectors[ids] == sector] for ids in side_ids]
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
        residual = float(np.sqrt(np.mean((points[supported, :2] @ normal-offset)**2)))
        planes.append({"normal": normal, "offset": offset, "ids": supported, "residual": residual,
                       "patches": patches,
                       "tangent_range": [float((points[supported, :2] @ tangent).min()),
                                         float((points[supported, :2] @ tangent).max())]})
    return planes


def _legacy_plane_features(points, planes, calibration_id, reference, policy):
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


def _legacy_discover_features(points_axis_mm: object, *, calibration_id: str,
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

# Candidate additions below consume only observed coordinates and optional view
# origins. Distances are millimetres in an independently calibrated axis frame.

def _sphere_lstsq(points):
    origin = points.mean(axis=0)
    centered = points-origin
    matrix = np.column_stack((2*centered, np.ones(len(points))))
    if np.linalg.matrix_rank(matrix) < 4 or np.linalg.cond(matrix) > 1e5:
        return None
    fit, *_ = np.linalg.lstsq(matrix, np.sum(centered*centered, axis=1), rcond=None)
    squared = fit[3]+fit[:3]@fit[:3]
    return (origin+fit[:3], float(np.sqrt(squared))) if squared > 0 else None


def _sphere_features(points, calibration_id, reference, policy):
    # Occupied-cell sampling prevents a dense base from outvoting sparsely
    # observed curved surfaces; it never supplies a synthetic point to fits.
    _, sample_ids = np.unique(np.floor(points/.5).astype(np.int64), axis=0, return_index=True)
    if len(sample_ids) > 5000:
        sample_ids = sample_ids[np.linspace(0, len(sample_ids)-1, 5000).astype(int)]
    sample = points[sample_ids]
    rng = np.random.default_rng(913)
    threshold = policy.plane_residual_mm
    proposals = []
    height = np.ptp(sample[:, 2])
    pools = [np.arange(len(sample))]
    if height > 0:
        for fraction in np.linspace(0, .75, 7):
            lo = sample[:, 2].min()+fraction*height
            pools.append(np.flatnonzero((sample[:, 2] >= lo)&(sample[:, 2] <= lo+.3*height)))
    for pool in pools:
        if len(pool) < 30:
            continue
        best = None
        for _ in range(160):
            fit = _sphere_lstsq(sample[rng.choice(pool, 4, replace=False)])
            if fit is None:
                continue
            center, radius = fit
            if radius < 4*threshold or radius > 2*np.linalg.norm(np.ptp(sample[pool], axis=0)):
                continue
            error = abs(np.linalg.norm(sample-center, axis=1)-radius)
            ids = np.flatnonzero(error <= threshold)
            if len(ids) >= 50 and (best is None or len(ids) > len(best)):
                best = ids
        if best is not None:
            proposals.append(best)
    output, claimed = [], np.zeros(len(points), bool)
    for subset in sorted(proposals, key=len, reverse=True):
        fit = _sphere_lstsq(sample[subset])
        if fit is None:
            continue
        center, radius = fit
        ids = np.flatnonzero(abs(np.linalg.norm(points-center, axis=1)-radius) <= threshold)
        for _ in range(3):
            fit = _sphere_lstsq(points[ids]) if len(ids) >= 30 else None
            if fit is None:
                break
            center, radius = fit
            ids = np.flatnonzero(abs(np.linalg.norm(points-center, axis=1)-radius) <= threshold)
        if fit is None or len(ids) < max(100, policy.minimum_points*5) or claimed[ids].mean() > .5:
            continue
        # A tiny spherical arc fits many arbitrary smooth surfaces. Require
        # resolved three-dimensional curvature, rather than extrapolating from
        # a narrow latitude band or a nearly flat cap.
        normals = (points[ids]-center)/radius
        eigen = np.linalg.eigvalsh(np.cov(normals.T))
        span = np.ptp(points[ids], axis=0)
        if eigen[0] < .025 or span[2] < .65*radius or min(span[:2]) < radius:
            continue
        # Require a resolved two-dimensional spherical patch: six consecutive
        # latitude strips must each have measured support around at least half
        # a circumference. Disconnected rings on cylinders/limbs can happen to
        # intersect the same sphere but do not establish spherical curvature.
        elevation = np.arcsin(np.clip(normals[:,2], -1, 1))
        azimuth = np.arctan2(normals[:,1], normals[:,0])
        latitude = np.floor(elevation/.12).astype(int)
        longitude = np.floor((azimuth+math.pi)/math.tau*36).astype(int)%36
        strips = [int(level) for level in np.unique(latitude)
                  if len(np.unique(longitude[latitude == level])) >= 18]
        longest, active = 0, 0
        previous = None
        for level in strips:
            active = active+1 if previous is not None and level == previous+1 else 1
            longest = max(longest, active)
            previous = level
        if longest < 6:
            continue
        residuals = abs(np.linalg.norm(points[ids]-center, axis=1)-radius)
        rms = float(np.sqrt(np.mean(residuals**2)))
        if rms > .65*threshold:
            continue
        # Residuals alone are not dimensional accuracy. Record a conservative
        # sampling/residual bound and the actual spatial evidence.
        output.append(_record('sphere_diameter', 2*radius, ids=ids, points=points,
            calibration_id=calibration_id, reference=reference, direction=[1,0,0],
            region_id=f'sphere_{len(output)}', residual=rms, shape='sphere',
            center_xyz_mm=center.tolist(), radius_mm=radius,
            observed_height_range_mm=[float(points[ids,2].min()),float(points[ids,2].max())],
            normal_covariance_eigenvalues=eigen.tolist(),
            resolved_latitude_strips=strips, consecutive_latitude_strips=longest,
            uncertainty_mm={'kind':'fit_residual_and_sampling_not_physical_accuracy','bound':2*threshold}))
        claimed[ids] = True
    return output


def _curved_cylinder_neighborhood(points, record, policy):
    """Reject a near-equator ring when neighbouring observed circles vary."""
    lo, hi = record['observed_height_range_mm']
    center = np.asarray(record['center_xy_mm'])
    radius = record['value_mm']/2
    support=np.asarray(record['support']['point_indices'],int)
    observed=points[support]
    sphere_fit=_sphere_lstsq(observed)
    if sphere_fit is not None:
        sphere_center,sphere_radius=sphere_fit
        sphere_rms=float(np.sqrt(np.mean((np.linalg.norm(observed-sphere_center,axis=1)-sphere_radius)**2)))
        cylinder_rms=float(np.sqrt(np.mean((np.linalg.norm(observed[:,:2]-center,axis=1)-radius)**2)))
        if sphere_rms < .25*cylinder_rms and cylinder_rms-sphere_rms > .005:
            return {'method':'observed_spherical_patch_fits_better_than_constant_cylinder',
                    'sphere_residual_mm':sphere_rms,'cylinder_residual_mm':cylinder_rms,
                    'limitation':'patch alone does not authorize whole-sphere diameter'}
    padding = max(2*policy.section_height_mm, hi-lo)
    near = np.flatnonzero((points[:,2] >= lo-padding)&(points[:,2] <= hi+padding))
    levels = np.floor((points[near,2]-lo)/policy.section_height_mm).astype(int)
    slices = []
    for level in np.unique(levels):
        ids = near[levels == level]
        annulus = ids[np.linalg.norm(points[ids,:2]-center,axis=1) < radius+2*policy.residual_mm]
        if len(annulus) < policy.minimum_points:
            continue
        fit = _circle(points[annulus,:2])
        if fit is None:
            continue
        c,r = fit
        errors = abs(np.linalg.norm(points[annulus,:2]-c,axis=1)-r)
        if np.linalg.norm(c-center) > policy.residual_mm or np.quantile(errors,.9) > policy.residual_mm:
            continue
        angles = np.sort(np.arctan2(points[annulus,1]-c[1],points[annulus,0]-c[0]))
        coverage = math.tau-max(np.diff(np.r_[angles,angles[0]+math.tau]))
        if coverage >= policy.minimum_angular_coverage_rad:
            slices.append((float(np.median(points[annulus,2])),r))
    if len(slices) < 6:
        return None
    z,r = np.asarray(slices).T
    inside = (z >= lo)&(z <= hi)
    outside = ~inside
    if not inside.any() or not outside.any() or np.ptp(r) <= 1.5*policy.residual_mm:
        return None
    quadratic = np.column_stack(((z-z.mean())**2,z-z.mean(),np.ones(len(z))))
    fitted = quadratic@np.linalg.lstsq(quadratic,r,rcond=None)[0]
    rms = float(np.sqrt(np.mean((r-fitted)**2)))
    if rms < .35*policy.residual_mm and np.ptp(fitted) > 1.5*policy.residual_mm:
        return {'method':'observed_neighbour_section_radius_curvature',
                'sample_heights_mm':z.tolist(),'sample_radii_mm':r.tolist(),'residual_mm':rms}
    return None


def _plane_features(points, planes, calibration_id, reference, policy, point_metadata=None):
    """Local common-support distances; no all-plane-pairs global dimensions."""
    patches=[]
    for plane in planes:
        normal=np.asarray(plane['normal']); tangent=np.array([-normal[1],normal[0]])
        ids=np.asarray(plane['ids'],int)
        projected=np.column_stack((points[ids,:2]@tangent,points[ids,2]))
        for group in _components(projected,policy.component_cell_mm):
            support=ids[group]
            if len(support) < 40 or not _supported_plane_patch(points,support,normal,plane['offset'],tangent,policy):
                continue
            patches.append({**plane,'ids':support,'region_id':f'local_plane_{len(patches)}'})
    output=[]
    origins=None
    if point_metadata is not None and 'camera_origins_axis_mm' in point_metadata:
        origins=np.asarray(point_metadata['camera_origins_axis_mm'],float)
        if origins.shape != points.shape or not np.isfinite(origins).all():
            raise ValueError('camera_origins_axis_mm must be finite Nx3 in point order')
    for patch in patches:
        ids=patch['ids'];normal=patch['normal']
        patch['outward']=None
        if origins is not None:
            votes=(origins[ids,:2]-points[ids,:2])@normal
            positive=float(np.mean(votes>0));negative=float(np.mean(votes<0))
            if max(positive,negative)>=.85:
                patch['outward']=normal if positive>=.85 else -normal
        output.append(_record('plane_offset',patch['offset'],ids=ids,points=points,
            calibration_id=calibration_id,reference=reference,direction=[*normal,0],
            region_id=patch['region_id'],residual=patch['residual'],plane_normal=[*normal,0],
            normal_facing_confirmed=patch['outward'] is not None))
    for left,right in combinations(patches,2):
        normal=left['normal'];tangent=np.array([-normal[1],normal[0]])
        if abs(normal@right['normal']) < .995:
            continue
        aid,bid=left['ids'],right['ids']
        da,db=float(np.median(points[aid,:2]@normal)),float(np.median(points[bid,:2]@normal))
        if da>db:
            left_,right_=right,left;aid,bid=bid,aid;da,db=db,da
        else:
            left_,right_=left,right
        delta=db-da
        if delta<=2*policy.residual_mm:
            continue
        at,bt=points[aid,:2]@tangent,points[bid,:2]@tangent
        lo=np.maximum([at.min(),points[aid,2].min()],[bt.min(),points[bid,2].min()])
        hi=np.minimum([at.max(),points[aid,2].max()],[bt.max(),points[bid,2].max()])
        if hi[0]-lo[0]<2 or hi[1]-lo[1]<policy.minimum_segment_height_mm:
            continue
        cell=policy.component_cell_mm
        def common_cells(ids):
            xy=np.column_stack((points[ids,:2]@tangent,points[ids,2]))
            active=np.all((xy>=lo)&(xy<=hi),axis=1)
            grid=np.floor((xy[active]-lo)/cell).astype(int)
            return {tuple(row) for row in grid},ids[active]
        ac,ai=common_cells(aid);bc,bi=common_cells(bid);common=ac&bc
        if len(common)<4 or len(common)/max(1,min(len(ac),len(bc)))<.5:
            continue
        # Coplanar points from unrelated limbs at different tangential/height
        # positions cannot authorize a measurement across those limbs.
        grid_a=np.floor((np.column_stack((points[ai,:2]@tangent,points[ai,2]))-lo)/cell).astype(int)
        grid_b=np.floor((np.column_stack((points[bi,:2]@tangent,points[bi,2]))-lo)/cell).astype(int)
        ai=ai[np.array([tuple(g) in common for g in grid_a])]
        bi=bi[np.array([tuple(g) in common for g in grid_b])]
        kind='observed_parallel_plane_spacing';reason=None
        oa,ob=left_['outward'],right_['outward']
        if oa is not None and ob is not None:
            if oa@normal>.85 and ob@normal<-.85:
                # Facing walls with a third observed surface in their interior
                # are not an open gap over this shared visible region.
                proj=points[:,:2]@normal;tr=points[:,:2]@tangent
                interior=(proj>da+policy.residual_mm)&(proj<db-policy.residual_mm)&(tr>=lo[0])&(tr<=hi[0])&(points[:,2]>=lo[1])&(points[:,2]<=hi[1])
                if interior.sum() > max(5,.02*(len(ai)+len(bi))):
                    continue
                kind='gap'
            elif oa@normal<-.85 and ob@normal>.85:
                # Two exterior faces can belong to two separate objects. A
                # local normal distance additionally requires a continuous
                # observed side bridge spanning this distance at common Z.
                # This identifies connected faces, not a whole-solid bounding
                # box or a claim that all four faces/endpoints were observed.
                bridges=[]
                for bridge in patches:
                    if bridge is left_ or bridge is right_ or abs(bridge['normal']@normal)>.1:
                        continue
                    ids_bridge=bridge['ids']
                    active=ids_bridge[(points[ids_bridge,2]>=lo[1])&(points[ids_bridge,2]<=hi[1])]
                    if len(active)<20 or np.ptp(points[active,2])<.6*(hi[1]-lo[1]):
                        continue
                    projected=points[active,:2]@normal
                    if projected.min()>da+3*policy.residual_mm or projected.max()<db-3*policy.residual_mm:
                        continue
                    bridges.append(float(np.median(points[active,:2]@tangent)))
                if not any(a<=lo[0]+3*policy.residual_mm or a>=hi[0]-3*policy.residual_mm for a in bridges):
                    continue
                kind='local_width' 
            else:
                continue
        else:
            reason='material_or_gap_not_identifiable_without_reliable_view_facing_normals'
        ids=np.unique(np.r_[ai,bi])
        output.append(_record(kind,delta,ids=ids,points=points,calibration_id=calibration_id,
            reference=reference,direction=[*normal,0],region_id=left_['region_id']+'_to_'+right_['region_id'],
            residual=max(left_['residual'],right_['residual']),reason=reason,
            reference_regions=[left_['region_id'],right_['region_id']],
            common_tangent_height_range_mm=[lo.tolist(),hi.tolist()],common_occupied_cells=len(common),
            measurement_kind='normal_distance_between_locally_cosupported_observed_planes',
            dimensional_scope='local_connected_face_normal_distance_not_complete_part_extent',
            uncertainty_mm={'kind':'plane_residual_and_sampling_not_physical_accuracy','bound':2*policy.plane_residual_mm}))
    return output


def discover_features(points_axis_mm: object, *, calibration_id: str,
                      reference_basis: str | None = None, scan_angles_rad: Sequence[float] | None = None,
                      policy: FeaturePolicy = FeaturePolicy(),point_metadata=None,
                      association_policy: AssociationPolicy | None = None) -> list[dict[str,Any]]:
    points=np.asarray(points_axis_mm,float)
    if points.ndim!=2 or points.shape[1]!=3 or not np.isfinite(points).all():
        raise ValueError('finite Nx3 observed points required')
    reference=reference_basis or f'SIMULATION_ONLY axis XYZ mm; calibration={calibration_id}'
    if 'SIMULATION_ONLY' not in reference:
        raise ValueError('features require explicit synthetic reference')
    if len(points)<policy.minimum_points:
        return [_record('geometry',None,ids=np.arange(len(points)),points=points,
            calibration_id=calibration_id,reference=reference,direction=[0,0,1],region_id='all',reason='insufficient_observed_points')]
    validity=MeasurementValidity(True,True,True,reference)
    angles=np.asarray(scan_angles_rad if scan_angles_rad is not None else [],float)
    fixed=(len(angles)>0 and np.ptp(angles)<1e-8) or (len(angles)==0 and np.linalg.matrix_rank(points-points.mean(axis=0),tol=1e-6)<3)
    if fixed:
        output=_section_features(points,calibration_id,reference,validity,policy)
        association_policy=association_policy or AssociationPolicy()
        if association_policy.enabled and len(output)>1:
            output=associate_features(output,points,policy=association_policy)
        return output or [_record('geometry',None,ids=np.arange(len(points)),points=points,
            calibration_id=calibration_id,reference=reference,direction=[0,0,1],region_id='all',
            reason='insufficient_observed_geometric_support')]
    spheres=_sphere_features(points,calibration_id,reference,policy)
    sphere_mask=np.zeros(len(points),bool)
    for feature in spheres:
        sphere_mask[feature['support']['point_indices']]=True
    non_sphere=np.flatnonzero(~sphere_mask)
    # Remove supported spherical observations before constant-section fitting;
    # do not replace them with a completed ideal sphere.
    solids=_solid_features(points[non_sphere],calibration_id,reference,validity,policy) if len(non_sphere)>=policy.minimum_points else []
    for feature in solids:
        ids=non_sphere[np.asarray(feature['support']['point_indices'],int)]
        feature['support']['point_indices']=ids.tolist()
    invalid={}
    for feature in solids:
        if feature['type']=='diameter':
            curvature=_curved_cylinder_neighborhood(points,feature,policy)
            if curvature is not None:
                invalid[feature['region_id']]=curvature
    output=list(spheres)
    for feature in solids:
        if feature['region_id'] in invalid:
            if feature['type']=='diameter':
                ids=np.asarray(feature['support']['point_indices'],int)
                output.append(_record('curved_surface',None,ids=ids,points=points,
                    calibration_id=calibration_id,reference=reference,direction=[0,0,1],
                    region_id=feature['region_id'],reason='resolved_neighbor_radius_curvature_rejects_constant_cylinder',
                    curvature_evidence=invalid[feature['region_id']]))
        else:
            output.append(feature)
    owned=sphere_mask.copy()
    for feature in output:
        if feature['type']=='diameter' and feature['status']=='available':
            owned[feature['support']['point_indices']]=True
    plane_ids=np.flatnonzero(~owned)
    planes=_vertical_planes(points[plane_ids],policy) if len(plane_ids)>=policy.minimum_points else []
    for plane in planes:
        plane['ids']=plane_ids[plane['ids']]
    output.extend(_plane_features(points,planes,calibration_id,reference,policy,point_metadata))
    # Mark fit precision separately from physical accuracy for all older helper
    # records; no unsupported dimensional confidence is implied.
    for feature in output:
        feature.setdefault('uncertainty_mm',{'kind':'sampling_and_fit_not_physical_accuracy',
            'bound':policy.section_height_mm+float(feature['residual_mm'] or 0)})
    if not output:
        output=[_record('geometry',None,ids=np.arange(len(points)),points=points,
            calibration_id=calibration_id,reference=reference,direction=[0,0,1],region_id='all',reason='insufficient_observed_geometric_support')]
    association_policy=association_policy or AssociationPolicy()
    if association_policy.enabled and len(output)>1:
        output=associate_features(output,points,policy=association_policy)
    return output


def discover_features_with_audit(points_axis_mm: object, *, calibration_id: str,
                                 reference_basis: str | None = None,
                                 scan_angles_rad: Sequence[float] | None = None,
                                 policy: FeaturePolicy = FeaturePolicy(),point_metadata=None,
                                 association_policy: AssociationPolicy | None = None
                                 ) -> tuple[list[dict[str,Any]], dict[str,Any]]:
    """Return ``(merged_features, association_audit)``; raw candidates are kept.

    ``association_audit['original_features']`` is the untouched raw candidate
    list from the same extraction pass.  No truth/CAD argument exists here.
    """
    points=np.asarray(points_axis_mm,float)
    association_policy=association_policy or AssociationPolicy()
    raw=discover_features(points,calibration_id=calibration_id,reference_basis=reference_basis,
        scan_angles_rad=scan_angles_rad,policy=policy,point_metadata=point_metadata,
        association_policy=AssociationPolicy(enabled=False))
    if len(raw)>1:
        merged,audit=associate_features(raw,points,policy=association_policy,return_audit=True)
    else:
        merged=raw
        audit={"schema":"R26_FEATURE_ASSOCIATION_V1_AUDIT","policy":asdict(association_policy),
               "original_count":len(raw),"merged_count":len(raw),"dropped_count":0,
               "groups":[],"original_features":raw,
               "note":"fewer than two candidates; association skipped"}
    audit["raw_feature_count"]=len(raw);audit["merged_feature_count"]=len(merged)
    return merged,audit
