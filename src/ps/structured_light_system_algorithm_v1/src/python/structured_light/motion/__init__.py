"""Offline-safe Stage B motion and capture host APIs."""

from .guard import (
    BSG1Guard,
    BatchGuard,
    GuardBusyError,
    GuardController,
    GuardError,
    GuardFault,
    GuardIdentityError,
    GuardTimeout,
)
from .orchestrator import (
    BMotionSession,
    MotionBusyError,
    MotionCaptureSessionError,
    MotionError,
    MotionIdentityError,
    MotionCaptureOrchestrator,
    MotionSession,
    MotionTimeout,
)
from .lifecycle import (
    GenerationGate,
    GenerationRevoked,
    HardwareOwnershipError,
    HardwareProcessLock,
    ProcessHardwareLock,
)
from .settle import (
    SAMPLE_SCHEDULE_MS,
    SettleConfig,
    SettleEstimate,
    brightness_normalized_difference,
    diagnostic_settle_schedule,
    estimate_settle,
    estimate_settling,
    phase_correlation_shift,
    run_diagnostic_settle,
)
from .types import CarrierMode, MotionCaptureResult, MotionConfig, MotionPhase, TaskReceipt

__all__ = [
    "CarrierMode", "GuardBusyError", "GuardController", "GuardError", "GuardFault",
    "GuardIdentityError", "GuardTimeout", "BatchGuard", "BSG1Guard", "GenerationGate", "GenerationRevoked", "HardwareOwnershipError", "HardwareProcessLock", "ProcessHardwareLock", "MotionBusyError", "MotionCaptureResult",
    "MotionCaptureSessionError", "MotionConfig", "MotionError", "MotionIdentityError",
    "MotionPhase", "MotionSession", "MotionCaptureOrchestrator", "BMotionSession", "MotionTimeout", "SAMPLE_SCHEDULE_MS",
    "SettleConfig", "SettleEstimate", "TaskReceipt", "brightness_normalized_difference",
    "diagnostic_settle_schedule", "estimate_settle", "estimate_settling", "phase_correlation_shift",
    "run_diagnostic_settle",
]
