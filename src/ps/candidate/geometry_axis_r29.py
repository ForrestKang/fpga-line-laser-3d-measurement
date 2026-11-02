"""Content-bound Rodrigues constants; exact frozen operation ordering.

Only calibration-derived arrays are cached (16 content keys maximum). Points,
angles and results are never cached here. This is CPU-only replay geometry;
it computes no stripe decision and grants no frame admission.
"""
from functools import lru_cache
import math

import numpy as np
from sl_geometry.geometry import _array, _points, _unit_rows


def _restore(key):
    if key is None:
        return None
    shape, data = key
    return np.frombuffer(data, np.float64).reshape(shape)


@lru_cache(maxsize=16)
def _axis_arrays(point_key, direction_key):
    # The bytes-key owners are immutable; validation failures are not cached.
    point = _array(_restore(point_key), name='axis_point_mm', shape=(3,))
    direction = _array(_restore(direction_key), name='axis_direction', shape=(3,))
    return point, direction


@lru_cache(maxsize=16)
def _axis_constants(point_key, direction_key):
    point, direction = _axis_arrays(point_key, direction_key)
    axis = _unit_rows(direction.reshape(1, 3), name='axis_direction')[0]
    x, y, z = axis
    skew = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])
    # Internal arrays are backed by bytes, not writable cache allocations.
    immutable = lambda a: np.frombuffer(a.tobytes(), a.dtype).reshape(a.shape)
    return point, immutable(skew), immutable(np.outer(axis, axis)), immutable(np.eye(3))


def unrotate_prepared(points_mm, axis_key, angle_rad):
    """Same validation and arithmetic as sl_geometry.unrotate_points.

    The caller obtains axis_key from current numeric calibration contents on
    every call. Mutating calibration therefore selects a new validated key.
    Do not combine the two matrix products or cancel the point translations:
    either would change the frozen floating-point rounding contract.
    """
    points = _points(points_mm, name='points_mm', width=3)
    point_key, direction_key = axis_key[:2]
    _axis_arrays(point_key, direction_key)
    if isinstance(angle_rad, bool) or not isinstance(angle_rad, (int, float)):
        raise ValueError('angle_rad must be finite')
    angle = float(angle_rad)
    if not math.isfinite(angle):
        raise ValueError('angle_rad must be finite')
    point, skew, outer, identity = _axis_constants(point_key, direction_key)
    rotation = math.cos(angle) * identity - math.sin(angle) * skew
    rotation += (1.0 - math.cos(angle)) * outer
    return (points - point) @ rotation.T + point
