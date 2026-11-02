"""R19b vectorized numeric path; same profile contract and admission checks."""
import math
from collections import Counter
import numpy as np
from .estimator import VirtualProfile,_readonly,unrotate_points,strict_geometry_mask
from .estimator_r19 import PipelineEstimator
from sl_geometry.section_fast_r19 import reconstruct_section_fast
from typing import Sequence,Any

class FastPipelineEstimator(PipelineEstimator):
    def process_stripe_only(self,center_q12,stripe_accepted,stripe_reasons,metadata):
        self._defer_snapshot=True
        try:
            profile,_=self.process_stripe(center_q12,stripe_accepted,stripe_reasons,metadata)
            return profile
        finally:self._defer_snapshot=False

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
        section = reconstruct_section_fast(rows, centers, accepted, self.camera_matrix, self.distortion,
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

