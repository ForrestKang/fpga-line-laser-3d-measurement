"""One captured frame -> selected stripe -> camera XYZ -> object-frame XYZ.

Every stage keeps source rows, Q12 centers and rejection reasons. Points
outside the laser-plane training bounds are kept and flagged as
extrapolated. Nothing is interpolated, smoothed or filled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from sl_geometry.section import reconstruct_section

from ..stripe_v1.candidates import extract_candidates
from ..stripe_v1.selection_v2 import SelectionConfigV2
from ..stripe_v1.selection_v3 import SelectionConfigV3, select_candidates_v3
from ..stripe_v1.stripe_fast import select_stripe, supports
from ..stripe_v1.types import StripeConfig
from .turntable import TurntableModel


FRAME_WIDTH = 752
FRAME_HEIGHT = 480

# Frozen T02 stripe policy: v2 values from the confirmed request5693 setup and
# the v3 gates chosen in optimization round 1 (development data only).
DEFAULT_STRIPE_CONFIG = StripeConfig(threshold_mode="fixed", fixed_threshold=60, roi=(620, 210, 710, 430))
DEFAULT_SELECTION = SelectionConfigV3(
    selection=SelectionConfigV2(association_max_distance_px=3, association_max_row_gap=1, minimum_segment_rows=3),
    ridge_asym_max_dn=10,
    max_segment_width_span_px=12,
)


class ScanPipelineError(ValueError):
    """Raised when a frame or calibration violates the scan pipeline contract."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class ScanCalibration:
    camera_matrix: np.ndarray
    distortion: np.ndarray
    plane: np.ndarray
    ray_range_mm: tuple[float, float]
    training_bounds_mm: np.ndarray
    camera_sha256: str
    plane_sha256: str

    @classmethod
    def load(cls, camera_path: str | Path, plane_path: str | Path) -> "ScanCalibration":
        camera_path, plane_path = Path(camera_path), Path(plane_path)
        camera = json.loads(camera_path.read_text(encoding="utf-8"))
        plane = json.loads(plane_path.read_text(encoding="utf-8"))
        camera_sha = _sha256(camera_path)
        if plane.get("camera_intrinsics_sha256") not in (None, camera_sha):
            raise ScanPipelineError("laser plane was fitted with different camera intrinsics")
        if tuple(camera.get("image_size", (FRAME_WIDTH, FRAME_HEIGHT))) != (FRAME_WIDTH, FRAME_HEIGHT):
            raise ScanPipelineError("camera intrinsics are not for 752x480 frames")
        bounds = plane["training_point_bounds_mm"]
        return cls(
            np.asarray(camera["camera_matrix"], float),
            np.asarray(camera["distortion_coefficients"], float),
            np.asarray(plane["plane_coefficients"], float),
            tuple(float(v) for v in plane["training_ray_range_mm"]),
            np.asarray([bounds["minimum"], bounds["maximum"]], float),
            camera_sha,
            _sha256(plane_path),
        )


@dataclass(frozen=True)
class FrameSection:
    position_index: int
    steps: int
    angle_rad: float
    request_id: int | None
    raw_sha256: str
    rows: np.ndarray
    center_q12: np.ndarray
    stripe_accepted: np.ndarray
    reasons: tuple[str | None, ...]
    points_camera_mm: np.ndarray
    valid: np.ndarray
    outside_training: np.ndarray
    points_object_mm: np.ndarray
    rejected_rows: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    rejected_center_q12: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))

    @property
    def point_count(self) -> int:
        return int(self.valid.sum())

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for reason in self.reasons:
            if reason is not None and reason != "roi_excluded":
                counts[reason] = counts.get(reason, 0) + 1
        return {
            "position_index": self.position_index, "steps": self.steps,
            "angle_deg": float(np.degrees(self.angle_rad)), "request_id": self.request_id,
            "raw_sha256": self.raw_sha256, "stripe_rows": int(self.stripe_accepted.sum()),
            "xyz_points": self.point_count,
            "outside_training_bounds": int(np.count_nonzero(self.valid & self.outside_training)),
            "rejection_counts": counts,
        }


def as_gray(raw: bytes | bytearray | memoryview | np.ndarray) -> np.ndarray:
    if isinstance(raw, np.ndarray):
        gray = raw
    else:
        data = bytes(raw)
        if len(data) != FRAME_WIDTH * FRAME_HEIGHT:
            raise ScanPipelineError("raw frame must be exactly 752x480 gray8 bytes")
        gray = np.frombuffer(data, np.uint8).reshape(FRAME_HEIGHT, FRAME_WIDTH)
    if gray.shape != (FRAME_HEIGHT, FRAME_WIDTH) or gray.dtype != np.uint8:
        raise ScanPipelineError("gray frame must be a 752x480 uint8 array")
    return gray


def select_rows(gray: np.ndarray, stripe_config: StripeConfig, selection: SelectionConfigV3, *,
                reference: bool = False) -> tuple[np.ndarray, np.ndarray, np.ndarray, tuple]:
    """(center_q12, accepted, rejected-by-v3, reasons) for every image row.

    The vectorized path is used whenever it covers the configuration; the
    object-based reference (``reference=True``) is the specification it is tested against.
    """

    if not reference and supports(stripe_config, selection):
        rows = select_stripe(gray, stripe_config, selection)
        return rows.center_q12, rows.accepted, rows.retained & ~rows.accepted, rows.reasons
    frame = extract_candidates(gray, stripe_config)
    selected = select_candidates_v3(gray, frame, stripe_config, selection)
    accepted = np.array([row.candidate is not None and row.reason is None for row in selected.rows])
    centers = np.array([row.candidate.center_q12 if row.candidate is not None else 0
                        for row in selected.rows], np.int64)
    rejected = np.array([row.candidate is not None and row.reason is not None for row in selected.rows])
    return centers, accepted, rejected, tuple(row.reason for row in selected.rows)


def process_frame(raw: bytes | np.ndarray, *, steps: int, position_index: int,
                  calibration: ScanCalibration, turntable: TurntableModel,
                  stripe_config: StripeConfig = DEFAULT_STRIPE_CONFIG,
                  selection: SelectionConfigV3 = DEFAULT_SELECTION,
                  request_id: int | None = None, reference: bool = False) -> FrameSection:
    gray = as_gray(raw)
    centers, accepted, rejected, reasons = select_rows(gray, stripe_config, selection, reference=reference)
    rows = np.arange(gray.shape[0], dtype=np.int64)
    section = reconstruct_section(rows, centers, accepted, calibration.camera_matrix, calibration.distortion,
                                  calibration.plane, calibration.ray_range_mm, calibration.training_bounds_mm)
    valid = np.asarray(section["valid"], bool)
    points_camera = np.asarray(section["points_mm"], float)
    points_object = np.full_like(points_camera, np.nan)
    if valid.any():
        points_object[valid] = turntable.to_object_frame(points_camera[valid], steps)
    return FrameSection(
        position_index=int(position_index), steps=int(steps), angle_rad=turntable.angle_rad(steps),
        request_id=request_id, raw_sha256=hashlib.sha256(gray.tobytes()).hexdigest(),
        rows=rows, center_q12=centers, stripe_accepted=accepted, reasons=reasons,
        points_camera_mm=points_camera, valid=valid,
        outside_training=np.asarray(section["outside_training_bounds"], bool),
        points_object_mm=points_object,
        rejected_rows=rows[rejected], rejected_center_q12=centers[rejected],
    )


__all__ = [
    "DEFAULT_SELECTION", "DEFAULT_STRIPE_CONFIG", "FRAME_HEIGHT", "FRAME_WIDTH", "FrameSection",
    "ScanCalibration", "ScanPipelineError", "as_gray", "process_frame", "select_rows",
]
