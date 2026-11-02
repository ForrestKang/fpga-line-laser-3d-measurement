"""Offline binding of CSM1 frame metadata; FV is not an exposure trigger.

CSM1 snapshots the emitted command count and admitted-FV event in the 100 MHz
domain. This proves neither shaft position nor the sensor exposure instant.
Only a separately validated, revision-specific timing model may register it.
The timing evidence must cover the particular sensor and motion configuration;
the hash/reference are provenance, not automatic verification of that evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re


def _uint(name: str, value: int, bits: int = 32) -> int:
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ValueError(f"{name} must be uint{bits}")
    return value


def _finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


@dataclass(frozen=True)
class FrameMotionMetadata:
    request_id: int
    capture_epoch: int
    admitted_event_count: int
    fv_ticks: int
    motor_epoch: int
    motor_accepted_id: int
    emitted_command_count: int
    exposure_revision: int
    exposure_lines: int
    encoder_count: int | None = None
    clock_hz: int = 100_000_000

    def __post_init__(self) -> None:
        _uint("capture_epoch", self.capture_epoch, 16)
        _uint("fv_ticks", self.fv_ticks, 64)
        for name in ("request_id", "admitted_event_count", "motor_epoch", "motor_accepted_id",
                     "emitted_command_count", "exposure_revision"):
            _uint(name, getattr(self, name))
        if type(self.exposure_lines) is not int or not 1 <= self.exposure_lines <= 32765:
            raise ValueError("exposure_lines must be a positive sensor register line count")
        if self.encoder_count is not None:
            if type(self.encoder_count) is not int or not -(1<<63) <= self.encoder_count < (1<<63):
                raise ValueError("encoder_count must be signed int64 feedback")
        if type(self.clock_hz) is not int or self.clock_hz != 100_000_000:
            raise ValueError("CSM1 FV ticks require a 100 MHz clock")


@dataclass(frozen=True)
class ValidatedTimingModel:
    evidence_sha256: str
    evidence_reference: str
    validated: bool
    exposure_revision: int
    exposure_lines: int
    fv_to_exposure_start_ticks: float
    line_period_ticks: float
    command_sample_to_midpoint_pulses: float
    pulses_per_revolution: int

    def __post_init__(self) -> None:
        if self.validated is not True:
            raise ValueError("timing model needs explicit validation")
        if not isinstance(self.evidence_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", self.evidence_sha256):
            raise ValueError("timing evidence needs SHA-256")
        if not isinstance(self.evidence_reference, str) or not self.evidence_reference.strip():
            raise ValueError("timing evidence reference is required")
        _uint("exposure_revision", self.exposure_revision)
        if type(self.exposure_lines) is not int or not 1 <= self.exposure_lines <= 32765:
            raise ValueError("invalid exposure line count")
        for name in ("fv_to_exposure_start_ticks", "line_period_ticks", "command_sample_to_midpoint_pulses"):
            _finite(name, getattr(self, name))
        if self.line_period_ticks <= 0:
            raise ValueError("line period must be positive")
        if type(self.pulses_per_revolution) is not int or self.pulses_per_revolution <= 0:
            raise ValueError("pulses per revolution must be positive")


@dataclass(frozen=True)
class RegisteredFrame:
    metadata: FrameMotionMetadata | None
    registered: bool
    angle_rad: float | None
    exposure_mid_ticks: float | None
    angle_source: str
    reason: str | None
    encoder_count: int | None = None
    timing_evidence_sha256: str | None = None


class TimingBinder:
    """One capture/motion/exposure epoch. Counter wrap requires a new binder.

    Validation is transactional: rejected input never advances ordering state.
    ``registered`` means timing-bound command estimate, not an encoder reading
    or permission to issue a metric PASS; axis/volume validity is separate.
    """

    def __init__(self, *, capture_epoch: int, motor_epoch: int, motor_accepted_id: int,
                 exposure_revision: int, model: ValidatedTimingModel | None = None) -> None:
        _uint("capture_epoch", capture_epoch, 16)
        for name, value in (("motor_epoch", motor_epoch), ("motor_accepted_id", motor_accepted_id),
                            ("exposure_revision", exposure_revision)):
            _uint(name, value)
        if model is not None and model.exposure_revision != exposure_revision:
            raise ValueError("model exposure revision mismatch")
        self.capture_epoch = capture_epoch
        self.motor_epoch = motor_epoch
        self.motor_accepted_id = motor_accepted_id
        self.exposure_revision = exposure_revision
        self.model = model
        self._previous: FrameMotionMetadata | None = None

    def bind(self, metadata: FrameMotionMetadata) -> RegisteredFrame:
        if not isinstance(metadata, FrameMotionMetadata):
            raise ValueError("CSM1 frame metadata required")
        metadata.__post_init__()
        for name in ("capture_epoch", "motor_epoch", "motor_accepted_id", "exposure_revision"):
            if getattr(metadata, name) != getattr(self, name):
                raise ValueError(f"mixed or stale {name}")
        if self.model is not None and metadata.exposure_lines != self.model.exposure_lines:
            raise ValueError("stale exposure line count")
        prev = self._previous
        if prev is not None and (metadata.request_id <= prev.request_id
                                 or metadata.fv_ticks <= prev.fv_ticks
                                 or metadata.admitted_event_count <= prev.admitted_event_count
                                 or metadata.emitted_command_count < prev.emitted_command_count):
            raise ValueError("frame identity, time, event or command count out of order")
        if self.model is None:
            result = RegisteredFrame(metadata, False, None, None, "unregistered", "timing_unvalidated",
                                     metadata.encoder_count)
        else:
            model = self.model
            midpoint = (metadata.fv_ticks + model.fv_to_exposure_start_ticks
                        + metadata.exposure_lines * model.line_period_ticks / 2)
            count = metadata.emitted_command_count + model.command_sample_to_midpoint_pulses
            if not math.isfinite(midpoint) or midpoint < 0 or not math.isfinite(count) or count < 0:
                raise ValueError("timing model produces invalid midpoint or command position")
            result = RegisteredFrame(metadata, True, count / model.pulses_per_revolution * math.tau,
                                     midpoint, "command_estimate", None, metadata.encoder_count,
                                     model.evidence_sha256)
        self._previous = metadata
        return result


def legacy_preview(*, assumed_angle_rad: float) -> RegisteredFrame:
    """Explicitly labelled preview only; legacy packets cannot register timing."""
    return RegisteredFrame(None, False, _finite("assumed_angle_rad", assumed_angle_rad), None,
                           "assumed_preview", "legacy_metadata_missing")
