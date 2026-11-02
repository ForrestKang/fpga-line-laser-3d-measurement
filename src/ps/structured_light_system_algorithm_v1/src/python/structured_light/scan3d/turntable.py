"""Turntable axis/angle model and its checkerboard calibration.

The object frame is the camera frame at motor step 0. A section captured after
``steps`` motor pulses is mapped back by the inverse rotation about the axis
line. Until a calibration has been fitted from real checkerboard poses the
model is a declared assumption and must never be reported as a measurement.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any, Sequence

import numpy as np

from sl_geometry.geometry import fit_rotation_axis, unrotate_points


NOMINAL_STEPS_PER_REV = 3200
CALIBRATED = "calibrated_candidate"
ASSUMED = "assumed_uncalibrated"


class TurntableError(ValueError):
    """Raised when a turntable model or its calibration input is invalid."""


@dataclass(frozen=True, slots=True)
class TurntableModel:
    axis_point_mm: tuple[float, float, float]
    axis_direction: tuple[float, float, float]
    rad_per_step: float
    state: str
    source: str

    def __post_init__(self) -> None:
        point = np.asarray(self.axis_point_mm, float)
        direction = np.asarray(self.axis_direction, float)
        if point.shape != (3,) or direction.shape != (3,) or not (
                np.isfinite(point).all() and np.isfinite(direction).all()):
            raise TurntableError("axis point and direction must be finite 3-vectors")
        if not math.isclose(float(np.linalg.norm(direction)), 1.0, abs_tol=1e-9):
            raise TurntableError("axis_direction must be a unit vector")
        if not math.isfinite(self.rad_per_step) or self.rad_per_step <= 0:
            raise TurntableError("rad_per_step must be finite and positive")
        if self.state not in (CALIBRATED, ASSUMED):
            raise TurntableError(f"state must be {CALIBRATED!r} or {ASSUMED!r}")

    @property
    def calibrated(self) -> bool:
        return self.state == CALIBRATED

    @property
    def steps_per_rev(self) -> float:
        return 2.0 * math.pi / self.rad_per_step

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def angle_rad(self, steps: int) -> float:
        if isinstance(steps, bool) or not isinstance(steps, (int, np.integer)):
            raise TurntableError("steps must be an integer motor pulse count")
        return float(steps) * self.rad_per_step

    def to_object_frame(self, points_camera_mm: np.ndarray, steps: int) -> np.ndarray:
        points = np.asarray(points_camera_mm, float).reshape(-1, 3)
        if len(points) == 0:
            return points.copy()
        return unrotate_points(points, np.asarray(self.axis_point_mm), np.asarray(self.axis_direction),
                               self.angle_rad(steps))

    def to_json(self) -> dict[str, Any]:
        return {"schema": "sl_turntable_model_v1", **asdict(self), "fingerprint": self.fingerprint,
                "steps_per_rev": self.steps_per_rev}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "TurntableModel":
        if payload.get("schema") != "sl_turntable_model_v1":
            raise TurntableError("unknown turntable model schema")
        model = cls(tuple(payload["axis_point_mm"]), tuple(payload["axis_direction"]),
                    float(payload["rad_per_step"]), payload["state"], payload["source"])
        if payload.get("fingerprint") not in (None, model.fingerprint):
            raise TurntableError("turntable model fingerprint does not match its fields")
        return model


def assumed_turntable(axis_point_mm: Sequence[float], axis_direction: Sequence[float], *,
                      steps_per_rev: int = NOMINAL_STEPS_PER_REV, source: str) -> TurntableModel:
    """Build an explicitly assumed, uncalibrated model for visual previews only."""

    direction = np.asarray(axis_direction, float)
    norm = float(np.linalg.norm(direction))
    if not math.isfinite(norm) or norm <= 0:
        raise TurntableError("axis_direction must be a nonzero finite vector")
    if isinstance(steps_per_rev, bool) or not isinstance(steps_per_rev, int) or steps_per_rev <= 0:
        raise TurntableError("steps_per_rev must be a positive integer")
    return TurntableModel(tuple(float(v) for v in axis_point_mm), tuple(float(v) for v in direction / norm),
                          2.0 * math.pi / steps_per_rev, ASSUMED, source)


def placeholder_axis_from_stripe(points_camera_mm: np.ndarray, plane: Sequence[float], *,
                                 offset_mm: float) -> tuple[np.ndarray, np.ndarray]:
    """Guess an axis for an uncalibrated preview only.

    Assumes the axis lies in the laser plane, parallel to the stripe within one
    surface band (row-to-row steps, ignoring jumps at step edges), and
    ``offset_mm`` farther from the camera than the stripe centroid. The offset
    is a declared input, not measured, so any size read from the resulting
    cloud depends on it. Points must be in source-row order.
    """

    points = np.asarray(points_camera_mm, float).reshape(-1, 3)
    points = points[np.isfinite(points).all(axis=1)]
    if len(points) < 3:
        raise TurntableError("placeholder axis needs at least three section points")
    if not math.isfinite(offset_mm) or offset_mm < 0:
        raise TurntableError("offset_mm must be finite and non-negative")
    centroid = points.mean(axis=0)
    steps = np.diff(points, axis=0)
    lengths = np.linalg.norm(steps, axis=1)
    smooth = steps[lengths <= 3.0 * np.median(lengths)] if len(steps) else steps
    if len(smooth) >= 2:
        direction = smooth.sum(axis=0)
    else:
        _, _, vt = np.linalg.svd(points - centroid, full_matrices=False)
        direction = vt[0]
    direction = direction / np.linalg.norm(direction)
    normal = np.asarray(plane, float)[:3]
    away = np.cross(normal, direction)
    away /= np.linalg.norm(away)
    if float(away @ centroid) < 0:
        away = -away
    return centroid + offset_mm * away, direction


def board_pose(gray: np.ndarray, camera_matrix: Any, distortion: Any, *,
               inner_corners: tuple[int, int] = (11, 8), square_size_mm: float = 6.0) -> dict[str, Any]:
    """Return the checkerboard pose (board->camera) for one raw gray frame."""

    import cv2
    from sl_geometry.camera import checkerboard_object_points, detect_checkerboard

    detection = detect_checkerboard(gray, inner_corners)
    if not detection["found"]:
        return {"found": False, "detection": detection}
    corners = np.asarray(detection["corners_px"], np.float64).reshape(-1, 1, 2)
    objects = checkerboard_object_points(inner_corners, square_size_mm)
    K = np.asarray(camera_matrix, np.float64)
    dist = np.asarray(distortion, np.float64)
    ok, rvec, tvec = cv2.solvePnP(objects, corners, K, dist, flags=cv2.SOLVEPNP_ITERATIVE)
    if not ok:
        return {"found": False, "detection": detection, "reason": "solvePnP_failed"}
    projected, _ = cv2.projectPoints(objects, rvec, tvec, K, dist)
    residual = projected.reshape(-1, 2) - corners.reshape(-1, 2)
    rotation, _ = cv2.Rodrigues(rvec)
    return {"found": True, "rotation": rotation, "translation_mm": tvec.reshape(3),
            "reprojection_rms_px": float(np.sqrt(np.mean(np.sum(residual ** 2, axis=1)))),
            "detection": detection}


def _signed_angle(relative: np.ndarray, axis: np.ndarray) -> float:
    vector = 0.5 * np.array([relative[2, 1] - relative[1, 2], relative[0, 2] - relative[2, 0],
                             relative[1, 0] - relative[0, 1]])
    cosine = 0.5 * (float(np.trace(relative)) - 1.0)
    return math.atan2(float(axis @ vector), cosine)


def calibrate_turntable(rotations: Sequence[np.ndarray], translations_mm: Sequence[np.ndarray],
                        steps: Sequence[int], *, source: str) -> dict[str, Any]:
    """Fit the axis line and angle-per-step from board poses at known step counts.

    ``rotations[i]``/``translations_mm[i]`` is the board pose observed after
    ``steps[i]`` cumulative motor pulses. The axis line comes from
    :func:`sl_geometry.geometry.fit_rotation_axis`; each pose's signed angle about
    that axis is then regressed on its step count. All residuals are returned;
    no acceptance threshold is applied here.
    """

    R = np.asarray(rotations, float)
    t = np.asarray(translations_mm, float)
    count = len(steps)
    if R.shape != (count, 3, 3) or t.shape != (count, 3) or count < 3:
        raise TurntableError("need at least three paired rotations, translations and step counts")
    step_values = np.asarray([int(s) for s in steps], np.int64)
    if len(set(step_values.tolist())) != count:
        raise TurntableError("step counts must be distinct")
    order = np.argsort(step_values)
    R, t, step_values = R[order], t[order], step_values[order]

    fit = fit_rotation_axis(R, t)
    axis = np.asarray(fit["axis_direction"], float)
    angles = np.array([_signed_angle(R[i] @ R[0].T, axis) for i in range(count)])
    unwrapped = np.unwrap(angles)
    relative_steps = (step_values - step_values[0]).astype(float)
    design = np.column_stack((relative_steps, np.ones(count)))
    (slope, intercept), *_ = np.linalg.lstsq(design, unwrapped, rcond=None)
    if not math.isfinite(slope) or abs(slope) <= 1e-12:
        raise TurntableError("measured board angle does not change with motor steps")
    if slope < 0:
        axis, slope, intercept, unwrapped = -axis, -slope, -intercept, -unwrapped
    residual_deg = np.degrees(unwrapped - (slope * relative_steps + intercept))
    point = np.asarray(fit["axis_point_mm"], float)
    model = TurntableModel(tuple(point.tolist()), tuple(axis.tolist()), float(slope), CALIBRATED, source)
    return {
        "model": model,
        "steps": step_values.tolist(),
        "measured_angle_deg": np.degrees(unwrapped).tolist(),
        "angle_residual_deg": residual_deg.tolist(),
        "angle_residual_rms_deg": float(np.sqrt(np.mean(residual_deg ** 2))),
        "angle_intercept_deg": float(math.degrees(intercept)),
        "steps_per_rev_estimate": float(2.0 * math.pi / slope),
        "nominal_steps_per_rev": NOMINAL_STEPS_PER_REV,
        "axis_fit_rms_mm": float(fit["rms_mm"]),
        "axis_fit_residuals_mm": np.asarray(fit["residuals_mm"]).tolist(),
        "physical_accuracy_verified": False,
    }


__all__ = [
    "ASSUMED", "CALIBRATED", "NOMINAL_STEPS_PER_REV", "TurntableError", "TurntableModel",
    "assumed_turntable", "board_pose", "calibrate_turntable", "placeholder_axis_from_stripe",
]
