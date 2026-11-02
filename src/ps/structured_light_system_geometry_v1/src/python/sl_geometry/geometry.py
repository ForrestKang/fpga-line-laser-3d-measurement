"""Small, explicit camera-space geometry helpers for structured light.

All distances use millimetres. Camera rays use +Z as the forward direction;
``intersect_plane`` normalizes them before computing Euclidean range.
"""

from __future__ import annotations

from pathlib import Path
import math

import numpy as np


_NORM_EPS = 1e-12
_ROTATION_ATOL = 1e-6
_MIN_ROTATION_RAD = 1e-6


def _array(value: object, *, name: str, shape: tuple[int, ...] | None = None) -> np.ndarray:
    try:
        result = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a numeric array") from exc
    if shape is not None and result.shape != shape:
        raise ValueError(f"{name} must have shape {shape}, got {result.shape}")
    if not np.isfinite(result).all():
        raise ValueError(f"{name} must contain only finite values")
    return result


def _points(value: object, *, name: str, width: int, minimum_rows: int = 0) -> np.ndarray:
    result = _array(value, name=name)
    if result.ndim != 2 or result.shape[1] != width:
        raise ValueError(f"{name} must have shape (N, {width})")
    if result.shape[0] < minimum_rows:
        raise ValueError(f"{name} must contain at least {minimum_rows} rows")
    return result


def _unit_rows(rows: np.ndarray, *, name: str) -> np.ndarray:
    """Normalize finite vectors without overflowing on large finite inputs."""

    scales = np.max(np.abs(rows), axis=1)
    if np.any(scales == 0.0):
        raise ValueError(f"each {name} vector must have non-zero norm")
    scaled = rows / scales[:, None]
    lengths = np.linalg.norm(scaled, axis=1)
    if not np.isfinite(lengths).all() or np.any(lengths <= _NORM_EPS):
        raise ValueError(f"each {name} vector must have a finite non-zero norm")
    return scaled / lengths[:, None]


def pixel_rays(points_px: object, K: object, distortion: object | None = None) -> np.ndarray:
    """Convert original image ``(u,v)`` pixels to camera rays ``(x,y,1)``.

    The returned coordinates are normalized camera coordinates with z fixed
    at one, matching OpenCV's ``undistortPoints`` result. Pixels are not
    cropped, scaled, mirrored, or otherwise shifted here.
    """

    pixels = _points(points_px, name="points_px", width=2)
    intrinsic = _array(K, name="K", shape=(3, 3))
    if np.linalg.matrix_rank(intrinsic) != 3:
        raise ValueError("K must be nonsingular")
    if intrinsic[0, 0] <= 0 or intrinsic[1, 1] <= 0:
        raise ValueError("K focal lengths must be positive")
    if not np.allclose(intrinsic[2], [0.0, 0.0, 1.0], rtol=0.0, atol=1e-12):
        raise ValueError("K must use the standard [0, 0, 1] final row")

    coefficients = None
    if distortion is not None:
        coefficients = _array(distortion, name="distortion").reshape(-1)
        if coefficients.size not in (4, 5, 8, 12, 14):
            raise ValueError("distortion must contain 4, 5, 8, 12, or 14 coefficients")
    if pixels.shape[0] == 0:
        return np.empty((0, 3), dtype=np.float64)

    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - environment dependency
        raise RuntimeError("pixel_rays requires OpenCV") from exc
    normalized = cv2.undistortPoints(
        pixels.reshape(-1, 1, 2), intrinsic, coefficients
    ).reshape(-1, 2)
    if not np.isfinite(normalized).all():
        raise ValueError("OpenCV returned non-finite undistorted coordinates")
    return np.column_stack((normalized, np.ones(len(normalized), dtype=np.float64)))


def intersect_plane(
    rays: object,
    plane: object,
    *,
    min_abs_cosine: float = 1e-6,
    depth_limits_mm: tuple[float, float] | None = None,
) -> dict[str, object]:
    """Intersect camera-origin rays with ``a*x+b*y+c*z+d=0``.

    Rays are normalized, so ``range_mm`` is Euclidean distance along each
    forward ray. Invalid intersections keep a NaN point and one of the exact
    reasons ``parallel``, ``behind``, or ``out_of_range``. Malformed input
    arrays raise ``ValueError`` instead of silently dropping rows.
    """

    ray_rows = _points(rays, name="rays", width=3)
    coefficients = _array(plane, name="plane")
    if coefficients.shape != (4,):
        raise ValueError("plane must contain four coefficients")
    normal_scale = float(np.max(np.abs(coefficients[:3])))
    if not math.isfinite(normal_scale) or normal_scale == 0.0:
        raise ValueError("plane normal must have non-zero finite norm")
    scaled_normal = coefficients[:3] / normal_scale
    scaled_normal_length = float(np.linalg.norm(scaled_normal))
    normal = scaled_normal / scaled_normal_length
    offset = float((coefficients[3] / normal_scale) / scaled_normal_length)
    if not math.isfinite(offset):
        raise ValueError("plane offset is outside the finite numeric range")
    if isinstance(min_abs_cosine, bool) or not isinstance(min_abs_cosine, (int, float)):
        raise ValueError("min_abs_cosine must be a finite number in [0, 1]")
    cosine_threshold = float(min_abs_cosine)
    if not math.isfinite(cosine_threshold) or not 0.0 <= cosine_threshold <= 1.0:
        raise ValueError("min_abs_cosine must be a finite number in [0, 1]")

    limits: tuple[float, float] | None = None
    if depth_limits_mm is not None:
        limits_array = _array(depth_limits_mm, name="depth_limits_mm", shape=(2,))
        lower, upper = map(float, limits_array)
        if lower < 0.0 or upper < lower:
            raise ValueError("depth_limits_mm must satisfy 0 <= minimum <= maximum")
        limits = (lower, upper)

    directions = _unit_rows(ray_rows, name="ray")
    points = np.full((len(directions), 3), np.nan, dtype=np.float64)
    ranges = np.full(len(directions), np.nan, dtype=np.float64)
    valid = np.zeros(len(directions), dtype=bool)
    reasons: list[str | None] = [None] * len(directions)
    parallel_floor = max(cosine_threshold, 10.0 * np.finfo(np.float64).eps)

    for index, direction in enumerate(directions):
        if direction[2] <= 0.0:
            reasons[index] = "behind"
            continue
        cosine = float(np.dot(normal, direction))
        if abs(cosine) <= parallel_floor:
            reasons[index] = "parallel"
            continue
        distance = -offset / cosine
        if not math.isfinite(distance) or distance <= 0.0:
            reasons[index] = "behind"
            continue
        ranges[index] = distance
        if limits is not None and not limits[0] <= distance <= limits[1]:
            reasons[index] = "out_of_range"
            continue
        point = distance * direction
        if point[2] <= 0.0:
            reasons[index] = "behind"
            ranges[index] = np.nan
            continue
        points[index] = point
        valid[index] = True

    return {"points_mm": points, "valid": valid, "reasons": reasons, "range_mm": ranges}


def fit_plane(points_mm: object) -> dict[str, object]:
    """Fit a least-squares plane; reject fewer than three or collinear points."""

    points = _points(points_mm, name="points_mm", width=3, minimum_rows=3)
    centroid = points.mean(axis=0)
    centered = points - centroid
    _, singular_values, vh = np.linalg.svd(centered, full_matrices=False)
    rank = int(np.linalg.matrix_rank(centered))
    if rank < 2:
        raise ValueError("plane fit requires at least two independent point directions")
    normal = vh[-1]
    normal /= np.linalg.norm(normal)
    dominant = int(np.argmax(np.abs(normal)))
    if normal[dominant] < 0.0:
        normal = -normal
    offset = -float(np.dot(normal, centroid))
    residuals = points @ normal + offset
    rms = float(np.sqrt(np.mean(residuals * residuals)))
    return {
        "plane_coefficients": np.concatenate((normal, [offset])),
        "rms_mm": rms,
        "rank": rank,
        "singular_values": singular_values,
        "residuals_mm": residuals,
    }


def _rotation_axis(rotation: np.ndarray, angle: float) -> np.ndarray:
    sine = math.sin(angle)
    if abs(sine) > 1e-6:
        vector = np.array([
            rotation[2, 1] - rotation[1, 2],
            rotation[0, 2] - rotation[2, 0],
            rotation[1, 0] - rotation[0, 1],
        ]) / (2.0 * sine)
    else:
        symmetric = 0.5 * (rotation + np.eye(3))
        _, vectors = np.linalg.eigh(symmetric)
        vector = vectors[:, -1]
    length = float(np.linalg.norm(vector))
    if not math.isfinite(length) or length <= _NORM_EPS:
        raise ValueError("rotation axis is numerically ambiguous")
    return vector / length


def fit_rotation_axis(rotations: object, translations: object) -> dict[str, object]:
    """Estimate a fixed camera-space rotation axis from rigid board poses.

    ``rotations[i]`` and ``translations[i]`` are the board-to-common-frame
    pose for the same rigid board. At least three non-collinear translations
    and measurable relative rotations are required. The returned axis point
    is the minimum-norm point on the line (perpendicular to its direction).
    Residuals are reported without applying an optical acceptance threshold.
    """

    poses_r = _array(rotations, name="rotations")
    poses_t = _points(translations, name="translations", width=3, minimum_rows=3)
    if poses_r.ndim != 3 or poses_r.shape[1:] != (3, 3):
        raise ValueError("rotations must have shape (N, 3, 3)")
    if len(poses_r) != len(poses_t) or len(poses_r) < 3:
        raise ValueError("rotations and translations must contain at least three paired poses")
    identity = np.eye(3)
    for index, rotation in enumerate(poses_r):
        if (not np.allclose(rotation.T @ rotation, identity, rtol=0.0, atol=_ROTATION_ATOL)
                or not math.isclose(float(np.linalg.det(rotation)), 1.0,
                                    rel_tol=0.0, abs_tol=_ROTATION_ATOL)):
            raise ValueError(f"rotations[{index}] must be a proper orthonormal matrix")

    centered_translations = poses_t - poses_t.mean(axis=0)
    translation_rank = int(np.linalg.matrix_rank(centered_translations))
    if translation_rank < 2:
        raise ValueError("rotation-axis fit requires non-collinear board translations")

    reference_r = poses_r[0]
    reference_t = poses_t[0]
    relative_rotations: list[np.ndarray] = []
    relative_translations: list[np.ndarray] = []
    directions: list[np.ndarray] = []
    angles: list[float] = []
    for rotation, translation in zip(poses_r[1:], poses_t[1:]):
        relative_r = rotation @ reference_r.T
        cosine = float(np.clip((np.trace(relative_r) - 1.0) * 0.5, -1.0, 1.0))
        angle = math.acos(cosine)
        if angle <= _MIN_ROTATION_RAD:
            continue
        relative_t = translation - relative_r @ reference_t
        relative_rotations.append(relative_r)
        relative_translations.append(relative_t)
        directions.append(_rotation_axis(relative_r, angle))
        angles.append(angle)
    if len(angles) < 2:
        raise ValueError("rotation-axis fit requires at least two non-trivial relative rotations")

    direction_rows = np.asarray(directions)
    direction_covariance = direction_rows.T @ direction_rows
    eigenvalues, eigenvectors = np.linalg.eigh(direction_covariance)
    if eigenvalues[-1] - eigenvalues[-2] <= 1e-10:
        raise ValueError("rotation-axis direction is ambiguous")
    axis = eigenvectors[:, -1]
    dominant = int(np.argmax(np.abs(axis)))
    if axis[dominant] < 0.0:
        axis = -axis

    blocks = [identity - relative for relative in relative_rotations]
    matrix = np.vstack((*blocks, axis.reshape(1, 3)))
    target = np.concatenate((*relative_translations, [0.0]))
    axis_point, _, solve_rank, _ = np.linalg.lstsq(matrix, target, rcond=None)
    if solve_rank < 3:
        raise ValueError("rotation-axis line is not uniquely determined")
    axis_point -= axis * float(np.dot(axis, axis_point))
    residual_vectors = np.asarray([
        block @ axis_point - displacement
        for block, displacement in zip(blocks, relative_translations)
    ])
    residuals = np.linalg.norm(residual_vectors, axis=1)
    rms = float(np.sqrt(np.mean(residuals * residuals)))
    return {
        "axis_direction": axis,
        "axis_point_mm": axis_point,
        "residuals_mm": residuals,
        "rms_mm": rms,
        "translation_rank": translation_rank,
        "relative_angles_rad": np.asarray(angles),
    }


def unrotate_points(
    points_mm: object,
    axis_point_mm: object,
    axis_direction: object,
    angle_rad: float,
) -> np.ndarray:
    """Apply the inverse Rodrigues rotation about an axis line."""

    points = _points(points_mm, name="points_mm", width=3)
    point = _array(axis_point_mm, name="axis_point_mm", shape=(3,))
    direction = _array(axis_direction, name="axis_direction", shape=(3,))
    if isinstance(angle_rad, bool) or not isinstance(angle_rad, (int, float)):
        raise ValueError("angle_rad must be finite")
    angle = float(angle_rad)
    if not math.isfinite(angle):
        raise ValueError("angle_rad must be finite")
    axis = _unit_rows(direction.reshape(1, 3), name="axis_direction")[0]
    x, y, z = axis
    skew = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])
    rotation = math.cos(angle) * np.eye(3) - math.sin(angle) * skew
    rotation += (1.0 - math.cos(angle)) * np.outer(axis, axis)
    return (points - point) @ rotation.T + point


def write_ply(points_mm: object, path: str | Path) -> Path:
    """Write finite vertices as ASCII PLY; never overwrite an existing path."""

    points = _points(points_mm, name="points_mm", width=3)
    output = Path(path)
    with output.open("x", encoding="ascii", newline="\n") as stream:
        stream.write("ply\nformat ascii 1.0\n")
        stream.write(f"element vertex {len(points)}\n")
        stream.write("property float x\nproperty float y\nproperty float z\nend_header\n")
        for x, y, z in points:
            stream.write(f"{x:.9g} {y:.9g} {z:.9g}\n")
    return output


__all__ = [
    "pixel_rays", "intersect_plane", "fit_plane", "fit_rotation_axis",
    "unrotate_points", "write_ply",
]
