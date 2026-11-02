"""Pure same-frame feature -> section -> calibrated axis cloud processor.

No hardware modules are imported. Evidence records are caller-validated
provenance declarations, not proof manufactured from an assumed axis. Static
mode produces single-camera sections. Continuous fusion additionally requires
real axis, exposure timing, and calibrated working-volume admission.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import math
import re
import threading
from typing import TYPE_CHECKING, Any

import numpy as np

from sl_geometry.geometry import unrotate_points
from sl_geometry.section import reconstruct_section

from ..stripe_v1.selection_v3 import SelectionConfigV3
from ..stripe_v1.types import StripeConfig
from .continuous_exposure import ExposureController
from .continuous_measurement import (MeasurementResult, MeasurementValidity, TruthComparison,
                                     compare_as_built, diameters_by_height)
from .continuous_pl_input import PLFrameRows, select_pl_rows, strict_geometry_mask
from .payload_digest import payload_sha256
from .continuous_quality import FrameQuality, QualityThresholds, assess_quality
from .continuous_timing import FrameMotionMetadata, TimingBinder, ValidatedTimingModel, _finite, _uint
from .pipeline import ScanCalibration, select_rows
from .turntable import TurntableModel

if TYPE_CHECKING:
    from .stream_capture import FeatureCapture


def _evidence(sha256: str | None, reference: str | None) -> None:
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
        raise ValueError("validated evidence SHA-256 required")
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("validated evidence reference required")


def _readonly(array: Any) -> np.ndarray:
    value = np.asarray(array).copy()
    value.setflags(write=False)
    return value


@dataclass(frozen=True)
class AxisCalibration:
    """Real calibrated axis plus explicit radial basis; height origin is axis point.

    ``radial_reference_camera`` specifies the zero-angle radial X direction.
    It is projected perpendicular to axis Z; Y=Z cross X makes a right-handed
    orthonormal basis. No arbitrary camera-Z height or assumed bottom is used.
    """
    model: TurntableModel
    radial_reference_camera: tuple[float, float, float]
    evidence_sha256: str
    evidence_reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.model, TurntableModel) or not self.model.calibrated:
            raise ValueError("real calibrated turntable model required")
        self.model.__post_init__()
        _evidence(self.evidence_sha256, self.evidence_reference)
        reference = np.asarray(self.radial_reference_camera, float)
        if reference.shape != (3,) or not np.isfinite(reference).all():
            raise ValueError("radial reference must be a finite 3-vector")
        axis = np.asarray(self.model.axis_direction)
        radial = reference - axis * float(reference @ axis)
        if not math.isfinite(float(np.linalg.norm(radial))) or np.linalg.norm(radial) <= 1e-9:
            raise ValueError("radial reference cannot be parallel to axis")

    @property
    def basis_camera(self) -> np.ndarray:
        z = np.asarray(self.model.axis_direction, float)
        reference = np.asarray(self.radial_reference_camera, float)
        x = reference - z * float(reference @ z)
        x /= np.linalg.norm(x)
        return np.column_stack((x, np.cross(z, x), z))

    @property
    def reference_basis(self) -> str:
        return (f"axis-Z; height origin axis_point_mm; axis={self.model.fingerprint}; "
                f"radial_reference_camera={tuple(self.radial_reference_camera)}; evidence={self.evidence_sha256}")


@dataclass(frozen=True)
class WorkingVolume:
    radius_mm: float
    height_range_mm: tuple[float, float]
    validated: bool
    evidence_sha256: str | None = None
    evidence_reference: str | None = None

    def __post_init__(self) -> None:
        _finite("radius_mm", self.radius_mm)
        if self.radius_mm <= 0 or len(self.height_range_mm) != 2:
            raise ValueError("working volume requires positive radius and two height limits")
        lo, hi = self.height_range_mm
        _finite("height lower", lo)
        _finite("height upper", hi)
        if lo >= hi or type(self.validated) is not bool:
            raise ValueError("invalid working volume or validation flag")
        if self.validated:
            _evidence(self.evidence_sha256, self.evidence_reference)

    def contains(self, points_axis_mm: np.ndarray) -> np.ndarray:
        points = np.asarray(points_axis_mm)
        return (np.isfinite(points).all(axis=1)
                & (np.linalg.norm(points[:, :2], axis=1) <= self.radius_mm)
                & (points[:, 2] >= self.height_range_mm[0])
                & (points[:, 2] <= self.height_range_mm[1]))


@dataclass(frozen=True)
class CloudSnapshot:
    revision: int
    points_axis_mm: np.ndarray
    reference_basis: str


@dataclass(frozen=True)
class ProcessedProfile:
    request_id: int
    capture_epoch: int
    mode: str
    rows: np.ndarray
    center_q12: np.ndarray
    stripe_accepted: np.ndarray
    points_camera_mm: np.ndarray
    points_axis_mm: np.ndarray
    valid_points: np.ndarray
    reasons: tuple[str | None, ...]
    rejection_counts: dict[str, int]
    quality: FrameQuality
    angle_rad: float | None
    angle_source: str
    exposure_mid_ticks: float | None
    registered: bool
    profile_valid: bool
    valid: bool
    fused: bool
    cloud_revision: int
    pl_primary_rows: int
    raw_fallback_rows: int
    pl_mismatch_rows: tuple[int, ...]
    registration_reason: str | None

    @property
    def valid_point_count(self) -> int:
        return int(self.valid_points.sum())

    def summary(self) -> dict[str, Any]:
        """Small scheduler/display record; large arrays remain in this profile."""
        return {"request_id": self.request_id, "capture_epoch": self.capture_epoch, "mode": self.mode,
                "angle_rad": self.angle_rad, "angle_source": self.angle_source,
                "exposure_mid_ticks": self.exposure_mid_ticks, "valid": self.valid,
                "profile_valid": self.profile_valid, "registered": self.registered, "fused": self.fused,
                "quality": self.quality.status, "quality_metrics": asdict(self.quality),
                "valid_point_count": self.valid_point_count, "cloud_revision": self.cloud_revision,
                "pl_primary_rows": self.pl_primary_rows, "raw_fallback_rows": self.raw_fallback_rows,
                "pl_mismatch_rows": self.pl_mismatch_rows, "rejection_counts": dict(self.rejection_counts),
                "registration_reason": self.registration_reason, "physical_accuracy_verified": False}


class ContinuousProcessor:
    """One immutable geometry/configuration/capture epoch; serialized processing.

    The caller supplies confirmed sensor line/revision state and per-frame
    feature configuration provenance (keyword or motion-metadata field).
    Exposure changes require an explicit drained, idle revision transition.
    Capture epochs/configurations need a new processor; nothing auto-rearms.
    """
    def __init__(self, *, calibration: ScanCalibration, stripe_config: StripeConfig,
                 selection: SelectionConfigV3, mode: str, exposure_lines: int,
                 exposure_revision: int, axis: AxisCalibration | None = None,
                 timing_binder: TimingBinder | None = None, volume: WorkingVolume | None = None,
                 quality_thresholds: QualityThresholds = QualityThresholds(), min_valid_rows: int = 3,
                 max_cloud_points: int = 250_000, exposure_controller: ExposureController | None = None) -> None:
        if mode not in {"static", "continuous"}:
            raise ValueError("mode must explicitly be static or continuous")
        if type(exposure_lines) is not int or not 1 <= exposure_lines <= 32765:
            raise ValueError("sensor exposure_lines must be in 1..32765")
        _uint("exposure_revision", exposure_revision)
        for name, value in (("min_valid_rows", min_valid_rows), ("max_cloud_points", max_cloud_points)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be positive")
        stripe_config.__post_init__()
        selection.__post_init__()
        quality_thresholds.__post_init__()
        # Own calibration arrays, so an external mutable array cannot silently
        # change a running processor's geometry/configuration.
        self.calibration = replace(calibration, camera_matrix=_readonly(calibration.camera_matrix),
                                   distortion=_readonly(calibration.distortion), plane=_readonly(calibration.plane),
                                   training_bounds_mm=_readonly(calibration.training_bounds_mm))
        _evidence(calibration.camera_sha256, "camera calibration")
        _evidence(calibration.plane_sha256, "laser-plane calibration")
        reconstruct_section(np.zeros(0, int), np.zeros(0, int), np.zeros(0, bool),
                            self.calibration.camera_matrix, self.calibration.distortion, self.calibration.plane,
                            self.calibration.ray_range_mm, self.calibration.training_bounds_mm)
        if timing_binder is not None and timing_binder.exposure_revision != exposure_revision:
            raise ValueError("processor/timing exposure revision mismatch")
        if timing_binder is not None and timing_binder.model is not None:
            timing_binder.model.__post_init__()
            if timing_binder.model.exposure_lines != exposure_lines:
                raise ValueError("processor/timing exposure lines mismatch")
        self.stripe_config, self.selection, self.mode = stripe_config, selection, mode
        self.exposure_lines, self.exposure_revision = exposure_lines, exposure_revision
        self.axis, self.timing_binder, self.volume = axis, timing_binder, volume
        self.quality_thresholds = quality_thresholds
        self.min_valid_rows, self.max_cloud_points = min_valid_rows, max_cloud_points
        self.exposure_controller = exposure_controller
        self._lock = threading.RLock()
        self._capture_epoch: int | None = None
        self._last_request: int | None = None
        self._cloud: list[np.ndarray] = []
        self._point_count = 0
        self._revision = 0
        self._motion_offset_pulses = 0
        self._axis_reference_basis = axis.reference_basis if axis is not None else None
        self._last_fv_ticks: int | None = None
        self._last_admitted_event_count: int | None = None
        self._last_command_count: int | None = None
        self._motion_identity = ((timing_binder.motor_epoch, timing_binder.motor_accepted_id)
                                 if timing_binder is not None else None)

    def _check_axis(self) -> None:
        basis = self.axis.reference_basis if self.axis is not None else None
        if basis != self._axis_reference_basis:
            raise ValueError('axis changed; construct a new processor for a different reference basis')

    def begin_exposure_revision(self, exposure_lines: int, exposure_revision: int,
                                timing_model: ValidatedTimingModel | None = None, *,
                                capture_drained: bool, processing_drained: bool,
                                camera_idle: bool) -> None:
        """Change only exposure/timing after explicit queue drain and sensor idle.

        The caller verifies the SCCB transaction/effect model separately. This
        method neither writes the sensor nor establishes its physical effect.
        Global CSM time/event and same-task command watermarks survive the
        timing binder replacement, as do cloud, axis and cumulative motor origin.
        """
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('profile processing is still active')
        try:
            if not (capture_drained is True and processing_drained is True and camera_idle is True):
                raise ValueError('capture/processing must explicitly drain and camera must be idle')
            if type(exposure_lines) is not int or not 1 <= exposure_lines <= 32765:
                raise ValueError('sensor exposure_lines must be in 1..32765')
            _uint('exposure_revision', exposure_revision)
            if exposure_revision <= self.exposure_revision:
                raise ValueError('exposure revision must advance')
            self._check_axis()
            old = self.timing_binder
            if self.mode == 'continuous' and (old is None or timing_model is None):
                raise ValueError('continuous exposure change requires a target validated timing model and binder')
            if timing_model is not None:
                if not isinstance(timing_model, ValidatedTimingModel):
                    raise ValueError('ValidatedTimingModel required')
                timing_model.__post_init__()
                if (timing_model.exposure_lines != exposure_lines
                        or timing_model.exposure_revision != exposure_revision):
                    raise ValueError('target timing model must match exposure lines/revision')
                if old is None:
                    raise ValueError('a timing model requires an existing timing binder identity')
                if old.model is not None and old.model.pulses_per_revolution != timing_model.pulses_per_revolution:
                    raise ValueError('exposure change cannot change the motor pulse configuration')
            replacement = (TimingBinder(capture_epoch=old.capture_epoch, motor_epoch=old.motor_epoch,
                           motor_accepted_id=old.motor_accepted_id, exposure_revision=exposure_revision,
                           model=timing_model) if old is not None else None)
            self.timing_binder = replacement
            self.exposure_lines, self.exposure_revision = exposure_lines, exposure_revision
        finally:
            self._lock.release()

    def begin_motion_epoch(self, *, capture_epoch: int, motor_epoch: int, motor_accepted_id: int,
                           previous_receipt: dict, capture_joined: bool, processing_drained: bool,
                           guard_was_revoked: bool) -> None:
        """Explicit second-turn transition after a completed/drained first turn.

        Only the command origin changes. Calibration, exposure, capture epoch
        and timing evidence remain identical. The accumulated measured cloud
        is preserved. No first-frame auto-detection or automatic reset occurs.
        """
        if not self._lock.acquire(blocking=False):raise RuntimeError('profile processing is still active')
        try:
            self._check_axis()
            _uint('capture_epoch', capture_epoch, 16)
            _uint('motor_epoch', motor_epoch)
            _uint('motor_accepted_id', motor_accepted_id)
            old=self.timing_binder
            if old is None or old.model is None or self._last_request is None:
                raise ValueError('a previously bound, processed motion epoch is required')
            if self._motion_offset_pulses!=0:raise ValueError('one additional turn is the finite limit')
            if not (capture_joined is True and processing_drained is True and guard_was_revoked is True):
                raise ValueError('previous capture/processing must drain and its guard must be revoked')
            if (previous_receipt.get('done') is not True or previous_receipt.get('emitted')!=3200
                or previous_receipt.get('accepted_id')!=old.motor_accepted_id
                or previous_receipt.get('epoch')!=old.motor_epoch or previous_receipt.get('error')!=0
                or previous_receipt.get('busy') is not False or previous_receipt.get('fault') is not False):
                raise ValueError('exact previous clean 3200-command receipt required')
            if capture_epoch!=self._capture_epoch or motor_accepted_id<=old.motor_accepted_id or motor_epoch<old.motor_epoch:
                raise ValueError('new motor identity must advance within the same capture epoch')
            replacement=TimingBinder(capture_epoch=capture_epoch,motor_epoch=motor_epoch,
                motor_accepted_id=motor_accepted_id,exposure_revision=self.exposure_revision,model=old.model)
            self.timing_binder=replacement;self._motion_offset_pulses=3200
            self._motion_identity=(motor_epoch,motor_accepted_id)
            self._last_command_count=None
        finally:self._lock.release()

    @property
    def measurement_validity(self) -> MeasurementValidity:
        return MeasurementValidity(
            axis_valid=self.mode == "continuous" and self.axis is not None,
            timing_valid=(self.mode == "continuous" and self.timing_binder is not None
                          and self.timing_binder.model is not None),
            volume_valid=self.volume is not None and self.volume.validated,
            reference_basis=self.axis.reference_basis if self.axis is not None else "unregistered camera section")

    def _decode_compact_capture(self, capture):
        from .stream_compact import decode_slc1
        diag = capture.diagnostics
        return decode_slc1(capture.profile_payload, frame_width=diag.width, frame_height=diag.height,
                           request_id=capture.request_id, capture_epoch=capture.epoch)

    def _select_compact_capture(self, packet, gray):
        from .feature_candidates import select_compact_candidates
        return select_compact_candidates(packet, gray, self.stripe_config, self.selection)

    def _reconstruct_section(self, *args):
        return reconstruct_section(*args)

    def _admit(self, capture: FeatureCapture, feature_config_fingerprint: str | None
               ) -> tuple[np.ndarray, FrameMotionMetadata | None, bool, Any]:
        self._check_axis()
        _uint("request_id", capture.request_id)
        _uint("capture epoch", capture.epoch, 16)
        if self._capture_epoch is not None and capture.epoch != self._capture_epoch:
            raise ValueError("capture epoch changed; construct a new processor")
        if self._last_request is not None and capture.request_id <= self._last_request:
            raise ValueError("capture request repeated or out of order")
        diag, profile = capture.diagnostics, capture.profile
        packet = None
        compact = getattr(capture,'compact_packet',None)
        pmc2=getattr(capture,'pmc2_payload',b'')
        if pmc2:
            if (getattr(self,'supports_pmc2',False) is not True or type(pmc2) is not bytes
                    or pmc2!=capture.profile_payload or profile is not None or compact is not None
                    or getattr(capture,'feature_packet',None) is not None or diag.rows):
                raise ValueError('PMC2 requires exclusive multi-candidate same-capture evidence')
        alternate=compact is not None or bool(pmc2)
        if compact is not None:
            packet = self._decode_compact_capture(capture)
            if compact != packet:
                raise ValueError('mixed SLC1 capture evidence')
            if packet.configuration.roi != (self.stripe_config.roi or (0,0,diag.width,diag.height)):
                raise ValueError('SLC1 quality ROI differs; use full-feature mode for this policy')
        if getattr(capture, 'feature_packet', None) is not None:
            from .stream_features import decode_slf1
            packet = decode_slf1(capture.profile_payload, frame_width=diag.width, frame_height=diag.height,
                                 request_id=capture.request_id, capture_epoch=capture.epoch)
            if (capture.feature_packet != packet or packet.profile != profile or packet.rows != diag.rows):
                raise ValueError('mixed or mutated SLF1 wire/capture evidence')
        if (diag.request_id != capture.request_id or diag.epoch != capture.epoch
                or (not alternate and (profile.profile_id != capture.request_id or profile.format_version != 2))):
            raise ValueError("mixed same-frame Profile/diagnostic/capture identity")
        if (not diag.ready or diag.error or diag.width <= 0 or diag.height <= 0
                or (compact is not None and packet.header[9])
                or (not alternate and (profile.error_flags
                    or (profile.frame_width,profile.frame_height) != (diag.width,diag.height)
                    or len(diag.rows) != diag.height)) or diag.row_count != diag.height):
            raise ValueError("incomplete/invalid feature capture or dimensions")
        raw = bytes(capture.raw)
        if len(raw) != diag.width * diag.height or payload_sha256(raw) != capture.raw_sha256:
            raise ValueError("raw frame length/hash does not match capture")
        for i, row in enumerate(diag.rows):
            if row.row != i or row.profile_id != capture.request_id or type(row.valid) is not bool:
                raise ValueError("mixed/reordered diagnostic row identity")
            for name in ("xc_fixed", "s0", "s1", "peak", "width", "candidate_count", "flags", "quality"):
                _uint(f"row {name}", getattr(row, name))
            if row.peak > 255 or row.quality > 255 or row.width > diag.width:
                raise ValueError("invalid PL width/intensity feature")
        if not alternate:
            profile.__post_init__()
        present = set()
        for point in (() if alternate else profile.points):
            row = diag.rows[point.row]
            if point.valid != row.valid or (point.valid and point.xc_fixed != row.xc_fixed):
                raise ValueError("Profile and diagnostic row differ")
            present.add(point.row)
        if not alternate and any(row.valid and row.row not in present for row in diag.rows):
            raise ValueError("valid PL row missing from same-frame Profile")
        meta = capture.motion_metadata
        exposure_eligible = True
        if meta is not None:
            # Canonical keys come directly from ExposureCoordinator. Aliases
            # admit older integration code but cannot override conflicting flags.
            for keys, expected in ((('effect_known', 'exposure_effect_known'), True),
                                   (('measurement_eligible',), True),
                                   (('transition', 'exposure_transition'), False)):
                flags = [meta[key] for key in keys if key in meta]
                if any(type(flag) is not bool for flag in flags):
                    raise ValueError('exposure metadata flags must be bool')
                if flags and any(flag != flags[0] for flag in flags):
                    raise ValueError('conflicting exposure metadata aliases')
                if flags and flags[0] != expected:
                    exposure_eligible = False
        declared_config = meta.get("stripe_config_fingerprint") if meta is not None else None
        if feature_config_fingerprint is None:
            feature_config_fingerprint = declared_config
        if feature_config_fingerprint != self.stripe_config.fingerprint or declared_config not in (None, feature_config_fingerprint):
            raise ValueError("missing or mixed feature configuration fingerprint")
        metadata = None
        if meta is not None:
            if meta.get("request_id") != capture.request_id or meta.get("capture_epoch") != capture.epoch:
                raise ValueError("mixed CSM1 capture identity")
            values = {f.name: meta[f.name] for f in fields(FrameMotionMetadata) if f.name in meta}
            for key, value in (("exposure_lines", self.exposure_lines), ("exposure_revision", self.exposure_revision)):
                if values.get(key, value) != value:
                    raise ValueError("stale exposure metadata")
                values[key] = value
            try:
                metadata = FrameMotionMetadata(**values)
            except TypeError as exc:
                raise ValueError("incomplete CSM1 frame metadata") from exc
            if ((self._last_fv_ticks is not None and metadata.fv_ticks <= self._last_fv_ticks)
                    or (self._last_admitted_event_count is not None
                        and metadata.admitted_event_count <= self._last_admitted_event_count)):
                raise ValueError('CSM1 FV time and admitted event count must advance globally')
            if self._motion_identity is not None and (metadata.motor_epoch, metadata.motor_accepted_id) != self._motion_identity:
                raise ValueError('mixed or stale motor_accepted_id/motor_epoch; explicit begin_motion_epoch required')
            if self._last_command_count is not None and metadata.emitted_command_count < self._last_command_count:
                raise ValueError('CSM1 command count regressed within the same motor task')
        return np.frombuffer(raw, np.uint8).reshape(diag.height, diag.width), metadata, exposure_eligible, packet

    def process(self, capture: FeatureCapture, *, feature_config_fingerprint: str | None = None) -> ProcessedProfile:
        with self._lock:
            gray, metadata, metadata_exposure_eligible, packet = self._admit(capture, feature_config_fingerprint)
            n = gray.shape[0]
            diagnostic = capture.diagnostics.rows
            compact = getattr(capture,'compact_packet',None)
            if compact is not None:
                (centers,accepted,_,reasons),primary_rows = self._select_compact_capture(packet,gray)
                primary = int(primary_rows.sum())
                fallback = sum(r != 'roi_excluded' for r in reasons)-primary
                mismatches = ()
            elif packet is not None:
                from .feature_candidates import select_feature_candidates
                (centers, accepted, _, reasons), primary_rows = select_feature_candidates(
                    packet, gray, self.stripe_config, self.selection)
                primary = int(primary_rows.sum())
                fallback = sum(r != 'roi_excluded' for r in reasons) - primary
                mismatches = ()
            elif metadata is not None:
                pl = PLFrameRows(metadata, np.array([r.xc_fixed for r in diagnostic], np.int64),
                                 np.array([r.valid and r.flags == 0 for r in diagnostic], bool),
                                 np.array([r.candidate_count for r in diagnostic], np.int64), self.stripe_config.fingerprint)
                selected = select_pl_rows(pl, raw=gray, raw_metadata=metadata,
                                          stripe_config=self.stripe_config, selection=self.selection)
                centers, accepted, reasons = selected.center_q12, selected.accepted, selected.reasons
                primary, fallback, mismatches = int(selected.pl_primary.sum()), int(selected.raw_fallback.sum()), selected.mismatch_rows
            else:
                centers, accepted, _, reasons = select_rows(gray, self.stripe_config, self.selection, reference=False)
                primary = 0
                fallback = sum(r != "roi_excluded" for r in reasons)
                mismatches = ()
            rows = np.arange(n, dtype=np.int64)
            c = self.calibration
            section = self._reconstruct_section(rows, centers, accepted, c.camera_matrix, c.distortion, c.plane,
                                          c.ray_range_mm, c.training_bounds_mm)
            camera = np.asarray(section["points_mm"], float)
            valid, _ = strict_geometry_mask(section["valid"], section["outside_training_bounds"], camera)
            final_reasons = list(reasons)
            for index in np.flatnonzero(accepted):
                if not section["valid"][index]:
                    final_reasons[index] = section["reasons"][index]
                elif section["outside_training_bounds"][index]:
                    final_reasons[index] = "outside_training_bounds"
                elif not valid[index]:
                    final_reasons[index] = "nonfinite_geometry"
            x0, y0, x1, y1 = self.stripe_config.roi or (0, 0, gray.shape[1], n)
            if x1 > gray.shape[1] or y1 > n:
                raise ValueError("stripe ROI outside captured frame")
            roi = gray[y0:y1, x0:x1]
            counts = Counter(r for r in final_reasons if r is not None and r != "roi_excluded")
            if compact is not None:
                from .stream_compact import compact_geometry_quality
                quality = compact_geometry_quality(packet,accepted[y0:y1],rejection_counts=counts,
                                                   thresholds=self.quality_thresholds)
            else:
                quality = assess_quality(roi, row_valid=accepted[y0:y1],
                                         widths_px=[r.width for r in diagnostic[y0:y1]],
                                         intensities=np.max(roi, axis=1),
                                         candidate_counts=[r.candidate_count for r in diagnostic[y0:y1]],
                                         rejection_counts=counts, thresholds=self.quality_thresholds)
            profile_valid = quality.status == "good" and int(valid.sum()) >= self.min_valid_rows
            exposure_eligible = metadata_exposure_eligible and (self.exposure_controller is None
                                 or self.exposure_controller.frame_eligible(request_id=capture.request_id,
                                                                            exposure_revision=self.exposure_revision))
            # Capacity checks precede the stateful timing bind; never lose a
            # measurement frame or corrupt a previous cloud on backpressure.
            if (self.mode == "continuous" and self.measurement_validity.valid and profile_valid
                    and exposure_eligible and self._point_count + int(valid.sum()) > self.max_cloud_points):
                raise BufferError("fused point cloud capacity exceeded; scan must stop")
            angle = midpoint = None
            registered = False
            angle_source = "unregistered"
            registration_reason = "static_mode" if self.mode == "static" else "axis_or_timing_unvalidated"
            if self.mode == "continuous" and metadata is not None and self.timing_binder is not None:
                binding = self.timing_binder.bind(metadata)
                if binding.registered and self.axis is not None and exposure_eligible:
                    # Timing correction is in fractional pulse units. Use the
                    # measured axis angle scale, not nominal integer steps.
                    pulses = (self._motion_offset_pulses + metadata.emitted_command_count
                              + self.timing_binder.model.command_sample_to_midpoint_pulses)
                    angle = pulses * self.axis.model.rad_per_step
                    midpoint = binding.exposure_mid_ticks
                    registered = True
                    angle_source = "calibrated_command_estimate"
                    registration_reason = None
            if not exposure_eligible:
                registration_reason = "exposure_transition_or_failure"
            axis_points = np.full_like(camera, np.nan)
            if registered and valid.any():
                unrotated = unrotate_points(camera[valid], self.axis.model.axis_point_mm,
                                            self.axis.model.axis_direction, angle)
                axis_points[valid] = (unrotated - np.asarray(self.axis.model.axis_point_mm)) @ self.axis.basis_camera
                if self.volume is not None and self.volume.validated:
                    outside = valid & ~self.volume.contains(axis_points)
                    valid[outside] = False
                    for index in np.flatnonzero(outside):
                        final_reasons[index] = "outside_working_volume"
            profile_valid = quality.status == "good" and int(valid.sum()) >= self.min_valid_rows
            fused = bool(registered and self.measurement_validity.valid and profile_valid and exposure_eligible)
            if fused:
                self._cloud.append(_readonly(axis_points[valid]))
                self._point_count += int(valid.sum())
                self._revision += 1
            result_valid = bool(profile_valid and exposure_eligible and (self.mode == "static" or fused))
            self._capture_epoch, self._last_request = capture.epoch, capture.request_id
            if metadata is not None:
                self._last_fv_ticks = metadata.fv_ticks
                self._last_admitted_event_count = metadata.admitted_event_count
                self._last_command_count = metadata.emitted_command_count
                self._motion_identity = (metadata.motor_epoch, metadata.motor_accepted_id)
            counts = dict(Counter(r for r in final_reasons if r is not None and r != "roi_excluded"))
            return ProcessedProfile(capture.request_id, capture.epoch, self.mode, _readonly(rows), _readonly(centers),
                                    _readonly(accepted), _readonly(camera), _readonly(axis_points), _readonly(valid),
                                    tuple(final_reasons), counts, quality, angle, angle_source, midpoint, registered,
                                    bool(profile_valid), result_valid, fused, self._revision, primary, fallback,
                                    mismatches, registration_reason)

    def __call__(self, capture: FeatureCapture) -> dict[str, Any]:
        return self.process(capture).summary()

    def snapshot(self) -> CloudSnapshot:
        with self._lock:
            cloud = np.concatenate(self._cloud) if self._cloud else np.empty((0, 3), float)
            return CloudSnapshot(self._revision, _readonly(cloud), self.measurement_validity.reference_basis)

    def measure_diameters(self, *, height_bins_mm: tuple[tuple[float, float], ...],
                          **fit_options: Any) -> tuple[MeasurementResult, ...]:
        return diameters_by_height(self.snapshot().points_axis_mm, height_bins_mm=height_bins_mm,
                                   validity=self.measurement_validity, **fit_options)

    def compare_diameter(self, result: MeasurementResult, *, truth_mm: float | None,
                         tolerance_mm: float | None, truth_reference: str | None,
                         truth_source: str = "as_built") -> TruthComparison:
        return compare_as_built(result, truth_mm=truth_mm, tolerance_mm=tolerance_mm,
                                truth_reference=truth_reference, truth_source=truth_source,
                                validity=self.measurement_validity)
