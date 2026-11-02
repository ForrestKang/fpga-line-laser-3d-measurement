"""Preserve row association and rejection status for a static XYZ section."""
import numpy as np
from .geometry import pixel_rays, intersect_plane


def reconstruct_section(rows, xc_fixed, stripe_valid, K, distortion, plane,
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
    hit = intersect_plane(pixel_rays(pixels, K, distortion), plane,
                          depth_limits_mm=range_limits_mm)
    points[ids] = hit['points_mm']
    valid[ids] = hit['valid']
    reasons[ids] = hit['reasons']
    outside[valid] = np.any((points[valid] < bounds[0]) | (points[valid] > bounds[1]), axis=1)
    return {'points_mm': points, 'valid': valid, 'reasons': reasons,
            'outside_training_bounds': outside}
