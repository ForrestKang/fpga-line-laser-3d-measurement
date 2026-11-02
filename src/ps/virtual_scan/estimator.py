"""Image-only simulation adapter around the frozen production numeric chain.

The physical ContinuousProcessor admission gate intentionally remains untouched:
synthetic calibration cannot establish physical axis/timing validity. This module
uses its CloudSnapshot record and strict geometry rule, and independently admits
only explicitly synthetic frame metadata into an observed-point simulation cloud.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
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

from sl_geometry.geometry import unrotate_points
from sl_geometry.section import reconstruct_section
from structured_light.scan3d.continuous_pl_input import strict_geometry_mask
from structured_light.scan3d.continuous_processor import CloudSnapshot
from structured_light.scan3d.segment_candidates_r15 import fixed_segment_policy
from structured_light.stripe_v1.candidates import extract_candidates
from structured_light.stripe_v1.selection_v4 import select_candidates_v4
from structured_light.stripe_v1.types import StripeConfig


def _readonly(value: Any) -> np.ndarray:
    array = np.asarray(value).copy()
    array.setflags(write=False)
    return array


@dataclass(frozen=True)
class VirtualProfile:
    frame_id: int
    timestamp_s: float
    angle_rad: float
    rows: np.ndarray
    center_q12: np.ndarray
    stripe_accepted: np.ndarray
    points_camera_mm: np.ndarray
    points_axis_mm: np.ndarray
    valid_points: np.ndarray
    reasons: tuple[str | None, ...]
    rejection_counts: dict[str, int]
    calibration_id: str
    cloud_revision: int
    evidence_type: str = "SIMULATION_ONLY"
    physical_validation: bool = False

    @property
    def valid_point_count(self) -> int:
        return int(self.valid_points.sum())

    @property
    def valid(self) -> bool:
        return self.valid_point_count >= 3

    @property
    def profile_valid(self) -> bool:
        return self.valid

    @property
    def fused(self) -> bool:
        return self.valid_point_count > 0

    def summary(self) -> dict[str, Any]:
        return {"frame_id": self.frame_id, "request_id": self.frame_id,
                "timestamp_s": self.timestamp_s, "angle_rad": self.angle_rad,
                "valid_point_count": self.valid_point_count,
                "stripe_rows": int(self.stripe_accepted.sum()), "valid": self.valid,
                "fused": self.fused, "cloud_revision": self.cloud_revision,
                "rejection_counts": dict(self.rejection_counts),
                "calibration_id": self.calibration_id, "evidence_type": self.evidence_type,
                "physical_validation": False, "physical_accuracy_verified": False,
                "numeric_chain": "extract_candidates/selection_v4/original_q12/reconstruct_section/strict_geometry_mask/unrotate_points"}


class VirtualScanEstimator:
    """Reconstruct 752x480 gray8 and calibrated scan angle, never CAD/truth.

    Config is an exported synthetic camera/laser/axis calibration mapping or its
    JSON path. Calibration bounds describe the simulator working volume, not a
    target. Frame IDs and timestamps must strictly advance. Only observed valid
    rows enter the cloud, including profiles with fewer than three valid points.
    """
    def __init__(self, config: dict[str, Any] | str | Path):
        if isinstance(config, (str, Path)):
            config = json.loads(Path(config).read_text(encoding="utf-8"))
        if config.get("evidence_type") != "SIMULATION_ONLY":
            raise ValueError("isolated SIMULATION_ONLY calibration required")
        if tuple(config.get("image_size", ())) != (752, 480):
            raise ValueError("calibration must describe 752x480 gray frames")
        self.calibration_id = str(config["calibration_id"])
        self.camera_matrix = np.asarray(config["camera_matrix"], float).copy()
        self.distortion = np.asarray(config["distortion_coefficients"], float).copy()
        self.plane = np.asarray(config["laser_plane_camera"], float).copy()
        self.axis_point = np.asarray(config["axis_point_camera_mm"], float).copy()
        self.axis_direction = np.asarray(config["axis_direction_camera"], float).copy()
        radial = np.asarray(config["radial_reference_camera"], float).copy()
        for value in (self.camera_matrix, self.distortion, self.plane,
                      self.axis_point, self.axis_direction, radial):
            if not np.isfinite(value).all():
                raise ValueError("nonfinite simulation calibration")
        if self.axis_point.shape != (3,) or self.axis_direction.shape != (3,) or radial.shape != (3,):
            raise ValueError("axis calibration requires 3-vectors")
        if np.linalg.norm(self.axis_direction) <= 1e-12:
            raise ValueError("nonzero calibrated axis required")
        self.axis_direction /= np.linalg.norm(self.axis_direction)
        radial -= self.axis_direction * (radial @ self.axis_direction)
        if np.linalg.norm(radial) <= 1e-12:
            raise ValueError("radial basis cannot be parallel to axis")
        radial /= np.linalg.norm(radial)
        self.basis_camera = np.column_stack((radial, np.cross(self.axis_direction, radial), self.axis_direction))
        self.ray_range = tuple(config.get("ray_range_mm", (1., 2000.)))
        self.bounds = np.asarray(config.get("camera_working_bounds_mm", [[-1000., -1000., 1.], [1000., 1000., 2000.]]), float)
        # Full sensor policy, independent of target position, dimensions or name.
        self.stripe_config = StripeConfig(fixed_threshold=60, min_width=1, max_width=32,
                                         roi=(0, 0, 752, 480))
        self.selection = fixed_segment_policy()
        reconstruct_section(np.zeros(0, int), np.zeros(0, int), np.zeros(0, bool),
                            self.camera_matrix, self.distortion, self.plane, self.ray_range, self.bounds)
        self.reference_basis = f"SIMULATION_ONLY axis XYZ mm; calibration={self.calibration_id}"
        self._cloud: list[np.ndarray] = []
        self._revision = 0
        self._last_id: int | None = None
        self._last_time: float | None = None
        self._angles: list[float] = []
        self._first_section: np.ndarray | None = None
        self._first_section_angle: float | None = None

    def process(self, gray: np.ndarray, frame_metadata: dict[str, Any]) -> tuple[VirtualProfile, np.ndarray]:
        image = np.asarray(gray)
        if image.shape != (480, 752) or image.dtype != np.uint8:
            raise ValueError("input must be 752x480 uint8 gray")
        frame = extract_candidates(image, self.stripe_config)
        selected = select_candidates_v4(image, frame, self.stripe_config, self.selection)
        centers = np.asarray([r.candidate.center_q12 if r.candidate is not None else 0
                              for r in selected.rows], np.int64)
        accepted = np.asarray([r.candidate is not None and r.reason is None for r in selected.rows], bool)
        return self.process_stripe(centers, accepted, tuple(r.reason for r in selected.rows), frame_metadata)

    def process_stripe(self, center_q12: np.ndarray, stripe_accepted: np.ndarray,
                       stripe_reasons: Sequence[str | None], frame_metadata: dict[str, Any]
                       ) -> tuple[VirtualProfile, np.ndarray]:
        """Numeric registration entry for already-admitted original-Q12 rows.

        The caller must decode/check actual PL evidence separately. This method
        never labels modeled or decoded rows as hardware evidence.
        """
        centers, accepted = np.asarray(center_q12), np.asarray(stripe_accepted)
        if (centers.shape != (480,) or centers.dtype.kind not in "iu"
                or accepted.shape != (480,) or accepted.dtype.kind != "b"
                or len(stripe_reasons) != 480 or np.any(centers < 0)
                or np.any(centers >= 752*4096)):
            raise ValueError("480 bounded original-Q12 centers/bool admission/reasons required")
        frame_id = frame_metadata.get("frame_id", frame_metadata.get("request_id"))
        timestamp = float(frame_metadata["timestamp_s"])
        angle = float(frame_metadata["angle_rad"])
        if type(frame_id) is not int or frame_id < 0 or not math.isfinite(timestamp) or not math.isfinite(angle):
            raise ValueError("finite scan frame_id/time/angle required")
        if ((self._last_id is not None and frame_id <= self._last_id)
                or (self._last_time is not None and timestamp <= self._last_time)):
            raise ValueError("frame IDs and timestamps must strictly advance")
        if frame_metadata.get("calibration_id", self.calibration_id) != self.calibration_id:
            raise ValueError("frame/calibration identity mismatch")
        rows = np.arange(480, dtype=np.int64)
        section = reconstruct_section(rows, centers, accepted, self.camera_matrix, self.distortion,
                                      self.plane, self.ray_range, self.bounds)
        camera = section["points_mm"]
        valid, geometry_reasons = strict_geometry_mask(section["valid"], section["outside_training_bounds"], camera)
        reasons = tuple(geometry_reasons[i] if accepted[i] and not valid[i] else reason
                        for i, reason in enumerate(stripe_reasons))
        axis = np.full_like(camera, np.nan)
        if valid.any():
            unrotated = unrotate_points(camera[valid], self.axis_point, self.axis_direction, angle)
            axis[valid] = (unrotated - self.axis_point) @ self.basis_camera
            self._cloud.append(_readonly(axis[valid]))
            self._revision += 1
            if self._first_section is None:
                self._first_section = _readonly((camera[valid]-self.axis_point) @ self.basis_camera)
                self._first_section_angle = angle
        self._last_id, self._last_time = frame_id, timestamp
        self._angles.append(angle)
        profile = VirtualProfile(frame_id, timestamp, angle, _readonly(rows), _readonly(centers),
                                 _readonly(accepted), _readonly(camera), _readonly(axis), _readonly(valid),
                                 reasons, dict(Counter(r for r in reasons if r is not None)),
                                 self.calibration_id, self._revision)
        return profile, self.get_cloud()

    def snapshot(self) -> CloudSnapshot:
        return CloudSnapshot(self._revision, self.get_cloud(), self.reference_basis)

    def get_cloud(self) -> np.ndarray:
        return _readonly(np.concatenate(self._cloud) if self._cloud else np.empty((0, 3), float))

    def finalize_features(self) -> list[dict[str, Any]]:
        from .features import discover_features
        result = discover_features(self.get_cloud(), calibration_id=self.calibration_id,
                                   reference_basis=self.reference_basis,
                                   scan_angles_rad=self._angles)
        for record in result:
            record["source_kind"] = "registered_observed_cloud"
        # Discovery is independent of fixture type. A full revolution can
        # contain both solids and useful stationary-view section geometry.
        # The first actually observed view is deterministic and keeps stair/
        # groove evidence without requiring a target-name-selected algorithm.
        if self._first_section is not None and len(self._angles) > 1 and np.ptp(self._angles) > 1e-8:
            sections = discover_features(self._first_section, calibration_id=self.calibration_id,
                                         reference_basis=self.reference_basis,
                                         scan_angles_rad=[0.])
            angle = float(self._first_section_angle)
            c, s = math.cos(angle), math.sin(angle)
            inverse_rotation = np.array([[c, s, 0.], [-s, c, 0.], [0., 0., 1.]])
            for record in sections:
                if record["status"] != "available":
                    continue
                record["source_kind"] = "single_observed_section"
                record["view_angle_rad"] = angle
                record["region_id"] = "view_" + record["region_id"]
                record["direction"] = (inverse_rotation @ np.asarray(record["direction"])).tolist()
                record["section_support_basis"] = "zero-angle camera/axis radial section; support indices refer to first observed profile"
                result.append(record)
            if any(r["status"] == "available" for r in result):
                result = [r for r in result if r["status"] == "available" or r["type"] != "geometry"]
        return result
