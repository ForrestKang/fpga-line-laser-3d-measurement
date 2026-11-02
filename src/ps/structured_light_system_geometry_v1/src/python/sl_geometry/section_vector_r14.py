"""Opt-in R14 vector geometry; scalar fallback near decision boundaries.

The original section_batch.py remains unchanged. Coordinates are compared to
the scalar reference with a 1e-8 mm numerical budget; masks and reasons must
agree. Near parallel, depth and training boundaries use the scalar reference.
"""
import math

import numpy as np

from .geometry import _array, _points, _unit_rows, pixel_rays


def intersect_plane_vector(rays, plane, *, min_abs_cosine=1e-6,
                          depth_limits_mm=None, _section_range_reject=False):
    ray_rows = _points(rays, name='rays', width=3)
    coefficients = _array(plane, name='plane')
    if coefficients.shape != (4,):
        raise ValueError('plane must contain four coefficients')
    normal_scale = float(np.max(np.abs(coefficients[:3])))
    if not math.isfinite(normal_scale) or normal_scale == 0.0:
        raise ValueError('plane normal must have non-zero finite norm')
    scaled_normal = coefficients[:3] / normal_scale
    scaled_normal_length = float(np.linalg.norm(scaled_normal))
    normal = scaled_normal / scaled_normal_length
    offset = float((coefficients[3] / normal_scale) / scaled_normal_length)
    if not math.isfinite(offset):
        raise ValueError('plane offset is outside the finite numeric range')
    if isinstance(min_abs_cosine, bool) or not isinstance(min_abs_cosine, (int, float)):
        raise ValueError('min_abs_cosine must be a finite number in [0, 1]')
    cosine_threshold = float(min_abs_cosine)
    if not math.isfinite(cosine_threshold) or not 0.0 <= cosine_threshold <= 1.0:
        raise ValueError('min_abs_cosine must be a finite number in [0, 1]')
    limits = None
    if depth_limits_mm is not None:
        limits_array = _array(depth_limits_mm, name='depth_limits_mm', shape=(2,))
        lower, upper = map(float, limits_array)
        if lower < 0.0 or upper < lower:
            raise ValueError('depth_limits_mm must satisfy 0 <= minimum <= maximum')
        limits = (lower, upper)

    directions = _unit_rows(ray_rows, name='ray')
    count = len(directions)
    points = np.full((count, 3), np.nan, dtype=np.float64)
    ranges = np.full(count, np.nan, dtype=np.float64)
    valid = np.zeros(count, dtype=bool)
    reasons = np.full(count, None, dtype=object)
    forward = directions[:, 2] > 0.0
    reasons[~forward] = 'behind'
    forward_ids = np.flatnonzero(forward)
    selected_directions = directions[forward_ids]
    # Three explicit vector operations avoid hundreds of Python np.dot calls.
    # Near-degenerate rays retain the original scalar path below.
    cosines = ((normal[0] * selected_directions[:, 0]
                + normal[1] * selected_directions[:, 1])
               + normal[2] * selected_directions[:, 2])
    if np.any(np.abs(cosines) < 1e-3):
        from .section_batch import intersect_plane_batch
        return intersect_plane_batch(rays, plane, min_abs_cosine=min_abs_cosine,
                                     depth_limits_mm=depth_limits_mm)
    floor = max(cosine_threshold, 10.0 * np.finfo(np.float64).eps)
    if np.any(np.abs(np.abs(cosines)-floor) < 1e-12):
        from .section_batch import intersect_plane_batch
        return intersect_plane_batch(rays, plane, min_abs_cosine=min_abs_cosine,
                                     depth_limits_mm=depth_limits_mm)
    parallel = np.abs(cosines) <= floor
    reasons[forward_ids[parallel]] = 'parallel'
    ids = forward_ids[~parallel]
    with np.errstate(over='ignore', under='ignore', invalid='ignore', divide='ignore'):
        distances = -offset / cosines[~parallel]
    # Sections do not expose ranges of rejected points. A finite validated
    # upper range <=1000 mm permits cheap rejection of far outside rays;
    # public unconstrained intersection retains exact scalar behavior there.
    far_scalar = not (_section_range_reject and limits is not None and limits[1] <= 1000.)
    if ((far_scalar and np.any(np.abs(distances) > 1000.)) or np.any(np.abs(distances) < 1e-8) or (limits is not None and
            np.any((np.abs(distances-limits[0]) < 1e-8) | (np.abs(distances-limits[1]) < 1e-8)))):
        from .section_batch import intersect_plane_batch
        return intersect_plane_batch(rays, plane, min_abs_cosine=min_abs_cosine,
                                     depth_limits_mm=depth_limits_mm)
    ahead = np.isfinite(distances) & (distances > 0.0)
    reasons[ids[~ahead]] = 'behind'
    ids, distances = ids[ahead], distances[ahead]
    ranges[ids] = distances
    if limits is not None:
        in_range = (distances >= limits[0]) & (distances <= limits[1])
        reasons[ids[~in_range]] = 'out_of_range'
        ids, distances = ids[in_range], distances[in_range]
    with np.errstate(over='ignore', under='ignore', invalid='ignore'):
        hits = distances[:, None] * directions[ids]
    ahead = hits[:, 2] > 0.0
    reasons[ids[~ahead]] = 'behind'
    ranges[ids[~ahead]] = np.nan
    points[ids[ahead]] = hits[ahead]
    valid[ids[ahead]] = True
    return dict(points_mm=points, valid=valid, reasons=reasons.tolist(), range_mm=ranges)


def reconstruct_section_vector(rows, xc_fixed, stripe_valid, K, distortion, plane,
                              range_limits_mm, training_bounds_mm):
    rows = np.asarray(rows)
    centers = np.asarray(xc_fixed)
    accepted = np.asarray(stripe_valid, bool)
    if rows.ndim != 1 or centers.shape != rows.shape or accepted.shape != rows.shape:
        raise ValueError('row arrays must have identical one-dimensional shape')
    if not np.isfinite(rows).all() or not np.isfinite(centers).all():
        raise ValueError('nonfinite pixel coordinates')
    bounds = np.asarray(training_bounds_mm, float)
    if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or np.any(bounds[0] > bounds[1]):
        raise ValueError('invalid training bounds')
    points = np.full((len(rows), 3), np.nan)
    valid = np.zeros(len(rows), bool)
    outside = np.zeros(len(rows), bool)
    reasons = np.full(len(rows), 'invalid_stripe', dtype=object)
    ids = np.flatnonzero(accepted)
    pixels = np.column_stack((centers[ids] / 4096., rows[ids]))
    hit = intersect_plane_vector(pixel_rays(pixels, K, distortion), plane,
                                depth_limits_mm=range_limits_mm, _section_range_reject=True)
    points[ids] = hit['points_mm']
    valid[ids] = hit['valid']
    reasons[ids] = hit['reasons']
    if valid.any() and np.any(np.abs(points[valid, None, :] - bounds[None, :, :]) < 1e-8):
        from .section_batch import reconstruct_section_batch
        return reconstruct_section_batch(rows, xc_fixed, stripe_valid, K, distortion, plane,
                                         range_limits_mm, training_bounds_mm)
    outside[valid] = np.any((points[valid] < bounds[0]) | (points[valid] > bounds[1]), axis=1)
    return dict(points_mm=points, valid=valid, reasons=reasons,
                outside_training_bounds=outside)
