"""Immutable value types shared by the P2 host transports and controller."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..frame_io import GrayFrame
from ..profile import Profile


@dataclass(frozen=True, slots=True)
class CentroidConfig:
    threshold: int = 30
    min_width: int = 1
    max_width: int = 32
    min_sum: int = 30
    min_peak: int = 15


@dataclass(frozen=True, slots=True)
class RoiConfig:
    x0: int
    y0: int
    x1: int
    y1: int

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x0, self.y0, self.x1, self.y1


@dataclass(frozen=True, slots=True)
class ControllerStatus:
    busy: bool
    scan_done: bool
    packer_busy: bool
    motion_state: int
    error_flags: int
    profile_id: int
    timestamp: int
    epoch: int
    request_id: int | None
    raw_dma_ready: bool
    profile_dma_ready: bool


@dataclass(frozen=True, slots=True)
class DiagnosticRow:
    row: int
    xc_fixed: int
    s0: int
    s1: int
    peak: int
    width: int
    candidate_count: int
    flags: int
    quality: int
    valid: bool
    diagnostics: int
    profile_id: int


@dataclass(frozen=True, slots=True)
class DiagnosticsSnapshot:
    version: int
    capabilities: int
    ready: bool
    pclk_hz: int
    width: int
    height: int
    last_line: int
    last_frame: int
    frame_count: int
    camera_device_address: int
    camera_register_address: int
    camera_write_data: int
    camera_read_data: int
    camera_command: int
    camera_status: int
    camera_control: int
    abort: int
    epoch: int
    request_id: int
    row_count: int
    error: int
    rows: tuple[DiagnosticRow, ...] = ()


@dataclass(frozen=True, slots=True)
class ReferenceMismatch:
    row: int | None
    field: str
    expected: Any
    actual: Any


@dataclass(frozen=True, slots=True)
class ReferenceComparison:
    passed: bool
    checked_rows: int
    mismatches: tuple[ReferenceMismatch, ...] = ()
    compared_fields: tuple[str, ...] = ()

    @property
    def mismatch_count(self) -> int:
        return len(self.mismatches)


@dataclass(frozen=True, slots=True)
class CaptureResult:
    request_id: int
    epoch: int
    session_id: str
    raw_frame: GrayFrame
    profile_payload: bytes
    profile: Profile
    diagnostics: DiagnosticsSnapshot
    comparison: ReferenceComparison | None
    timestamp: int
    angle_raw: int = 0
    angle_valid: bool = False
    motion_enabled: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def raw_bytes(self) -> bytes:
        return self.raw_frame.pixels

    @property
    def input_sha256(self) -> str:
        return self.raw_frame.sha256


@dataclass(frozen=True, slots=True)
class CaptureHandle:
    request_id: int
    epoch: int
    raw_armed: bool
    profile_armed: bool


__all__ = [
    "CentroidConfig", "RoiConfig", "ControllerStatus", "DiagnosticRow",
    "DiagnosticsSnapshot", "ReferenceMismatch", "ReferenceComparison",
    "CaptureResult", "CaptureHandle",
]
