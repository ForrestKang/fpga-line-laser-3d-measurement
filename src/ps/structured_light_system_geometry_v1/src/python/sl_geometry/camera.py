"""Checkerboard camera calibration with explicit evidence boundaries.

This module reports software fits and reprojection measurements only.  It
does not certify the physical board's measured square size, focus, camera
mounting, or measurement accuracy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

import cv2
import numpy as np


def _checkerboard_shape(inner_corners: Sequence[int]) -> tuple[int, int]:
    if not isinstance(inner_corners, (tuple, list)) or len(inner_corners) != 2:
        raise ValueError("inner_corners must be (columns, rows)")
    cols, rows = inner_corners
    if (isinstance(cols, bool) or isinstance(rows, bool)
            or not isinstance(cols, (int, np.integer))
            or not isinstance(rows, (int, np.integer))
            or int(cols) < 2 or int(rows) < 2):
        raise ValueError("inner_corners columns and rows must both be integers >= 2")
    return int(cols), int(rows)


def _square_size(value: float) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError("square_size_mm must be a finite positive number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("square_size_mm must be a finite positive number") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError("square_size_mm must be a finite positive number")
    return result


def checkerboard_object_points(
    inner_corners: tuple[int, int], square_size_mm: float
) -> np.ndarray:
    """Return row-major planar points for ``(columns, rows)`` inner corners.

    The first point is ``(0, 0, 0)``; columns advance along +X and rows along
    +Y.  Coordinates use the caller-declared millimeter square size.
    """

    cols, rows = _checkerboard_shape(inner_corners)
    square = _square_size(square_size_mm)
    grid = np.zeros((rows * cols, 3), dtype=np.float64)
    grid[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * square
    return grid


def _raw_image_diagnostics(gray: np.ndarray) -> dict[str, Any]:
    return {
        "input_dtype": str(gray.dtype),
        "input_shape": [int(gray.shape[0]), int(gray.shape[1])],
        "preprocessing": "none",
        "minimum": int(gray.min()),
        "maximum": int(gray.max()),
        "mean": float(np.mean(gray, dtype=np.float64)),
        "standard_deviation": float(np.std(gray, dtype=np.float64)),
        "zero_fraction": float(np.count_nonzero(gray == 0) / gray.size),
        "max_value_fraction": float(np.count_nonzero(gray == 255) / gray.size),
    }


def _spacing_summary(values: np.ndarray) -> dict[str, float | int]:
    flat = np.asarray(values, dtype=np.float64).reshape(-1)
    mean = float(np.mean(flat))
    std = float(np.std(flat))
    return {
        "count": int(flat.size),
        "minimum": float(np.min(flat)),
        "maximum": float(np.max(flat)),
        "median": float(np.median(flat)),
        "mean": mean,
        "standard_deviation": std,
        "coefficient_of_variation": float(std / mean) if mean > 0 else math.inf,
    }


def _valid_detected_corners(
    corners: Any, *, expected_count: int, image_width: int, image_height: int
) -> tuple[np.ndarray | None, str | None, int]:
    try:
        points = np.asarray(corners, dtype=np.float64).reshape(-1, 2)
    except (TypeError, ValueError):
        return None, "corner_array_malformed", 0
    count = int(points.shape[0])
    if count != expected_count:
        return None, "corner_count_mismatch", count
    if not np.all(np.isfinite(points)):
        return None, "nonfinite_corner", count
    if (np.any(points[:, 0] < 0) or np.any(points[:, 0] >= image_width)
            or np.any(points[:, 1] < 0) or np.any(points[:, 1] >= image_height)):
        return None, "corner_outside_image", count
    if np.unique(points, axis=0).shape[0] != expected_count:
        return None, "duplicate_corner", count
    return points, None, count


def _corner_quality(points: np.ndarray, cols: int, rows: int) -> dict[str, Any]:
    grid = points.reshape(rows, cols, 2)
    horizontal = np.linalg.norm(grid[:, 1:, :] - grid[:, :-1, :], axis=2)
    vertical = np.linalg.norm(grid[1:, :, :] - grid[:-1, :, :], axis=2)
    return {
        "corner_bounds_px": {
            "x_min": float(np.min(points[:, 0])),
            "x_max": float(np.max(points[:, 0])),
            "y_min": float(np.min(points[:, 1])),
            "y_max": float(np.max(points[:, 1])),
        },
        "grid_spacing_px": {
            "horizontal": _spacing_summary(horizontal),
            "vertical": _spacing_summary(vertical),
        },
    }


def _validated_sb_flags(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError("SB flags must be an integer bitmask")
    flags = int(value)
    if flags < 0:
        raise ValueError("SB flags must be a non-negative integer bitmask")
    documented_names = (
        "CALIB_CB_NORMALIZE_IMAGE",
        "CALIB_CB_EXHAUSTIVE",
        "CALIB_CB_ACCURACY",
        "CALIB_CB_LARGER",
        "CALIB_CB_MARKER",
        "CALIB_CB_PLAIN",
    )
    allowed = 0
    for name in documented_names:
        constant = getattr(cv2, name, None)
        if isinstance(constant, (int, np.integer)):
            allowed |= int(constant)
    if flags & ~allowed:
        raise ValueError(f"unsupported SB flags bit(s): {flags & ~allowed:#x}")
    return flags


def detect_checkerboard(
    gray: np.ndarray, inner_corners: tuple[int, int], *, sb_flags: int = 0
) -> dict[str, Any]:
    """Detect all requested inner corners in a raw 8-bit grayscale image.

    This function never alters the input array and applies no external
    preprocessing.  ``sb_flags`` is an explicit bitmask of documented OpenCV
    SB options. ``CALIB_CB_NORMALIZE_IMAGE`` requests internal search-image
    normalization; ``CALIB_CB_ACCURACY`` requests OpenCV's internal upscaling
    for subpixel accuracy. Those policies and the exact flags are recorded.
    The classic fallback always uses flags=0. A partial result is never
    returned as a successful detection.
    """

    cols, rows = _checkerboard_shape(inner_corners)
    sb_flags = _validated_sb_flags(sb_flags)
    if not isinstance(gray, np.ndarray):
        raise TypeError("gray must be a NumPy uint8 grayscale image")
    if gray.ndim != 2 or gray.dtype != np.uint8:
        raise ValueError("gray must be a single-channel uint8 image; no conversion is implicit")
    if gray.size == 0 or gray.shape[0] == 0 or gray.shape[1] == 0:
        raise ValueError("gray image must be non-empty")

    height, width = map(int, gray.shape)
    expected_count = cols * rows
    attempts: list[dict[str, Any]] = []
    accepted: np.ndarray | None = None
    method = "none"
    sb_detector = getattr(cv2, "findChessboardCornersSB", None)
    sb_attempted = False

    if callable(sb_detector):
        sb_attempted = True
        try:
            found, result = sb_detector(gray, (cols, rows), flags=sb_flags)
            points, reason, returned_count = _valid_detected_corners(
                result, expected_count=expected_count,
                image_width=width, image_height=height,
            )
            attempts.append({
                "method": "opencv_sb",
                "flags": sb_flags,
                "found": bool(found),
                "returned_count": returned_count,
                "accepted": bool(found and points is not None),
                "reason": None if found and points is not None else (reason or "not_found"),
            })
            if found and points is not None:
                accepted = points
                method = "opencv_sb"
        except cv2.error as exc:
            attempts.append({
                "method": "opencv_sb",
                "flags": sb_flags,
                "found": False,
                "returned_count": 0,
                "accepted": False,
                "reason": "opencv_error",
                "error": str(exc),
            })
    else:
        attempts.append({
            "method": "opencv_sb",
            "flags": sb_flags,
            "found": False,
            "returned_count": 0,
            "accepted": False,
            "reason": "unavailable",
        })

    if accepted is None:
        try:
            found, result = cv2.findChessboardCorners(gray, (cols, rows), flags=0)
            points, reason, returned_count = _valid_detected_corners(
                result, expected_count=expected_count,
                image_width=width, image_height=height,
            )
            refinement = "not_run"
            if found and points is not None:
                refined = np.asarray(result, dtype=np.float32).reshape(-1, 1, 2).copy()
                try:
                    criteria = (
                        cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER,
                        30,
                        0.001,
                    )
                    refined = cv2.cornerSubPix(gray, refined, (5, 5), (-1, -1), criteria)
                    refined_points, refine_reason, _ = _valid_detected_corners(
                        refined, expected_count=expected_count,
                        image_width=width, image_height=height,
                    )
                    if refined_points is not None:
                        points = refined_points
                        refinement = "cornerSubPix_on_original_gray"
                    else:
                        refinement = "cornerSubPix_rejected:" + str(refine_reason)
                except cv2.error as exc:
                    refinement = "cornerSubPix_error:" + str(exc)
                accepted = points
                method = "opencv_classic"
            attempts.append({
                "method": "opencv_classic",
                "flags": 0,
                "found": bool(found),
                "returned_count": returned_count,
                "accepted": bool(found and points is not None),
                "reason": None if found and points is not None else (reason or "not_found"),
                "subpixel_refinement": refinement,
            })
        except cv2.error as exc:
            attempts.append({
                "method": "opencv_classic",
                "flags": 0,
                "found": False,
                "returned_count": 0,
                "accepted": False,
                "reason": "opencv_error",
                "error": str(exc),
            })

    diagnostics = _raw_image_diagnostics(gray)
    diagnostics["expected_count"] = expected_count
    diagnostics["sb_flags"] = sb_flags
    diagnostics["sb_detector_available"] = bool(callable(sb_detector))
    diagnostics["sb_detector_attempted"] = sb_attempted
    normalize_flag = int(getattr(cv2, "CALIB_CB_NORMALIZE_IMAGE", 2))
    accuracy_flag = int(getattr(cv2, "CALIB_CB_ACCURACY", 32))
    diagnostics["detector_internal_normalization"] = bool(
        sb_attempted and sb_flags & normalize_flag
    )
    diagnostics["detector_internal_upsampling"] = bool(
        sb_attempted and sb_flags & accuracy_flag
    )
    diagnostics["attempts"] = attempts
    if accepted is None:
        return {
            "found": False,
            "corners_px": [],
            "image_size": [width, height],
            "count": 0,
            "method": method,
            "diagnostics": diagnostics,
        }

    diagnostics.update(_corner_quality(accepted, cols, rows))
    return {
        "found": True,
        "corners_px": accepted.tolist(),
        "image_size": [width, height],
        "count": int(accepted.shape[0]),
        "method": method,
        "diagnostics": diagnostics,
    }


def _view_id(value: Any, index: int) -> tuple[Any, tuple[type, Any]]:
    if isinstance(value, bool) or not isinstance(value, (str, int, np.integer)):
        raise ValueError(f"observation {index} id must be a non-empty string or integer")
    normalized = int(value) if isinstance(value, np.integer) else value
    if isinstance(normalized, str) and not normalized.strip():
        raise ValueError(f"observation {index} id must not be empty")
    return normalized, (type(normalized), normalized)


def _validate_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    inner_corners: tuple[int, int],
    expected_image_size: tuple[int, int] | None = None,
) -> tuple[list[Any], tuple[int, int], list[np.ndarray]]:
    cols, rows = inner_corners
    expected_count = cols * rows
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
        raise ValueError("observations must be a non-empty sequence of mappings")
    if not observations:
        raise ValueError("observations must be a non-empty sequence")

    ids: list[Any] = []
    seen_ids: set[tuple[type, Any]] = set()
    sizes: list[tuple[int, int]] = []
    all_points: list[np.ndarray] = []
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            raise ValueError(f"observation {index} must be a mapping")
        view_id, id_key = _view_id(observation.get("id"), index)
        if id_key in seen_ids:
            raise ValueError(f"duplicate view id: {view_id!r}")
        seen_ids.add(id_key)

        raw_size = observation.get("image_size")
        if not isinstance(raw_size, (tuple, list)) or len(raw_size) != 2:
            raise ValueError(f"observation {view_id!r} image_size must be [width, height]")
        width, height = raw_size
        if (isinstance(width, bool) or isinstance(height, bool)
                or not isinstance(width, (int, np.integer))
                or not isinstance(height, (int, np.integer))
                or int(width) <= 0 or int(height) <= 0):
            raise ValueError(f"observation {view_id!r} image_size must contain positive integers")
        size = (int(width), int(height))
        sizes.append(size)

        try:
            points = np.asarray(observation.get("corners_px"), dtype=np.float64)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"observation {view_id!r} corners must be numeric") from exc
        if points.shape != (expected_count, 2):
            raise ValueError(
                f"observation {view_id!r} must have exactly {expected_count} (x, y) corners"
            )
        if not np.all(np.isfinite(points)):
            raise ValueError(f"observation {view_id!r} corners must be finite")
        if np.unique(points, axis=0).shape[0] != expected_count:
            raise ValueError(f"observation {view_id!r} contains duplicate corners")
        if (np.any(points[:, 0] < 0) or np.any(points[:, 0] >= size[0])
                or np.any(points[:, 1] < 0) or np.any(points[:, 1] >= size[1])):
            raise ValueError(f"observation {view_id!r} has a corner outside image_size")
        ids.append(view_id)
        all_points.append(points)

    if len(set(sizes)) != 1:
        raise ValueError("all observations must use the same image_size")
    image_size = sizes[0]
    if expected_image_size is not None and image_size != expected_image_size:
        raise ValueError("observation image_size does not match calibration geometry")
    return ids, image_size, all_points


def _zhang_constraint_row(h_i: np.ndarray, h_j: np.ndarray) -> np.ndarray:
    return np.array(
        [
            h_i[0] * h_j[0],
            h_i[0] * h_j[1] + h_i[1] * h_j[0],
            h_i[1] * h_j[1],
            h_i[2] * h_j[0] + h_i[0] * h_j[2],
            h_i[2] * h_j[1] + h_i[1] * h_j[2],
            h_i[2] * h_j[2],
        ],
        dtype=np.float64,
    )


def _pose_coverage(
    observations: Sequence[np.ndarray], object_points: np.ndarray
) -> dict[str, Any]:
    xy = np.asarray(object_points[:, :2], dtype=np.float64)
    constraints: list[np.ndarray] = []
    for index, points in enumerate(observations):
        homography, _ = cv2.findHomography(xy, points, method=0)
        if homography is None or not np.all(np.isfinite(homography)):
            raise ValueError(f"pose coverage could not estimate a homography for view {index}")
        scale = float(np.linalg.norm(homography))
        if not math.isfinite(scale) or scale == 0:
            raise ValueError(f"pose coverage has a degenerate homography for view {index}")
        homography = homography / scale
        h1, h2 = homography[:, 0], homography[:, 1]
        row12 = _zhang_constraint_row(h1, h2)
        row11_minus_22 = _zhang_constraint_row(h1, h1) - _zhang_constraint_row(h2, h2)
        for row in (row12, row11_minus_22):
            norm = float(np.linalg.norm(row))
            if not math.isfinite(norm) or norm == 0:
                raise ValueError(f"pose coverage has a zero Zhang constraint in view {index}")
            constraints.append(row / norm)

    matrix = np.stack(constraints, axis=0)
    singular_values = np.linalg.svd(matrix, compute_uv=False)
    tolerance = np.finfo(np.float64).eps * max(matrix.shape) * float(singular_values[0])
    rank = int(np.count_nonzero(singular_values > tolerance))
    identifiable = float(singular_values[0] / singular_values[4]) if singular_values.size >= 5 and singular_values[4] > 0 else None
    condition_limit = float(1.0 / math.sqrt(np.finfo(np.float64).eps))
    result = {
        "method": "zhang_planar_homography_constraints",
        "constraint_rows": int(matrix.shape[0]),
        "zhang_constraint_rank": rank,
        "rank_tolerance": float(tolerance),
        "singular_values": [float(value) for value in singular_values],
        "identifiable_condition_number": identifiable,
        "float64_condition_limit": condition_limit,
        "numerically_identifiable_intrinsic_parameters": 5,
    }
    if rank < 5 or identifiable is None or identifiable > condition_limit:
        raise ValueError(
            "pose coverage is degenerate or numerically ill-conditioned for camera intrinsics "
            f"(Zhang constraint rank {rank}/5; identifiable condition="
            f"{identifiable}; float64 limit={condition_limit}; "
            f"singular values={result['singular_values']})"
        )
    return result


def _reprojection_rms(
    object_points: np.ndarray,
    image_points: np.ndarray,
    rvec: np.ndarray,
    tvec: np.ndarray,
    camera_matrix: np.ndarray,
    distortion: np.ndarray,
) -> float:
    projected, _ = cv2.projectPoints(
        object_points, rvec, tvec, camera_matrix, distortion
    )
    residual = np.asarray(projected, dtype=np.float64).reshape(-1, 2) - image_points
    return float(np.sqrt(np.mean(np.sum(residual * residual, axis=1))))


def calibrate_camera_views(
    observations: Sequence[Mapping[str, Any]],
    inner_corners: tuple[int, int],
    square_size_mm: float,
    min_views: int = 15,
) -> dict[str, Any]:
    """Fit camera intrinsics from complete, pose-diverse training views.

    View count is only a minimum input gate.  Before fitting, the planar
    homographies must provide five numerically identifiable Zhang constraints.
    The returned object deliberately has no ``passed`` or sign-off flag.
    """

    cols, rows = _checkerboard_shape(inner_corners)
    square = _square_size(square_size_mm)
    if isinstance(min_views, bool) or not isinstance(min_views, (int, np.integer)) or int(min_views) < 3:
        raise ValueError("min_views must be an integer >= 3")
    minimum = int(min_views)
    ids, image_size, image_points = _validate_observations(
        observations, inner_corners=(cols, rows)
    )
    if len(image_points) < minimum:
        raise ValueError(f"at least {minimum} training views are required; got {len(image_points)}")

    object_points = checkerboard_object_points((cols, rows), square)
    pose_coverage = _pose_coverage(image_points, object_points)
    object_views = [object_points.astype(np.float32) for _ in image_points]
    image_views = [points.astype(np.float32).reshape(-1, 1, 2) for points in image_points]
    try:
        opencv_rms, camera_matrix, distortion, rvecs, tvecs = cv2.calibrateCamera(
            object_views,
            image_views,
            image_size,
            None,
            None,
            flags=0,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-12),
        )
    except cv2.error as exc:
        raise ValueError(f"OpenCV camera calibration failed: {exc}") from exc

    camera_matrix = np.asarray(camera_matrix, dtype=np.float64)
    distortion = np.asarray(distortion, dtype=np.float64).reshape(-1)
    if (camera_matrix.shape != (3, 3) or not np.all(np.isfinite(camera_matrix))
            or not np.all(np.isfinite(distortion)) or distortion.size != 5
            or camera_matrix[0, 0] <= 0 or camera_matrix[1, 1] <= 0):
        raise ValueError("OpenCV returned invalid camera parameters")

    per_view = []
    squared_error_sum = 0.0
    point_count = 0
    for view_id, points, rvec, tvec in zip(ids, image_points, rvecs, tvecs):
        projected, _ = cv2.projectPoints(object_points, rvec, tvec, camera_matrix, distortion)
        residual = np.asarray(projected, dtype=np.float64).reshape(-1, 2) - points
        squared = np.sum(residual * residual, axis=1)
        per_view.append({"id": view_id, "rms_px": float(np.sqrt(np.mean(squared)))})
        squared_error_sum += float(np.sum(squared))
        point_count += int(points.shape[0])
    rms_px = float(math.sqrt(squared_error_sum / point_count))
    if not math.isfinite(float(opencv_rms)) or not math.isfinite(rms_px):
        raise ValueError("OpenCV returned a non-finite reprojection error")

    return {
        "schema": "sl_camera_intrinsics_v1",
        "fit_state": "computed_unvalidated",
        "physical_signoff": "not_implied",
        "camera_matrix": camera_matrix.tolist(),
        "distortion_coefficients": distortion.tolist(),
        "distortion_model": "opencv_k1_k2_p1_p2_k3",
        "rms_px": rms_px,
        "opencv_rms_px": float(opencv_rms),
        "per_view_rms_px": per_view,
        "train_view_ids": ids,
        "view_count": len(ids),
        "image_size": [int(image_size[0]), int(image_size[1])],
        "inner_corners": [cols, rows],
        "square_size_mm": square,
        "units": "mm",
        "pose_coverage": pose_coverage,
        "opencv_version": str(cv2.__version__),
    }


def assess_camera_views(
    calibration: Mapping[str, Any],
    holdout_observations: Sequence[Mapping[str, Any]],
    inner_corners: tuple[int, int],
    square_size_mm: float,
) -> dict[str, Any]:
    """Measure independent holdout reprojection error with fixed intrinsics.

    Each holdout view gets a newly solved pose through PnP; camera intrinsics
    and distortion are never optimized on holdout observations.
    """

    if not isinstance(calibration, Mapping):
        raise ValueError("calibration must be the mapping returned by calibrate_camera_views")
    if calibration.get("schema") != "sl_camera_intrinsics_v1":
        raise ValueError("calibration schema is missing or unsupported")
    cols, rows = _checkerboard_shape(inner_corners)
    square = _square_size(square_size_mm)
    declared_corners = calibration.get("inner_corners")
    declared_square = calibration.get("square_size_mm")
    if (declared_corners != [cols, rows] or isinstance(declared_square, bool)
            or not isinstance(declared_square, (int, float, np.integer, np.floating))
            or float(declared_square) != square):
        raise ValueError("holdout checkerboard geometry does not match calibration geometry")
    raw_size = calibration.get("image_size")
    if not isinstance(raw_size, (tuple, list)) or len(raw_size) != 2:
        raise ValueError("calibration image_size is missing or malformed")
    width, height = raw_size
    if (isinstance(width, bool) or isinstance(height, bool)
            or not isinstance(width, (int, np.integer))
            or not isinstance(height, (int, np.integer))
            or int(width) <= 0 or int(height) <= 0):
        raise ValueError("calibration image_size is missing or malformed")
    image_size = (int(width), int(height))

    train_ids_raw = calibration.get("train_view_ids")
    if not isinstance(train_ids_raw, list) or not train_ids_raw:
        raise ValueError("calibration train_view_ids are missing")
    train_ids: set[tuple[type, Any]] = set()
    for index, value in enumerate(train_ids_raw):
        _, key = _view_id(value, index)
        if key in train_ids:
            raise ValueError("calibration contains duplicate training view ids")
        train_ids.add(key)

    holdout_ids, observed_size, image_points = _validate_observations(
        holdout_observations,
        inner_corners=(cols, rows),
        expected_image_size=image_size,
    )
    if observed_size != image_size:
        raise ValueError("holdout image_size does not match calibration geometry")
    for view_id in holdout_ids:
        _, key = _view_id(view_id, 0)
        if key in train_ids:
            raise ValueError(f"training/holdout view ID overlap: {view_id!r}")

    try:
        camera_matrix = np.asarray(calibration.get("camera_matrix"), dtype=np.float64)
        distortion = np.asarray(calibration.get("distortion_coefficients"), dtype=np.float64).reshape(-1)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("calibration camera parameters are malformed") from exc
    if (camera_matrix.shape != (3, 3) or distortion.size != 5
            or not np.all(np.isfinite(camera_matrix)) or not np.all(np.isfinite(distortion))
            or camera_matrix[0, 0] <= 0 or camera_matrix[1, 1] <= 0):
        raise ValueError("calibration camera parameters are invalid")

    object_points = checkerboard_object_points((cols, rows), square).astype(np.float64)
    per_view = []
    total_squared = 0.0
    total_points = 0
    for view_id, points in zip(holdout_ids, image_points):
        try:
            success, rvec, tvec = cv2.solvePnP(
                object_points,
                points.astype(np.float64),
                camera_matrix,
                distortion,
                flags=cv2.SOLVEPNP_ITERATIVE,
            )
        except cv2.error as exc:
            raise ValueError(f"holdout PnP failed for view {view_id!r}: {exc}") from exc
        if not success or not np.all(np.isfinite(rvec)) or not np.all(np.isfinite(tvec)):
            raise ValueError(f"holdout PnP did not yield a finite pose for view {view_id!r}")
        rotation, _ = cv2.Rodrigues(rvec)
        camera_points = object_points @ rotation.T + np.asarray(tvec, dtype=np.float64).reshape(1, 3)
        if np.any(camera_points[:, 2] <= 0):
            raise ValueError(f"holdout PnP placed checkerboard behind the camera for view {view_id!r}")
        projected, _ = cv2.projectPoints(object_points, rvec, tvec, camera_matrix, distortion)
        residual = np.asarray(projected, dtype=np.float64).reshape(-1, 2) - points
        squared = np.sum(residual * residual, axis=1)
        per_view.append({
            "id": view_id,
            "rms_px": float(np.sqrt(np.mean(squared))),
            "rvec_rad": np.asarray(rvec, dtype=np.float64).reshape(3).tolist(),
            "tvec_mm": np.asarray(tvec, dtype=np.float64).reshape(3).tolist(),
        })
        total_squared += float(np.sum(squared))
        total_points += int(points.shape[0])

    rms_px = float(math.sqrt(total_squared / total_points))
    return {
        "schema": "sl_camera_holdout_assessment_v1",
        "rms_px": rms_px,
        "per_view_rms_px": per_view,
        "holdout_view_ids": holdout_ids,
        "training_view_ids": list(train_ids_raw),
        "intrinsics_refit_on_holdout": False,
        "image_size": [image_size[0], image_size[1]],
        "inner_corners": [cols, rows],
        "square_size_mm": square,
        "units": "mm",
        "physical_signoff": "not_implied",
    }

