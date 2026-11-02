"""Value types for the Stage B motion/capture host.

The values in this module are deliberately independent of PYNQ.  They are
also used by the offline runner, so an import on a development workstation
does not load an overlay or touch a board.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class MotionPhase(str, Enum):
    """The only phases a motion task may publish."""

    READY = "READY"
    MOVING = "MOVING"
    SETTLING = "SETTLING"
    CAPTURING = "CAPTURING"
    COMPARING = "COMPARING"
    SAVING = "SAVING"
    DISPLAYING = "DISPLAYING"


class CarrierMode(str, Enum):
    DIRECT_WIRE = "direct_wire"
    EXPANSION_BOARD = "expansion_board"

    @property
    def contract_value(self) -> int:
        return 0 if self is CarrierMode.DIRECT_WIRE else 1


@dataclass(frozen=True, slots=True)
class MotionConfig:
    """Bounded BSG1/MFP1 defaults.

    ``pulses_per_position`` is kept explicit because there is no calibrated
    angle in Stage B.  Ten positions by 320 pulses is a finite synthetic
    sequence; it is not a claim about a mechanical step angle.
    """

    positions: int = 10
    pulses_per_position: int = 320
    direction: int = 0
    period_cycles: int = 375_000
    high_cycles: int = 187_500
    dir_setup_cycles: int = 10_000_000
    enable_setup_cycles: int = 10_000_000
    motor_lease_cycles: int = 100_000_000
    motor_timeout_cycles: int = 1_800_000_000
    motor_timeout_s: float = 18.0
    guard_lease_cycles: int = 10_000_000
    guard_timeout_ms: int = 120_000
    host_poll_max_s: float = 0.025
    cleanup_timeout_s: float = 2.0
    rpm: float = 5.0
    capture_timeout_s: float = 2.0
    capture_phase_timeout_s: float = 6.0

    def __post_init__(self) -> None:
        int_fields = (
            "positions", "pulses_per_position", "direction", "period_cycles",
            "high_cycles", "dir_setup_cycles", "enable_setup_cycles",
            "motor_lease_cycles", "motor_timeout_cycles", "guard_lease_cycles",
            "guard_timeout_ms",
        )
        for name in int_fields:
            value = getattr(self, name)
            minimum = 0 if name == "direction" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if self.direction not in (0, 1):
            raise ValueError("direction must be 0 or 1")
        if isinstance(self.rpm, bool) or not isinstance(self.rpm, (int, float)) or self.rpm <= 0:
            raise ValueError("rpm must be positive")
        if self.high_cycles >= self.period_cycles:
            raise ValueError("high_cycles must be smaller than period_cycles")
        if self.pulses_per_position > 100_000:
            raise ValueError("pulses_per_position exceeds MFP1 maximum")
        if not 1_000_000 <= self.guard_lease_cycles <= 100_000_000:
            raise ValueError("guard_lease_cycles must be in [1000000,100000000]")
        if not 1_000 <= self.guard_timeout_ms <= 300_000:
            raise ValueError("guard_timeout_ms must be in [1000,300000]")
        if not 0 < self.host_poll_max_s <= 0.025:
            raise ValueError("host_poll_max_s must be in (0,0.025]")
        timeouts = (
            self.motor_timeout_s,
            self.cleanup_timeout_s,
            self.capture_timeout_s,
            self.capture_phase_timeout_s,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
            for value in timeouts
        ):
            raise ValueError("timeouts must be positive")

    @property
    def count(self) -> int:
        return self.positions

    @property
    def pulses(self) -> int:
        return self.pulses_per_position

    @property
    def speed_rpm(self) -> float:
        return float(self.rpm)

    @property
    def setup_cycles(self) -> int:
        return self.dir_setup_cycles

    @property
    def lease_cycles(self) -> int:
        return self.motor_lease_cycles

    @property
    def timeout_cycles(self) -> int:
        return self.motor_timeout_cycles


@dataclass(frozen=True, slots=True)
class MotionCaptureResult:
    """One persisted paired result and all of its cross-domain identities."""

    position_index: int
    guard_request_id: int
    motor_receipt: Mapping[str, Any]
    capture_result: Any
    settle_elapsed_s: float
    camera_request_id: int
    camera_epoch: int
    phase_history: tuple[str, ...] = ()
    persistence_path: str | None = None
    display_receipt: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.position_index < 0:
            raise ValueError("position_index must be non-negative")
        for name, value in (
            ("guard_request_id", self.guard_request_id),
            ("camera_request_id", self.camera_request_id),
            ("camera_epoch", self.camera_epoch),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be an unsigned integer")
        if self.guard_request_id == self.camera_request_id:
            raise ValueError("guard and camera request IDs must be distinct")
        if self.settle_elapsed_s < 0:
            raise ValueError("settle_elapsed_s must be non-negative")

    def as_dict(self) -> dict[str, Any]:
        capture = self.capture_result
        comparison = getattr(capture, "comparison", None)
        return {
            "position_index": self.position_index,
            "guard_request_id": self.guard_request_id,
            "motor_request_id": int(self.motor_receipt.get("request_id", self.motor_receipt.get("accepted_id", 0))),
            "motor_receipt": dict(self.motor_receipt),
            "camera_request_id": self.camera_request_id,
            "camera_epoch": self.camera_epoch,
            "settle_elapsed_s": self.settle_elapsed_s,
            "persistence_path": self.persistence_path,
            "display_receipt": dict(self.display_receipt) if self.display_receipt is not None else None,
            "phase_history": list(self.phase_history),
            "timings": dict(self.metadata.get("timings", {})),
            "angle_raw": int(getattr(capture, "angle_raw", 0)),
            "angle_valid": bool(getattr(capture, "angle_valid", False)),
            "comparison_passed": None if comparison is None else bool(getattr(comparison, "passed", False)),
            "raw_sha256": getattr(capture, "input_sha256", None),
            "metadata": dict(self.metadata),
        }

    @property
    def settle_elapsed(self) -> float:
        return self.settle_elapsed_s

    @property
    def capture(self) -> Any:
        return self.capture_result


@dataclass(frozen=True, slots=True)
class TaskReceipt:
    """Sequence-level receipt returned by :meth:`MotionSession.run_sequence`."""

    task_request_id: int
    results: tuple[MotionCaptureResult, ...]
    phase: MotionPhase
    guard_revoked: bool
    hardware_evidence: str = "offline_mock"
    metadata: Mapping[str, Any] = field(default_factory=dict)


__all__ = [
    "CarrierMode",
    "MotionCaptureResult",
    "MotionConfig",
    "MotionPhase",
    "TaskReceipt",
]
