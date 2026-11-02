"""True duplicate association for observed geometric feature candidates.

A candidate is merged only when the *same observed entity* is re-described.  The
decision uses five independent evidence groups:

1. role/type compatibility (a diameter is never merged with a height),
2. observed point support overlap (fraction of the smaller support and Jaccard),
3. spatial position (solid centre, plane offset/point, spacing midpoint, section
   edge coordinate, sphere centre),
4. axis/normal agreement (signed for planes/spacings, sign-agnostic for the
   arbitrary axis label of a prism width/depth, fixed Z for height/centre),
5. measurement-range agreement (observed height range, tangent/Z support range,
   sphere height range), including a bounded contiguous-range rule for a track
   split by an outlier slice.

Scalar equality is **never** sufficient.  Two equal-sized solids at different
positions, two opposing faces, and two adjacent but independent coplanar faces
all remain distinct.  Original candidates are retained: merged output records
carry compact ``association`` provenance and ``associate_features(...,
return_audit=True)`` returns the untouched original records together with the
per-group match metrics.

The implementation is deterministic: candidates are ranked by evidence, each is
compared with existing cluster representatives only (no transitive chaining), and
output order follows the survivor's original position.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from typing import Any, Sequence

import numpy as np

SCHEMA = "R26_FEATURE_ASSOCIATION_V1"

SOLID_TYPES = frozenset({
    "diameter", "segment_height", "prism_width", "prism_depth", "prism_height",
    "center_x", "center_y",
})
PLANE_TYPES = frozenset({"plane_offset"})
SPACING_TYPES = frozenset({
    "parallel_plane_spacing", "gap", "local_width", "observed_parallel_plane_spacing",
})
SECTION_TYPES = frozenset({"section_depth_step", "groove_width"})
SPHERE_TYPES = frozenset({"sphere_diameter"})
DIMENSION_TYPES = SOLID_TYPES | PLANE_TYPES | SPACING_TYPES | SECTION_TYPES | SPHERE_TYPES


@dataclass(frozen=True)
class AssociationPolicy:
    """Bounded evidence gates; all distances are millimetres in the axis frame."""

    enabled: bool = True
    normal_cosine: float = 0.999
    solid_axis_cosine: float = 0.995
    position_tolerance_mm: float = 0.35
    plane_offset_tolerance_mm: float = 0.25
    value_tolerance_mm: float = 0.30
    support_overlap_min: float = 0.50
    support_jaccard_min: float = 0.20
    range_overlap_min: float = 0.60
    range_endpoint_sections: float = 2.0
    section_height_mm: float = 0.5
    section_sample_mm: float = 0.15
    minimum_support_points: int = 3
    maximum_pairs: int = 200000

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be bool")
        for name in ("normal_cosine", "solid_axis_cosine", "support_overlap_min",
                     "support_jaccard_min", "range_overlap_min"):
            value = getattr(self, name)
            if not np.isfinite(value) or not 0.0 < value <= 1.0:
                raise ValueError(name + " must be finite in (0, 1]")
        for name in ("position_tolerance_mm", "plane_offset_tolerance_mm",
                     "value_tolerance_mm", "range_endpoint_sections",
                     "section_height_mm", "section_sample_mm"):
            value = getattr(self, name)
            if not np.isfinite(value) or value <= 0:
                raise ValueError(name + " must be positive finite")
        for name in ("minimum_support_points", "maximum_pairs"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(name + " must be a positive integer")


def _unit(vector):
    values = np.asarray(vector, dtype=float).reshape(-1)
    norm = float(np.linalg.norm(values))
    if values.size < 2 or not np.isfinite(norm) or norm <= 1e-12:
        return None
    return values / norm


def _feature_direction(feature, length=3):
    direction = _unit(feature.get("direction"))
    if direction is None:
        return None
    if len(direction) < length:
        direction = np.r_[direction, np.zeros(length - len(direction))]
    return direction[:length]


def _support(feature):
    support = feature.get("support") or {}
    indices = support.get("point_indices")
    if indices is None:
        return np.empty(0, dtype=np.int64)
    values = np.asarray(indices, dtype=np.int64)
    if values.ndim != 1:
        raise ValueError("support point_indices must be one-dimensional")
    return values


def _validated_support(feature, point_count):
    values = _support(feature)
    if len(values) and (values.min() < 0 or values.max() >= point_count):
        raise ValueError("support point index outside the observed cloud")
    return np.unique(values)


def _overlap(first, second):
    if not len(first) or not len(second):
        return 0.0, 0.0, 0
    shared = int(np.intersect1d(first, second, assume_unique=True).size)
    smaller = int(min(len(first), len(second)))
    union = int(len(first) + len(second) - shared)
    return shared / smaller, shared / union if union else 0.0, shared


def _centroid(points, indices):
    if not len(indices):
        return None
    return points[indices].mean(axis=0)


def _bbox(points, indices):
    if not len(indices):
        return None
    observed = points[indices]
    return observed.min(axis=0), observed.max(axis=0)


def _bbox_overlap_score(first, second):
    """Mean per-axis intersection/union fraction; robust to zero-thickness faces."""
    if first is None or second is None:
        return 0.0
    (alo, ahi), (blo, bhi) = first, second
    fractions = []
    for axis in range(3):
        span = max(ahi[axis] - alo[axis], bhi[axis] - blo[axis])
        if span <= 1e-9:
            continue
        overlap = min(ahi[axis], bhi[axis]) - max(alo[axis], blo[axis])
        fractions.append(max(0.0, overlap) / span)
    return float(np.mean(fractions)) if fractions else 0.0


def _range_pair(feature, points, indices):
    lo = hi = None
    observed = feature.get("observed_height_range_mm")
    if observed is not None and len(observed) == 2:
        lo, hi = float(observed[0]), float(observed[1])
    elif len(indices):
        z = points[indices, 2]
        lo, hi = float(z.min()), float(z.max())
    return (lo, hi) if lo is not None and hi is not None else None


def _range_metrics(first, second, policy):
    if first is None or second is None:
        return{"overlap_fraction": 0.0, "endpoint_delta_mm": float("inf"), "compatible": False}
    lo = max(first[0], second[0])
    hi = min(first[1], second[1])
    span = min(first[1] - first[0], second[1] - second[0])
    overlap = max(0.0, hi - lo)
    fraction = overlap / span if span > 1e-9 else 0.0
    endpoint = max(abs(first[0] - second[0]), abs(first[1] - second[1]))
    limit = max(2.0 * policy.section_height_mm,
                0.10 * max(first[1] - first[0], second[1] - second[0]))
    compatible = fraction >= policy.range_overlap_min or endpoint <= limit
    return {"overlap_fraction": float(fraction), "endpoint_delta_mm": float(endpoint),
            "compatible": bool(compatible)}


def _plane_frame(points, indices, normal):
    observed = points[indices]
    up = np.asarray([0.0, 0.0, 1.0])
    if abs(float(normal @ up)) > 0.9:
        up = np.asarray([1.0, 0.0, 0.0])
    tangent = _unit(np.cross(normal, up))
    if tangent is None:
        tangent = np.asarray([1.0, 0.0, 0.0])
    normal_coordinate = observed @ normal
    tangent_coordinate = observed @ tangent
    z = observed[:, 2]
    return {
        "normal_range": (float(normal_coordinate.min()), float(normal_coordinate.max())),
        "tangent_range": (float(tangent_coordinate.min()), float(tangent_coordinate.max())),
        "z_range": (float(z.min()), float(z.max())),
        "tangent": tangent,
    }


def _interval_overlap(first, second):
    span = min(first[1] - first[0], second[1] - second[0])
    overlap = max(0.0, min(first[1], second[1]) - max(first[0], second[0]))
    return overlap / span if span > 1e-9 else 0.0


def _value(feature):
    value = feature.get("value_mm")
    if value is None:
        return None
    value = float(value)
    return value if np.isfinite(value) else None


def _record_rank(feature, support, residual, index):
    available = 1 if feature.get("status") == "available" and _value(feature) is not None else 0
    return (-available, -len(support), float(residual if residual is not None else 1e9),
            int(index))


def _compact(feature):
    """Candidate identity retained in merged provenance (no full index list)."""
    support = feature.get("support") or {}
    compact = {
        "type": feature.get("type"),
        "region_id": feature.get("region_id"),
        "value_mm": feature.get("value_mm"),
        "status": feature.get("status"),
        "reason": feature.get("reason"),
        "direction": feature.get("direction"),
        "residual_mm": feature.get("residual_mm"),
        "support_point_count": support.get("point_count"),
        "support_minimum_mm": support.get("minimum_mm"),
        "support_maximum_mm": support.get("maximum_mm"),
        "observed_height_range_mm": feature.get("observed_height_range_mm"),
        "reference_regions": feature.get("reference_regions"),
    }
    return {key: value for key, value in compact.items() if value is not None}


def _entry(feature, index, support):
    return {"feature": feature, "index": int(index), "support": support,
            "first": int(index)}


def _cached_plane_frame(entry, points, normal):
    """Immutable per-entry support statistic; never shared across entities.

    The cache lives on the internal clustering entry, which exists only for the
    duration of one ``associate_features`` call.  The frame is derived from the
    entry's own immutable feature direction, so it is identical on every
    comparison and cannot leak between two different supports.
    """
    cached = entry.get("plane_frame")
    if cached is None:
        cached = _plane_frame(points, entry["support"], normal)
        entry["plane_frame"] = cached
    return cached


def _cached_centroid(entry, points):
    if "support_centroid" not in entry:
        entry["support_centroid"] = _centroid(points, entry["support"])
    return entry["support_centroid"]


def _cached_bbox(entry, points):
    if "support_bbox" not in entry:
        entry["support_bbox"] = _bbox(points, entry["support"])
    return entry["support_bbox"]


def _plane_match(first, second, points, policy):
    a, b = first["feature"], second["feature"]
    if a.get("type") not in PLANE_TYPES or b.get("type") not in PLANE_TYPES:
        return None
    normal_a = _feature_direction(a)
    normal_b = _feature_direction(b)
    if normal_a is None or normal_b is None:
        return None
    cosine = float(normal_a @ normal_b)
    # A signed comparison is intentional: opposing faces are different features
    # even when |offset| is equal.
    if cosine < policy.normal_cosine:
        return None
    value_a, value_b = _value(a), _value(b)
    if value_a is None or value_b is None:
        return None
    offset_delta = abs(value_a - value_b)
    if offset_delta > policy.plane_offset_tolerance_mm:
        return None
    smaller, jaccard, shared = _overlap(first["support"], second["support"])
    if smaller < policy.support_overlap_min or jaccard < policy.support_jaccard_min:
        return None
    frame_a = _cached_plane_frame(first, points, normal_a)
    frame_b = _cached_plane_frame(second, points, normal_b)
    tangent_overlap = _interval_overlap(frame_a["tangent_range"], frame_b["tangent_range"])
    z_overlap = _interval_overlap(frame_a["z_range"], frame_b["z_range"])
    if tangent_overlap < 0.25 and z_overlap < 0.25:
        return None
    score = (1.0 - smaller) + (1.0 - jaccard) + offset_delta / policy.plane_offset_tolerance_mm
    return {"normal_cosine": cosine, "offset_delta_mm": float(offset_delta),
            "support_overlap_fraction": float(smaller), "support_jaccard": float(jaccard),
            "shared_support_points": int(shared), "tangent_range_overlap_fraction": float(tangent_overlap),
            "height_range_overlap_fraction": float(z_overlap), "score": float(score)}


def _solid_groups(features):
    groups: dict[str, list[int]] = {}
    for index, feature in enumerate(features):
        if feature.get("type") not in SOLID_TYPES:
            continue
        region = feature.get("region_id")
        if not isinstance(region, str) or not region.startswith("solid_"):
            continue
        groups.setdefault(region, []).append(index)
    return groups


def _solid_summary(features, indices, points, supports):
    records = [features[index] for index in indices]
    kinds = {record.get("type") for record in records}
    if "diameter" in kinds:
        kind = "cylinder"
    elif kinds & {"prism_width", "prism_depth", "prism_height"}:
        kind = "prism"
    else:
        return None
    dimensions = {}
    for record in records:
        name = record.get("type")
        value = _value(record)
        if value is not None and name in ("diameter", "segment_height", "prism_width",
                                          "prism_depth", "prism_height", "center_x", "center_y"):
            dimensions.setdefault(name, value)
    center = None
    if "center_x" in dimensions and "center_y" in dimensions:
        center = np.asarray([dimensions["center_x"], dimensions["center_y"]])
    else:
        for record in records:
            candidate = record.get("center_xy_mm")
            if candidate is not None and len(candidate) >= 2:
                center = np.asarray(candidate[:2], dtype=float)
                break
    support = np.unique(np.concatenate([supports[index] for index in indices])) if indices else np.empty(0, np.int64)
    if center is None:
        centroid = _centroid(points, support)
        center = centroid[:2] if centroid is not None else np.zeros(2)
    ranges = [_range_pair(record, points, supports[index]) for record, index in zip(records, indices)]
    ranges = [item for item in ranges if item is not None]
    height_range = (min(item[0] for item in ranges), max(item[1] for item in ranges)) if ranges else None
    residuals = [float(record["residual_mm"]) for record in records
                 if record.get("residual_mm") is not None]
    basis = []
    for record in records:
        if record.get("type") in ("prism_width", "prism_depth"):
            direction = _feature_direction(record, 2)
            if direction is not None:
                basis.append(direction)
    return {
        "indices": list(indices), "records": records, "kind": kind,
        "dimensions": dimensions, "center": np.asarray(center, dtype=float),
        "height_range": height_range, "support": support,
        "bbox": _bbox(points, support),
        "residual": float(np.median(residuals)) if residuals else 1e9,
        "basis": basis, "first": min(indices),
        "available": sum(1 for name, value in dimensions.items()
                         if value is not None and name not in ("center_x", "center_y")),
    }


def _dimension_close(first, second, policy):
    if first is None or second is None:
        return True
    return abs(first - second) <= policy.value_tolerance_mm + 0.02 * min(abs(first), abs(second))


def _solid_match(first, second, policy):
    if first["kind"] != second["kind"]:
        return None
    center_distance = float(np.linalg.norm(first["center"] - second["center"]))
    sizes = [abs(first["dimensions"][name]) for name in
             ("diameter", "segment_height", "prism_width", "prism_depth", "prism_height")
             if first["dimensions"].get(name) is not None]
    sizes += [abs(second["dimensions"][name]) for name in
              ("diameter", "segment_height", "prism_width", "prism_depth", "prism_height")
              if second["dimensions"].get(name) is not None]
    size = max(sizes + [1.0])
    gate = min(max(policy.position_tolerance_mm, 0.03 * size), 1.5)
    range_metrics = _range_metrics(first["height_range"], second["height_range"], policy)
    smaller, jaccard, shared = _overlap(first["support"], second["support"])
    bbox_score = _bbox_overlap_score(first["bbox"], second["bbox"])
    if center_distance > gate:
        return None
    if not range_metrics["compatible"]:
        return None
    metrics = {
        "center_distance_mm": center_distance,
        "position_gate_mm": float(gate),
        "support_overlap_fraction": float(smaller),
        "support_jaccard": float(jaccard),
        "shared_support_points": int(shared),
        "support_bbox_overlap": float(bbox_score),
        "height_range_overlap_fraction": range_metrics["overlap_fraction"],
        "height_endpoint_delta_mm": range_metrics["endpoint_delta_mm"],
    }
    if first["kind"] == "cylinder":
        if not _dimension_close(first["dimensions"].get("diameter"),
                                second["dimensions"].get("diameter"), policy):
            return None
    else:
        pairings = (
            (("prism_width", "prism_width"), ("prism_depth", "prism_depth")),
            (("prism_width", "prism_depth"), ("prism_depth", "prism_width")),
        )
        matched = False
        for (a_name, b_name), (a2_name, b2_name) in pairings:
            if (_dimension_close(first["dimensions"].get(a_name),
                                 second["dimensions"].get(b_name), policy)
                    and _dimension_close(first["dimensions"].get(a2_name),
                                         second["dimensions"].get(b2_name), policy)):
                matched = True
                break
        if not matched:
            return None
        axes_a = first["basis"]
        axes_b = second["basis"]
        if axes_a and axes_b:
            if len(axes_a) < 2 or len(axes_b) < 2:
                return None
            cosine = max(
                min(abs(float(axes_a[0] @ axes_b[0])), abs(float(axes_a[1] @ axes_b[1]))),
                min(abs(float(axes_a[0] @ axes_b[1])), abs(float(axes_a[1] @ axes_b[0]))),
            )
            if cosine < policy.solid_axis_cosine:
                return None
            metrics["axis_cosine"] = float(cosine)
    same_surface = smaller >= policy.support_overlap_min and jaccard >= policy.support_jaccard_min
    # A track split by a sparse/outlier slice can have disjoint support while its
    # observed position and range coincide.  Merge only if both supports occupy
    # the same fitted region and nearly the same measurement range; two coaxial
    # solids stacked with a real gap fail the range test, and two equal solids at
    # different positions fail the centre test.
    split_track = (center_distance <= 0.5 * gate
                   and range_metrics["overlap_fraction"] >= 0.90
                   and bbox_score >= 0.80)
    if not same_surface and not split_track:
        return None
    metrics["score"] = (center_distance / gate + (1.0 - smaller)
                        + (1.0 - range_metrics["overlap_fraction"]))
    return metrics


def _spacing_pair_key(feature, region_map):
    references = feature.get("reference_regions")
    if references is None or len(references) != 2:
        return None
    mapped = [region_map.get(str(reference), str(reference)) for reference in references]
    if len(set(mapped)) != 2:
        return None
    return tuple(sorted(mapped))


def _spacing_match(first, second, points, policy, region_map):
    a, b = first["feature"], second["feature"]
    if a.get("type") not in SPACING_TYPES or b.get("type") not in SPACING_TYPES:
        return None
    value_a, value_b = _value(a), _value(b)
    if value_a is None or value_b is None:
        return None
    if abs(value_a - value_b) > policy.value_tolerance_mm + 0.01 * max(abs(value_a), abs(value_b)):
        return None
    normal_a = _feature_direction(a)
    normal_b = _feature_direction(b)
    if normal_a is None or normal_b is None:
        return None
    cosine = float(normal_a @ normal_b)
    if cosine < policy.normal_cosine:
        return None
    key_a = _spacing_pair_key(a, region_map)
    key_b = _spacing_pair_key(b, region_map)
    same_pair = (key_a is not None and key_b is not None and key_a == key_b)
    if key_a is not None and key_b is not None and key_a != key_b:
        # Distinct surface pairs remain distinct even when their scalar distance
        # is identical.  This is the T01/T03 equal-value negative case.
        return None
    smaller, jaccard, shared = _overlap(first["support"], second["support"])
    bbox_score = _bbox_overlap_score(_cached_bbox(first, points),
                                     _cached_bbox(second, points))
    if same_pair:
        # Identity is the canonical surface pair itself.  A duplicate observation
        # may cover a shifted support subset, so the support centroid is not an
        # identity gate.  Require substantial support/bbox overlap instead.
        if smaller < policy.support_overlap_min and bbox_score < 0.50:
            return None
        if jaccard < 0.10:
            return None
        midpoint_distance = float(np.linalg.norm(_cached_centroid(first, points)
                                                 - _cached_centroid(second, points)))
        midpoint_gate_applied = False
    else:
        if smaller < policy.support_overlap_min or jaccard < policy.support_jaccard_min:
            return None
        centroid_a = _cached_centroid(first, points)
        centroid_b = _cached_centroid(second, points)
        midpoint_distance = float(np.linalg.norm(centroid_a - centroid_b))
        midpoint_gate_applied = True
        gate = max(2.0 * policy.position_tolerance_mm,
                   0.05 * max(abs(value_a), abs(value_b)))
        if midpoint_distance > gate:
            return None
    gate = max(2.0 * policy.position_tolerance_mm,
               0.05 * max(abs(value_a), abs(value_b)))
    metrics = {
        "normal_cosine": cosine, "value_delta_mm": float(abs(value_a - value_b)),
        "midpoint_distance_mm": midpoint_distance, "midpoint_gate_mm": float(gate),
        "midpoint_gate_applied": bool(midpoint_gate_applied),
        "same_canonical_pair": bool(same_pair),
        "support_overlap_fraction": float(smaller), "support_jaccard": float(jaccard),
        "support_bbox_overlap": float(bbox_score),
        "shared_support_points": int(shared),
        "canonical_surface_pair": list(key_a) if key_a is not None else None,
    }
    metrics["score"] = ((1.0 - smaller)
                        + (0.0 if same_pair else midpoint_distance / gate))
    return metrics


def _section_position(feature):
    if feature.get("edge_coordinate_mm") is not None:
        return float(feature["edge_coordinate_mm"])
    edges = feature.get("edges_mm")
    if edges is not None and len(edges) == 2:
        return 0.5 * (float(edges[0]) + float(edges[1]))
    return None


def _section_match(first, second, points, policy):
    a, b = first["feature"], second["feature"]
    if a.get("type") not in SECTION_TYPES or a.get("type") != b.get("type"):
        return None
    position_a = _section_position(a)
    position_b = _section_position(b)
    if position_a is None or position_b is None:
        return None
    tolerance = max(policy.position_tolerance_mm, 2.0 * policy.section_sample_mm)
    delta = abs(position_a - position_b)
    if delta > tolerance:
        return None
    normal_a = _feature_direction(a)
    normal_b = _feature_direction(b)
    cosine = float(normal_a @ normal_b) if normal_a is not None and normal_b is not None else 1.0
    if cosine < policy.normal_cosine:
        return None
    value_a, value_b = _value(a), _value(b)
    if value_a is not None and value_b is not None:
        if abs(value_a - value_b) > policy.value_tolerance_mm + 0.01 * max(abs(value_a), abs(value_b)):
            return None
    smaller, jaccard, shared = _overlap(first["support"], second["support"])
    if smaller < policy.support_overlap_min:
        return None
    metrics = {"edge_position_delta_mm": float(delta), "normal_cosine": cosine,
               "support_overlap_fraction": float(smaller), "support_jaccard": float(jaccard),
               "shared_support_points": int(shared),
               "score": delta / tolerance + (1.0 - smaller)}
    return metrics


def _sphere_match(first, second, points, policy):
    a, b = first["feature"], second["feature"]
    if a.get("type") not in SPHERE_TYPES or b.get("type") not in SPHERE_TYPES:
        return None
    center_a = a.get("center_xyz_mm")
    center_b = b.get("center_xyz_mm")
    value_a, value_b = _value(a), _value(b)
    if center_a is None or center_b is None or value_a is None or value_b is None:
        return None
    center_a = np.asarray(center_a, dtype=float)
    center_b = np.asarray(center_b, dtype=float)
    distance = float(np.linalg.norm(center_a - center_b))
    size = max(abs(value_a), abs(value_b), 1.0)
    gate = min(max(policy.position_tolerance_mm, 0.02 * size), 1.0)
    if distance > gate:
        return None
    if abs(value_a - value_b) > policy.value_tolerance_mm + 0.02 * min(abs(value_a), abs(value_b)):
        return None
    smaller, jaccard, shared = _overlap(first["support"], second["support"])
    if smaller < policy.support_overlap_min or jaccard < policy.support_jaccard_min:
        return None
    metrics = {"center_distance_mm": distance, "value_delta_mm": float(abs(value_a - value_b)),
               "support_overlap_fraction": float(smaller), "support_jaccard": float(jaccard),
               "shared_support_points": int(shared),
               "score": distance / gate + (1.0 - smaller)}
    return metrics


def _cluster(entries, matcher, limit=200000):
    """Greedy deterministic clustering against cluster representatives.

    Comparing only the representative avoids transitive chaining, which could
    otherwise merge A~B and B~C while A and C are different objects.  ``limit``
    bounds the number of geometry comparisons so an unexpectedly large candidate
    set fails explicitly instead of consuming the end-of-run budget silently.
    """
    entries = sorted(entries, key=lambda entry: entry["rank"])
    clusters = []
    comparisons = 0
    for entry in entries:
        best = None
        for cluster in clusters:
            comparisons += 1
            if comparisons > limit:
                raise ValueError("association candidate pair budget exceeded")
            metrics = matcher(entry, cluster["canonical"])
            if metrics is None:
                continue
            if best is None or metrics["score"] < best[0]:
                best = (metrics["score"], cluster, metrics)
        if best is None:
            clusters.append({"canonical": entry, "members": [entry], "metrics": [None]})
        else:
            _, cluster, metrics = best
            cluster["members"].append(entry)
            cluster["metrics"].append(metrics)
    return clusters


def _group_provenance(role, cluster, extra=None):
    canonical = cluster["canonical"]
    members = []
    for member, metrics in zip(cluster["members"], cluster["metrics"]):
        candidate = _compact(member["feature"])
        if "group" in member:
            candidate["records"] = [_compact(record) for record in member["records"]]
        members.append({
            "region_id": member["feature"].get("region_id"),
            "survivor": member is canonical,
            "candidate": candidate,
            "match": metrics,
        })
    support_union = np.unique(np.concatenate([member["support"] for member in cluster["members"]]))
    digest = hashlib.sha256(np.asarray(support_union, dtype="<i8").tobytes()).hexdigest()
    provenance = {
        "schema": SCHEMA,
        "role": role,
        "method": "same_observed_support_position_axis_and_range_not_scalar_equality",
        "merged_count": len(cluster["members"]),
        "survivor_region_id": canonical["feature"].get("region_id"),
        "merged_regions": [member["feature"].get("region_id") for member in cluster["members"]],
        "support_union_point_count": int(len(support_union)),
        "support_union_sha256": digest,
        "merged_from": members,
        "association_policy": "AssociationPolicy",
    }
    if extra:
        provenance.update(extra)
    return provenance


def _audit_group(role, cluster):
    return {
        "role": role,
        "survivor_index": int(cluster["canonical"]["first"]),
        "survivor_region_id": cluster["canonical"]["feature"].get("region_id"),
        "member_indices": [int(member["first"]) for member in cluster["members"]],
        "member_regions": [member["feature"].get("region_id") for member in cluster["members"]],
        "metrics": [dict(metrics) if metrics is not None else None
                    for metrics in cluster["metrics"]],
    }


def associate_features(features: Sequence[dict[str, Any]], points_xyz_mm: object, *,
                       policy: AssociationPolicy | None = None,
                       return_audit: bool = False):
    """Return candidates with same-entity duplicates merged under one survivor.

    ``points_xyz_mm`` is the observed axis-frame cloud used by the extractor.  It
    is used only to measure support overlap/ranges; no nominal/CAD value enters.
    When nothing is merged, the original list object is returned unchanged.
    """
    policy = policy or AssociationPolicy()
    features = list(features)
    originals = list(features)
    points = np.asarray(points_xyz_mm, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
        raise ValueError("finite Nx3 observed points required for association")
    if not policy.enabled or len(features) < 2:
        audit = {"schema": SCHEMA + "_AUDIT", "policy": asdict(policy),
                 "original_count": len(features), "merged_count": len(features),
                 "dropped_count": 0, "groups": [], "original_features": features}
        return (features, audit) if return_audit else features

    supports = {}
    for index, feature in enumerate(features):
        supports[index] = _validated_support(feature, len(points))

    already = any("association" in feature for feature in features)
    if already:
        # Idempotent: an already associated record is not folded again.
        audit = {"schema": SCHEMA + "_AUDIT", "policy": asdict(policy),
                 "original_count": len(features), "merged_count": len(features),
                 "dropped_count": 0, "groups": [], "original_features": originals,
                 "note": "already associated input left unchanged"}
        return (features, audit) if return_audit else features

    drops: set[int] = set()
    provenance_by_index: dict[int, dict] = {}
    audit_groups = []

    # 1. Planes first: spacings reference plane regions, so plane duplicates must
    #    be canonicalized before spacing identity is evaluated.
    plane_entries = []
    for index, feature in enumerate(features):
        if feature.get("type") not in PLANE_TYPES or _value(feature) is None:
            continue
        if len(supports[index]) < policy.minimum_support_points:
            continue
        entry = _entry(feature, index, supports[index])
        entry["rank"] = _record_rank(feature, entry["support"],
                                     feature.get("residual_mm"), index)
        plane_entries.append(entry)
    plane_clusters = _cluster(plane_entries,
                              lambda a, b: _plane_match(a, b, points, policy),
                              policy.maximum_pairs)
    region_map: dict[str, str] = {}
    for cluster in plane_clusters:
        if len(cluster["members"]) < 2:
            continue
        survivor = cluster["canonical"]["feature"]
        survivor_region = survivor.get("region_id")
        for member in cluster["members"][1:]:
            drops.add(member["index"])
            if survivor_region is not None and member["feature"].get("region_id") is not None:
                region_map[str(member["feature"]["region_id"])] = str(survivor_region)
        provenance = _group_provenance("plane_offset", cluster)
        provenance_by_index[cluster["canonical"]["index"]] = provenance
        audit_groups.append(_audit_group("plane_offset", cluster))

    # 2. Solid region groups (diameter/height/centre/prism dimensions).
    groups = _solid_groups(features)
    solid_entries = []
    for region, indices in groups.items():
        summary = _solid_summary(features, indices, points, supports)
        if summary is None or len(summary["support"]) < policy.minimum_support_points:
            continue
        entry = {"feature": features[summary["first"]], "index": summary["first"],
                 "support": summary["support"], "first": summary["first"],
                 "group": summary, "records": summary["records"]}
        entry["rank"] = (-summary["available"], -len(summary["support"]),
                         summary["residual"], summary["first"])
        solid_entries.append(entry)
    solid_clusters = _cluster(
        solid_entries,
        lambda a, b: _solid_match(a["group"], b["group"], policy),
        policy.maximum_pairs,
    )
    for cluster in solid_clusters:
        if len(cluster["members"]) < 2:
            continue
        canonical = cluster["canonical"]
        for member in cluster["members"][1:]:
            drops.update(member["group"]["indices"])
        provenance = _group_provenance("solid", cluster)
        for index in canonical["group"]["indices"]:
            provenance_by_index[index] = provenance
        audit_groups.append(_audit_group("solid", cluster))

    # 3. Spacings / gaps, using canonical parent-surface pairs.
    spacing_entries = []
    for index, feature in enumerate(features):
        if feature.get("type") not in SPACING_TYPES or _value(feature) is None:
            continue
        if len(supports[index]) < policy.minimum_support_points:
            continue
        entry = _entry(feature, index, supports[index])
        entry["rank"] = _record_rank(feature, entry["support"],
                                     feature.get("residual_mm"), index)
        spacing_entries.append(entry)
    spacing_clusters = _cluster(
        spacing_entries,
        lambda a, b: _spacing_match(a, b, points, policy, region_map),
        policy.maximum_pairs,
    )
    for cluster in spacing_clusters:
        if len(cluster["members"]) < 2:
            continue
        for member in cluster["members"][1:]:
            drops.add(member["index"])
        provenance = _group_provenance("spacing", cluster)
        survivor_index = cluster["canonical"]["index"]
        survivor = dict(cluster["canonical"]["feature"])
        original_references = survivor.get("reference_regions")
        if original_references is not None:
            remapped = [region_map.get(str(reference), str(reference))
                        for reference in original_references]
            if remapped != original_references:
                survivor["reference_regions"] = remapped
                provenance["reference_regions_original"] = original_references
        provenance_by_index[survivor_index] = provenance
        features[survivor_index] = survivor
        audit_groups.append(_audit_group("spacing", cluster))

    # Keep every surviving spacing reference consistent with the canonical plane
    # regions; a reference to a merged-away duplicate plane would otherwise point
    # at a record that is no longer in the output.
    if region_map:
        for index in range(len(features)):
            if index in drops:
                continue
            feature = features[index]
            if feature.get("type") not in SPACING_TYPES:
                continue
            references = feature.get("reference_regions")
            if references is None:
                continue
            remapped = [region_map.get(str(reference), str(reference))
                        for reference in references]
            if remapped != references:
                updated = dict(feature)
                updated["reference_regions"] = remapped
                updated["reference_regions_original"] = references
                features[index] = updated

    # 4. Fixed-section step/groove observations.
    section_entries = []
    for index, feature in enumerate(features):
        if feature.get("type") not in SECTION_TYPES:
            continue
        if len(supports[index]) < policy.minimum_support_points:
            continue
        entry = _entry(feature, index, supports[index])
        entry["rank"] = _record_rank(feature, entry["support"],
                                     feature.get("residual_mm"), index)
        section_entries.append(entry)
    section_clusters = _cluster(
        section_entries,
        lambda a, b: _section_match(a, b, points, policy),
        policy.maximum_pairs,
    )
    for cluster in section_clusters:
        if len(cluster["members"]) < 2:
            continue
        for member in cluster["members"][1:]:
            drops.add(member["index"])
        provenance_by_index[cluster["canonical"]["index"]] = _group_provenance("section", cluster)
        audit_groups.append(_audit_group("section", cluster))

    # 5. Spherical patches.
    sphere_entries = []
    for index, feature in enumerate(features):
        if feature.get("type") not in SPHERE_TYPES or _value(feature) is None:
            continue
        if len(supports[index]) < policy.minimum_support_points:
            continue
        entry = _entry(feature, index, supports[index])
        entry["rank"] = _record_rank(feature, entry["support"],
                                     feature.get("residual_mm"), index)
        sphere_entries.append(entry)
    sphere_clusters = _cluster(
        sphere_entries,
        lambda a, b: _sphere_match(a, b, points, policy),
        policy.maximum_pairs,
    )
    for cluster in sphere_clusters:
        if len(cluster["members"]) < 2:
            continue
        for member in cluster["members"][1:]:
            drops.add(member["index"])
        provenance_by_index[cluster["canonical"]["index"]] = _group_provenance("sphere", cluster)
        audit_groups.append(_audit_group("sphere", cluster))

    if not drops and not any(index in provenance_by_index for index in range(len(features))):
        audit = {"schema": SCHEMA + "_AUDIT", "policy": asdict(policy),
                 "original_count": len(features), "merged_count": len(features),
                 "dropped_count": 0, "groups": [], "original_features": originals,
                 "note": "no same-entity duplicate satisfied all evidence gates"}
        return (features, audit) if return_audit else features

    output = []
    for index, feature in enumerate(features):
        if index in drops:
            continue
        if index in provenance_by_index:
            merged = dict(feature)
            merged["association"] = provenance_by_index[index]
            output.append(merged)
        else:
            output.append(feature)
    audit = {
        "schema": SCHEMA + "_AUDIT",
        "policy": asdict(policy),
        "original_count": len(features),
        "merged_count": len(output),
        "dropped_count": len(drops),
        "groups": audit_groups,
        "region_map": region_map,
        "original_features": originals,
        "note": "originals are preserved here and in merged_from; matching used support/"
                "position/axis/range, never scalar equality alone",
    }
    return (output, audit) if return_audit else output
