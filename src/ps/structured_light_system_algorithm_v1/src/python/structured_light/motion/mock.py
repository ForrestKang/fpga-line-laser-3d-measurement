"""Deterministic offline transports for Stage B host tests and demos."""

from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Any, Iterable

from ..frame_io import GrayFrame
from ..p2 import P2Controller, SessionRecorder
from ..p2.errors import CaptureTimeout, ProtocolError
from ..p2.transport import MockTransport, ReplayTransport
from .guard import (
    CAPABILITIES,
    CLOCK_HZ,
    CONTRACT_VERSION,
    HEARTBEAT_HALF_CYCLES,
    REGISTERS as GREG,
    STATUS_BITS as GBITS,
    GuardController,
)
from .types import MotionConfig
from ..motion.motor_control import (
    CLOCK_HZ as MOTOR_CLOCK_HZ,
    CONTRACT_VERSION as MOTOR_VERSION,
    MAX_PULSES,
    REGISTERS as MREG,
    COMMANDS as MCOMMANDS,
    STATUS_BITS as MBITS,
    REQUIRED_CAPABILITIES as MOTOR_CAPABILITIES,
    MotorController,
)


class MockGuardTransport:
    """Small BSG1 model; it is not a cycle-accurate RTL simulation."""

    base_address = 0x43C5_0000
    address_range = 0x1_0000

    def __init__(self, *, carrier_mode: int = 0, failures: Iterable[str] = (), events: list[str] | None = None) -> None:
        self.failures = frozenset(str(item) for item in failures)
        self.events = events if events is not None else []
        self.regs = {
            GREG["version"]: CONTRACT_VERSION,
            GREG["clock_hz"]: CLOCK_HZ,
            GREG["capabilities"]: CAPABILITIES,
            GREG["heartbeat_half_cycles"]: HEARTBEAT_HALF_CYCLES,
            GREG["carrier_mode"]: int(carrier_mode),
            GREG["accepted_id"]: 0,
            GREG["heartbeat"]: 0,
            GREG["error"]: 0,
        }
        self.heartbeat_writes: list[int] = []
        self.closed = False
        self.max_active_positions = 0
        self._active_positions = 0
        self._positions_seen = 0
        self._armed_once = False
        self._disarmed = False

    @property
    def active(self) -> bool:
        return bool(self._status_word() & (1 << GBITS["active"]))

    @property
    def fault(self) -> bool:
        return bool(self._status_word() & (1 << GBITS["fault"]))

    def read32(self, offset: int) -> int:
        if self.closed:
            raise RuntimeError("guard transport closed")
        if offset == GREG["status"]:
            return self._status_word()
        return int(self.regs.get(offset, 0))

    def write32(self, offset: int, value: int) -> None:
        if self.closed:
            raise RuntimeError("guard transport closed")
        value &= 0xFFFF_FFFF
        if offset == GREG["heartbeat"]:
            self.heartbeat_writes.append(value)
            self.regs[offset] = value
            self.events.append("guard.heartbeat")
            return
        if offset == GREG["command"]:
            self._command(value)
            return
        self.regs[offset] = value

    def close(self) -> None:
        self.closed = True
        self.events.append("guard.close")

    def note_position(self) -> None:
        if self.active:
            self._positions_seen += 1
            self._active_positions += 1
            self.max_active_positions = max(self.max_active_positions, self._positions_seen)

    def finish_position(self) -> None:
        self._active_positions = max(0, self._active_positions - 1)

    def _command(self, command: int) -> None:
        if command == 1:  # ARM
            self.events.append("guard.arm")
            self._armed_once = True
            self._disarmed = False
            if "guard_busy" in self.failures:
                self.regs[GREG["error"]] = 7
                return
            if "guard_fault" in self.failures:
                self.regs[GREG["error"]] = 5
                return
            self.regs[GREG["accepted_id"]] = self.regs[GREG["request_id"]]
            return
        if command == 2:  # DISARM
            self.events.append("guard.revoke")
            self._disarmed = True
            self._active_positions = 0
            self.regs[GREG["accepted_id"]] = self.regs.get(GREG["accepted_id"], 0)
            return
        if command == 4:  # STOP
            self.events.append("guard.stop")
            self.regs[GREG["error"]] = 3
            return
        if command == 8:  # CLEAR
            self.events.append("guard.clear")
            self._disarmed = False
            self.regs[GREG["error"]] = 0
            return
        self.regs[GREG["error"]] = 8

    def _status_word(self) -> int:
        active = bool(self.regs.get(GREG["request_id"], 0)) and not self.regs.get(GREG["error"], 0)
        # ``disarm`` is represented by a private marker so request_id remains
        # readable as the last request like the RTL contract.
        active = active and not getattr(self, "_disarmed", False)
        if getattr(self, "_armed_once", False) and not getattr(self, "_disarmed", False):
            active = True
        bits = 0
        if active:
            bits |= 1 << GBITS["active"]
            bits |= 1 << GBITS["enable_pin"]
        if self.regs.get(GREG["error"], 0):
            bits |= 1 << GBITS["fault"]
        if self.regs.get(GREG["heartbeat"], 0) and active:
            bits |= 1 << GBITS["heartbeat_pin"]
        if self.regs.get(GREG["carrier_mode"], 0) == 1:
            bits |= 1 << GBITS["field_power_good"]
        return bits


class MockMotorTransport:
    """Behavioral MFP1 model for finite host orchestration tests."""

    def __init__(self, *, mode: str = "done", failures: Iterable[str] = (), events: list[str] | None = None) -> None:
        self.mode = mode
        self.failures = frozenset(str(item) for item in failures)
        self.events = events if events is not None else []
        self.regs = {
            MREG["version"]: MOTOR_VERSION,
            MREG["clock_hz"]: MOTOR_CLOCK_HZ,
            MREG["capabilities"]: MOTOR_CAPABILITIES,
            MREG["accepted_id"]: 0,
            MREG["heartbeat"]: 0,
            MREG["error"]: 0,
            MREG["emitted"]: 0,
        }
        self.busy = "motor_busy" in self.failures
        self.armed = False
        self.done = False
        self.cancelled = False
        self.fault = False
        self.ready_to_arm = not self.busy
        self.step = False
        self.enable_n = True
        self.closed = False
        self.stop_calls = 0
        self.start_writes = 0
        self._finish_at: float | None = None

    def read32(self, offset: int) -> int:
        if self.closed:
            raise RuntimeError("motor transport closed")
        if offset == MREG["status"]:
            self._finish_if_due()
            return self._status_word()
        return int(self.regs.get(offset, 0))

    def write32(self, offset: int, value: int) -> None:
        if self.closed:
            raise RuntimeError("motor transport closed")
        value &= 0xFFFF_FFFF
        self.events.append(f"motor.write:{offset:#x}")
        if offset == MREG["command"]:
            self._command(value)
        else:
            self.regs[offset] = value

    def close(self) -> None:
        self.closed = True
        self.events.append("motor.close")

    def _command(self, value: int) -> None:
        if value == MCOMMANDS["disarm"]:
            self.armed = False
            self.enable_n = True
            self.ready_to_arm = not self.busy
            self.events.append("motor.disarm")
        elif value == MCOMMANDS["clear"]:
            self.fault = False
            self.done = False
            self.cancelled = False
            self.regs[MREG["error"]] = 0
        elif value == MCOMMANDS["arm"]:
            if not self.busy and not self.fault:
                self.armed = True
                self.enable_n = False
                self.ready_to_arm = False
        elif value == MCOMMANDS["start"]:
            self.start_writes += 1
            self.events.append("motor.start")
            configured = self.regs[MREG["request_id"]]
            self.regs[MREG["accepted_id"]] = configured + (1 if "motor_id_mismatch" in self.failures else 0)
            self.done = self.cancelled = False
            self.ready_to_arm = False
            mode = "fault" if "motor_fault" in self.failures else ("stuck" if "motor_timeout" in self.failures else self.mode)
            if mode == "done":
                self.busy = False
                self.done = True
                self.regs[MREG["emitted"]] = self.regs[MREG["pulse_count"]]
            elif mode == "delay":
                self.busy = True
                self._finish_at = time.monotonic() + 0.01
            elif mode == "stuck":
                self.busy = True
            elif mode == "cancelled":
                self.busy = False
                self.cancelled = True
            elif mode == "fault":
                self.busy = False
                self.fault = True
                self.regs[MREG["error"]] = 5
        elif value == MCOMMANDS["stop"]:
            self.stop_calls += 1
            # Distinguish the worker's controller-level timeout STOP from the
            # owner cleanup call recorded by TrackingMotorController.
            self.events.append("motor.transport_stop")
            self.busy = False
            self.cancelled = True
            self.done = False

    def _finish_if_due(self) -> None:
        if self._finish_at is not None and time.monotonic() >= self._finish_at:
            self.busy = False
            self.done = True
            self._finish_at = None
            self.regs[MREG["emitted"]] = self.regs[MREG["pulse_count"]]

    def _status_word(self) -> int:
        values = {
            "busy": self.busy,
            "armed": self.armed,
            "done": self.done,
            "cancelled": self.cancelled,
            "fault": self.fault,
            "ready_to_arm": self.ready_to_arm,
            "step": self.step,
            "enable_n": self.enable_n,
        }
        return sum((1 << MBITS[name]) for name, active in values.items() if active)


class _FailingRecorder:
    def __init__(self, root: Path, events: list[str]) -> None:
        self._delegate = SessionRecorder(root)
        self.session_id = self._delegate.session_id
        self.events = events

    def record_capture(self, _result: Any) -> None:
        self.events.append("disk.error")
        raise OSError("synthetic disk failure")

    def record_error(self, **fields: Any) -> None:
        self._delegate.record_error(**fields)

    def record_cancel(self, **fields: Any) -> None:
        self._delegate.record_cancel(**fields)

    def close(self) -> None:
        self._delegate.close()


class TrackingP2Controller(P2Controller):
    def __init__(self, *args: Any, events: list[str], failures: frozenset[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.events = events
        self.failures = failures
        self.stop_calls = 0

    def capture_pair(self, timeout_s: float | None = None):
        if "camera_busy" in self.failures:
            raise RuntimeError("camera is busy")
        if "capture_timeout" in self.failures:
            raise CaptureTimeout("synthetic camera timeout")
        if "capture_id_mismatch" in self.failures:
            raise ProtocolError("synthetic camera request_id mismatch")
        if "capture_epoch_mismatch" in self.failures:
            raise ProtocolError("synthetic camera epoch mismatch")
        self.events.append("camera.capture")
        return super().capture_pair(timeout_s=timeout_s)

    def stop(self) -> None:
        self.stop_calls += 1
        self.events.append("camera.stop")
        super().stop()


class MockDisplay:
    def __init__(self, *, failures: Iterable[str] = (), events: list[str] | None = None) -> None:
        self.failures = frozenset(str(item) for item in failures)
        self.events = events if events is not None else []
        self.closed = False
        self.show_calls = 0
        self.accepts_gray8 = True

    def show_capture(self, result: Any) -> dict[str, Any]:
        return self.show(bytes(getattr(result, "raw_bytes", b"")))

    def show(self, payload: bytes) -> dict[str, Any]:
        if "display_error" in self.failures:
            self.events.append("display.error")
            raise OSError("synthetic display failure")
        self.show_calls += 1
        self.events.append("display.show")
        return {"shown": True, "bytes": len(payload), "call": self.show_calls}

    def close(self) -> None:
        self.closed = True
        self.events.append("display.close")


class TrackingMotorController(MotorController):
    def __init__(self, *args: Any, events: list[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.events = events
        self.stop_calls = 0

    def stop(self) -> dict[str, Any]:
        self.stop_calls += 1
        self.events.append("motor.stop")
        return super().stop()


class MockMotionHardware:
    """Convenience assembly of BSG1, MFP1, P2 and display mocks."""

    def __init__(
        self,
        *,
        output: str | Path,
        width: int = 40,
        height: int = 32,
        failures: Iterable[str] = (),
    ) -> None:
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.failures = frozenset(str(item) for item in failures)
        # Offline fault paths stay quick while preserving the production
        # register defaults in MotionConfig itself.
        self.config = MotionConfig(motor_timeout_s=0.05, capture_timeout_s=1.0, cleanup_timeout_s=0.1)
        self.events: list[str] = []
        self.guard_transport = MockGuardTransport(failures=self.failures, events=self.events)
        self.guard = GuardController(self.guard_transport, carrier_mode="direct_wire")
        self.motor_transport = MockMotorTransport(
            mode="done", failures=self.failures, events=self.events
        )
        self.motor = TrackingMotorController(self.motor_transport, poll_interval_s=0.001, events=self.events)
        self.motor.stage_b_default_config = self.config
        pixels = bytearray()
        for row in range(height):
            for column in range(width):
                value = (column * 7 + row * 11) % 30
                if 8 <= column <= 12:
                    value += 180
                pixels.append(value & 0xFF)
        frame = GrayFrame.from_pixels(width, height, bytes(pixels))
        faults = {"profile_error"} if "capture_mismatch" in self.failures else set()
        recorder: Any = SessionRecorder(self.output / "camera")
        if "disk_error" in self.failures:
            recorder = _FailingRecorder(self.output / "camera", self.events)
        camera_transport = MockTransport(frame, faults=faults, latency_s=0.0)
        self.camera_transport = camera_transport
        self.camera = TrackingP2Controller(
            camera_transport, recorder=recorder, events=self.events, failures=self.failures,
            default_timeout_s=0.1,
        )
        self.display = MockDisplay(failures=self.failures, events=self.events)

    def components(self) -> dict[str, Any]:
        return {
            "guard": self.guard,
            "motor": self.motor,
            "camera": self.camera,
            "display": self.display,
        }


class ReplayMotionHardware(MockMotionHardware):
    """Stage B assembly that reuses a recorded P2 request without board I/O."""

    def __init__(self, source: str | Path, *, output: str | Path, failures: Iterable[str] = ()) -> None:
        # The guard and MFP1 remain deterministic mock domains; only the
        # camera stream comes from the immutable replay fixture.
        super().__init__(output=output, failures=failures)
        self.camera_transport = ReplayTransport(source)
        recorder: Any = SessionRecorder(Path(output) / "camera_replay")
        self.camera = TrackingP2Controller(
            self.camera_transport, recorder=recorder, events=self.events,
            failures=self.failures, default_timeout_s=0.1,
        )
        self.config = MotionConfig(motor_timeout_s=0.05, capture_timeout_s=1.0, cleanup_timeout_s=0.1)


MockBSG1Transport = MockGuardTransport
MockMFP1Transport = MockMotorTransport


__all__ = [
    "MockDisplay", "MockGuardTransport", "MockMotionHardware", "MockMotorTransport", "ReplayMotionHardware",
    "TrackingMotorController", "TrackingP2Controller", "MockBSG1Transport", "MockMFP1Transport",
]
