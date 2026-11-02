"""Simulation-only quality admission and bounded reacquisition.

The supervisor accepts raw pixels and reported acquisition metadata only. CAD,
renderer truth, intended perturbations and actual angles are evaluator inputs and
are deliberately absent from this API. Extra quality limits do not alter the
frozen stripe, geometry or feature algorithms.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math

import numpy as np

from .estimator import VirtualScanEstimator
from structured_light.scan3d.continuous_exposure import ExposureController
from structured_light.stripe_v1.candidates import extract_candidates


@dataclass(frozen=True)
class QualityLimits:
    max_median_width_px: float = 12.
    max_background_noise_mad: float = 8.
    max_multiple_candidate_fraction: float = .15
    min_observed_row_fill: float = .8
    min_valid_points: int = 3


def image_quality(gray, profile, stripe_config, limits=QualityLimits()):
    """Telemetry from Raw and unchanged production extraction/profile output.

    Row fill is the fraction of valid rows in their *observed* bounding interval.
    It cannot establish complete target coverage or detect an entirely missing
    surface. Intensity and width statistics include rejected candidates.
    """
    image = np.asarray(gray)
    frame = extract_candidates(image, stripe_config)
    candidates = [candidate for row in frame.rows for candidate in row.candidates]
    active = [row for row in frame.rows if row.candidates]
    widths = [candidate.width for candidate in candidates]
    valid_rows = np.flatnonzero(profile.valid_points)
    fill = len(valid_rows) / (int(np.ptp(valid_rows)) + 1) if len(valid_rows) else 0.
    # Horizontal first differences ignore the narrow stripe in the median.
    # This is image noise telemetry, not calibrated sensor read noise.
    differences = np.diff(image.astype(np.int16), axis=1)
    noise = float(np.median(np.abs(differences - np.median(differences))) / math.sqrt(2))
    multiple = sum(len(row.candidates) > 1 for row in active) / len(active) if active else 0.
    telemetry = {
        "raw_max": int(image.max()), "raw_median": float(np.median(image)),
        "candidate_peak_above_threshold_median": float(np.median([c.peak for c in candidates])) if candidates else None,
        "candidate_width_median_px": float(np.median(widths)) if widths else None,
        "candidate_width_max_px": max(widths) if widths else None,
        "saturated_pixels": int(np.count_nonzero(image == 255)),
        "saturated_candidate_samples": sum(c.saturated_count for c in candidates),
        "background_noise_difference_mad": noise,
        "active_candidate_rows": len(active), "candidate_count": len(candidates),
        "multiple_candidate_row_fraction": multiple,
        "selected_stripe_rows": int(profile.stripe_accepted.sum()),
        "valid_points": profile.valid_point_count, "observed_row_fill": fill,
        "sensor_row_fraction": profile.valid_point_count / image.shape[0],
        "rejection_counts": dict(profile.rejection_counts),
        "completeness_scope": "observed_row_envelope_only; target/surface completeness unknown",
    }
    reasons = []
    if telemetry["saturated_candidate_samples"]:
        reasons.append("saturated")
    if int(image.max()) <= stripe_config.fixed_threshold:
        reasons.append("weak")
    if widths and telemetry["candidate_width_median_px"] > limits.max_median_width_px:
        reasons.append("too_wide")
    if noise > limits.max_background_noise_mad:
        reasons.append("noisy")
    if multiple > limits.max_multiple_candidate_fraction:
        reasons.append("ambiguous")
    if profile.valid_point_count < limits.min_valid_points or fill < limits.min_observed_row_fill:
        reasons.append("incomplete")
    telemetry["reasons"] = reasons
    telemetry["quality"] = next((q for q in ("saturated", "weak", "ambiguous", "incomplete") if q in reasons),
                                "incomplete" if reasons else "good")
    telemetry["admitted"] = not reasons
    return telemetry


class BoundedScanSupervisor:
    """At most one retry per interval, within one extra-revolution allowance.

    ``interval_count`` is a predeclared angular schedule, independent of shape.
    Transport failures are terminal for that acquisition; dropped intervals can
    be requested exactly once. No interpolation or synthetic cloud filling.
    An image-quality pass does not validate angle timing or axis calibration.
    """
    def __init__(self, config, interval_count, *, limits=QualityLimits(),
                 exposure_lines=(4, 16, 128), initial_lines=16, hysteresis_frames=1):
        if type(interval_count) is not int or interval_count < 1:
            raise ValueError("positive interval_count required")
        self.config = dict(config)
        self.estimator = VirtualScanEstimator(config)
        self.interval_count = interval_count
        self.limits = limits
        self.exposure = ExposureController(line_values=tuple(exposure_lines), initial_lines=initial_lines,
                                           hysteresis_frames=hysteresis_frames)
        self.initial_lines = initial_lines
        self.trace = []
        self._attempts = {}
        self._available = set()
        self._last_id = None
        self._last_time = None
        self._last_angle = None
        self._retries_started = False

    def metadata(self, frame_id, timestamp_s, angle_rad):
        return {"frame_id": frame_id, "timestamp_s": timestamp_s, "angle_rad": angle_rad,
                "calibration_id": self.estimator.calibration_id,
                "exposure_revision": self.exposure.revision, "exposure_lines": self.exposure.lines,
                "exposure_gain": 16, "exposure_mode": "SIMULATED_AMPLITUDE_RESPONSE_ONLY"}

    @property
    def amplitude_scale(self):
        return self.exposure.lines / self.initial_lines

    def _reserve(self, interval_id, revolution):
        if type(interval_id) is not int or not 0 <= interval_id < self.interval_count:
            raise ValueError("interval outside declared schedule")
        if revolution not in (0, 1):
            raise ValueError("at most one extra revolution is allowed")
        key = (interval_id, revolution)
        if key in self._attempts:
            raise ValueError("only one acquisition per interval per revolution")
        if revolution == 0 and self._retries_started:
            raise ValueError("initial revolution already closed")
        if revolution == 1:
            if (interval_id, 0) not in self._attempts:
                raise ValueError("retry requires an initial acquisition/drop record")
            if interval_id in self._available:
                raise ValueError("available interval does not need a retry")
            self._retries_started = True
        self._attempts[key] = True

    def dropped(self, interval_id, *, revolution=0):
        self._reserve(interval_id, revolution)
        record = {"interval_id": interval_id, "revolution": revolution, "status": "dropped",
                  "admitted": False, "point_count_added": 0}
        self.trace.append(record)
        return record

    def process(self, gray, metadata, interval_id, *, revolution=0):
        self._reserve(interval_id, revolution)
        record = {"interval_id": interval_id, "revolution": revolution,
                  "metadata": dict(metadata), "admitted": False, "point_count_added": 0}
        self.trace.append(record)
        frame_id = metadata.get("frame_id")
        timestamp, angle = metadata.get("timestamp_s"), metadata.get("angle_rad")
        fault = None
        if (type(frame_id) is not int or frame_id < 0 or type(timestamp) not in (int, float)
                or type(angle) not in (int, float) or not math.isfinite(timestamp) or not math.isfinite(angle)):
            fault = "invalid_acquisition_metadata"
        elif self._last_id is not None and frame_id <= self._last_id:
            fault = "duplicate_or_backward_frame_id"
        elif self._last_time is not None and timestamp <= self._last_time:
            fault = "nonadvancing_timestamp"
        elif self._last_angle is not None and angle <= self._last_angle:
            fault = "nonadvancing_reported_angle"
        elif metadata.get("calibration_id") != self.estimator.calibration_id:
            fault = "calibration_identity_mismatch"
        elif (type(metadata.get("exposure_revision")) is not int
              or not 0 <= metadata["exposure_revision"] <= 0xFFFFFFFF
              or metadata.get("exposure_lines") != self.exposure.lines
              or metadata.get("exposure_gain") != 16
              or not self.exposure.frame_eligible(request_id=frame_id,
                                                 exposure_revision=metadata.get("exposure_revision", -1))):
            fault = "stale_or_unacknowledged_exposure"
        if fault:
            record.update(status="transport_rejected", fault=fault)
            return record
        self._last_id, self._last_time, self._last_angle = frame_id, timestamp, angle
        # Probe actual frozen image->geometry output. Only admissible original
        # Q12 rows enter the persistent estimator/cloud, never probe-only points.
        probe = VirtualScanEstimator(self.config)
        profile, _ = probe.process(gray, metadata)
        quality = image_quality(gray, profile, probe.stripe_config, self.limits)
        record.update(quality=quality, status="quality_rejected", profile=profile.summary())
        if quality["admitted"]:
            committed, _ = self.estimator.process_stripe(profile.center_q12, profile.stripe_accepted,
                                                        profile.reasons, metadata)
            self._available.add(interval_id)
            record.update(status="accepted", admitted=True, point_count_added=committed.valid_point_count)
        operation = self.exposure.propose(quality["quality"])
        if operation:
            # Simulated sensor acknowledgement, effective only for later IDs.
            self.exposure.acknowledge(operation.revision, accepted=True, effective_after_request_id=frame_id)
            record["exposure_operation"] = dict(asdict(operation), effective_after_request_id=frame_id,
                                                 response="amplitude proportional to lines; fixed gain 16",
                                                 physical_response_verified=False)
        return record

    def retry_intervals(self):
        return [i for i in range(self.interval_count)
                if (i, 0) in self._attempts and (i, 1) not in self._attempts and i not in self._available]

    def summary(self):
        return {"evidence_type": "SIMULATION_ONLY", "physical_validation": False,
                "declared_intervals": self.interval_count, "maximum_revolutions": 2,
                "maximum_attempts": 2 * self.interval_count, "attempts": len(self._attempts),
                "retry_attempts": sum(revolution == 1 for _, revolution in self._attempts),
                "available_intervals": sorted(self._available),
                "unavailable_intervals": [i for i in range(self.interval_count) if i not in self._available],
                "point_count": len(self.estimator.get_cloud()), "quality_limits": asdict(self.limits),
                "exposure_history": list(self.exposure.history),
                "undetected_limits": ["constant angle timestamp/delay bias lacks independent angle reference",
                                      "unknown rotation-axis bias lacks independent calibration validation",
                                      "observed row fill is not target surface completeness",
                                      "blur/background structure can pass image-only quality gates"],
                "metrology_status": "pending_independent_angle_axis_and_surface_validation"}
