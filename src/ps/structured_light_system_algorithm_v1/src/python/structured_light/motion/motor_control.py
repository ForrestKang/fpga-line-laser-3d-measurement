"""Dependency-free host API for the one-shot motor register contract.

The transport only needs ``read32(offset)``, ``write32(offset, value)``, and
``close()``. This module never imports PYNQ; board-specific mapping belongs in
the explicitly invoked board entry point.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Dict, Optional


CONTRACT_VERSION = 0x4D465031
CLOCK_HZ = 100_000_000
MAX_PULSES = 100_000
UINT32_MAX = 0xFFFFFFFF

REGISTERS = {
    "version": 0x00,
    "status": 0x04,
    "command": 0x08,
    "request_id": 0x0C,
    "pulse_count": 0x10,
    "period_cycles": 0x14,
    "high_cycles": 0x18,
    "dir_setup_cycles": 0x1C,
    "enable_setup_cycles": 0x20,
    "lease_cycles": 0x24,
    "timeout_cycles": 0x28,
    "direction": 0x2C,
    "heartbeat": 0x30,
    "emitted": 0x34,
    "error": 0x38,
    "accepted_id": 0x3C,
    "epoch": 0x40,
    "clock_hz": 0x44,
    "capabilities": 0x48,
}
COMMANDS = {"arm": 1, "disarm": 2, "start": 4, "stop": 8, "clear": 16}
STATUS_BITS = {
    "busy": 0,
    "armed": 1,
    "done": 2,
    "cancelled": 3,
    "fault": 4,
    "ready_to_arm": 5,
    "step": 6,
    "enable_n": 7,
}
REQUIRED_CAPABILITIES = 0x7


class MotorControlError(RuntimeError):
    """Base exception. ``diagnostics`` is the snapshot from before cleanup."""

    def __init__(self, message: str, *, diagnostics: Optional[dict] = None):
        super().__init__(message)
        self.diagnostics = diagnostics
        self.cleanup_error = None
        self.cleanup_status = None


class ConfigurationError(MotorControlError, ValueError):
    pass


class IdentityError(MotorControlError):
    pass


class BusyError(MotorControlError):
    pass


class MotionCancelled(MotorControlError):
    pass


class MotionTimeout(MotorControlError, TimeoutError):
    pass


class MotorFault(MotorControlError):
    pass


class CleanupError(MotorControlError):
    pass


class MotorController:
    """One-owner controller for bounded, finite pulse requests."""

    def __init__(
        self,
        transport: Any,
        *,
        monotonic: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        poll_interval_s: float = 0.01,
    ) -> None:
        if not callable(getattr(transport, "read32", None)) or not callable(
            getattr(transport, "write32", None)
        ):
            raise TypeError("transport must provide read32(offset) and write32(offset, value)")
        if not math.isfinite(poll_interval_s) or poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be finite and positive")
        self._transport = transport
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._poll_interval_s = float(poll_interval_s)
        self._operation_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._active = False
        self._close_requested = False
        self._closed = False
        self._identity_validated = False
        self.last_diagnostics: Optional[dict] = None
        self.last_cleanup_error: Optional[str] = None

    def status(self) -> dict:
        """Return a read-only register and decoded status snapshot."""
        self._acquire_operation("status")
        try:
            self._ensure_open()
            return self._snapshot()
        finally:
            self._release_operation()

    def validate_identity(self) -> dict:
        """Read and validate VERSION, clock, and required capability bits."""
        self._acquire_operation("validate_identity")
        try:
            self._ensure_open()
            self._validate_identity()
            return self._snapshot()
        finally:
            self._release_operation()

    def move_pulses(
        self,
        count: int,
        period_cycles: int,
        high_cycles: int,
        dir_setup_cycles: int,
        enable_setup_cycles: int,
        lease_cycles: int,
        timeout_cycles: int,
        direction: int,
        request_id: int,
        timeout_s: float,
    ) -> dict:
        """Run exactly one finite request or raise with pre-cleanup diagnostics."""
        config = self._validate_parameters(
            count=count,
            period_cycles=period_cycles,
            high_cycles=high_cycles,
            dir_setup_cycles=dir_setup_cycles,
            enable_setup_cycles=enable_setup_cycles,
            lease_cycles=lease_cycles,
            timeout_cycles=timeout_cycles,
            direction=direction,
            request_id=request_id,
            timeout_s=timeout_s,
        )
        self._acquire_operation("move_pulses")
        self._active = True
        self._stop_requested.clear()
        self._close_requested = False

        deadline = self._monotonic() + config["timeout_s"]
        changed_hardware = False
        result = None
        failure = None
        failure_tb = None
        motion_diagnostics = None

        try:
            self._ensure_open()
            self._validate_identity()
            before = self._snapshot()
            if before["busy"]:
                raise BusyError("hardware is busy; refusing to write a new configuration", diagnostics=before)
            if config["request_id"] <= before["accepted_id"]:
                raise ConfigurationError(
                    "request_id must be strictly greater than hardware accepted_id"
                )

            # Revoke any old permission before clearing, configuring, and arming.
            changed_hardware = True
            self._write_command("disarm")
            disarmed = self._snapshot()
            if disarmed["busy"]:
                raise BusyError("hardware remained busy after DISARM", diagnostics=disarmed)

            if disarmed["fault"] or disarmed["error"]:
                self._write_command("clear")
                cleared = self._wait_until(
                    deadline,
                    lambda snap: not snap["fault"] and snap["error"] == 0,
                    "fault clear did not complete before the host deadline",
                )
                if cleared["busy"]:
                    raise BusyError("hardware became busy while clearing a fault", diagnostics=cleared)

            ready = self._wait_until(
                deadline,
                lambda snap: snap["ready_to_arm"] and not snap["busy"] and not snap["fault"],
                "hardware did not become ready to arm before the host deadline",
            )
            if ready["error"]:
                raise MotorFault("hardware reports an uncleared error", diagnostics=ready)

            # Configuration is frozen before a distinct lease heartbeat and ARM.
            config_registers = (
                ("request_id", config["request_id"]),
                ("pulse_count", config["count"]),
                ("period_cycles", config["period_cycles"]),
                ("high_cycles", config["high_cycles"]),
                ("dir_setup_cycles", config["dir_setup_cycles"]),
                ("enable_setup_cycles", config["enable_setup_cycles"]),
                ("lease_cycles", config["lease_cycles"]),
                ("timeout_cycles", config["timeout_cycles"]),
                ("direction", config["direction"]),
            )
            for name, value in config_registers:
                self._write(name, value)
            readback = self._read_registers()
            mismatches = {
                name: {"expected": value, "actual": readback[name]}
                for name, value in config_registers
                if readback[name] != value
            }
            if mismatches:
                diagnostic = self._snapshot()
                diagnostic["config_mismatches"] = mismatches
                raise MotorControlError("configuration readback mismatch", diagnostics=diagnostic)

            heartbeat = self._next_heartbeat()
            self._write("heartbeat", heartbeat)
            heartbeat_period_s = config["lease_cycles"] / CLOCK_HZ / 3.0
            heartbeat_state = {
                "value": heartbeat,
                "period_s": heartbeat_period_s,
                "next_at": self._monotonic() + heartbeat_period_s,
            }

            self._write_command("arm")
            armed = self._wait_until(
                deadline,
                lambda snap: snap["armed"] or snap["fault"],
                "hardware did not acknowledge ARM before the host deadline",
                heartbeat_state=heartbeat_state,
            )
            if armed["fault"]:
                raise MotorFault("hardware faulted while arming", diagnostics=armed)
            if not armed["armed"]:
                raise MotorControlError("hardware did not enter ARMED", diagnostics=armed)

            # There is exactly one START write for this request.
            self._write_command("start")
            while True:
                now = self._monotonic()
                if now >= deadline:
                    timed_out = MotionTimeout("finite motor request exceeded its host deadline")
                    timed_out.diagnostics = self._snapshot()
                    raise timed_out

                if now >= heartbeat_state["next_at"]:
                    heartbeat_state["value"] = (heartbeat_state["value"] + 1) & UINT32_MAX
                    self._write("heartbeat", heartbeat_state["value"])
                    heartbeat_state["next_at"] = now + heartbeat_period_s

                current = self._snapshot()
                if current["fault"]:
                    raise MotorFault("hardware entered FAULT", diagnostics=current)
                if current["cancelled"]:
                    raise MotionCancelled("finite motor request was cancelled", diagnostics=current)
                if current["done"] and not current["busy"]:
                    if current["accepted_id"] != config["request_id"]:
                        raise MotorControlError(
                            "DONE reported for an unexpected accepted_id", diagnostics=current
                        )
                    if current["emitted"] != config["count"]:
                        raise MotorControlError(
                            "DONE reported with an incorrect emitted pulse count", diagnostics=current
                        )
                    if current["error"] != 0:
                        raise MotorFault("DONE reported with a nonzero hardware error", diagnostics=current)
                    result = dict(current)
                    break
                if not current["busy"]:
                    raise MotorControlError(
                        "request stopped without DONE, CANCELLED, or FAULT", diagnostics=current
                    )

                if self._stop_requested.is_set():
                    # Only the owner thread touches the transport during a move.
                    self._write_command("stop")
                    self._stop_requested.clear()

                remaining = deadline - self._monotonic()
                until_heartbeat = heartbeat_state["next_at"] - self._monotonic()
                lease_s = config["lease_cycles"] / CLOCK_HZ
                delay = min(self._poll_interval_s, lease_s / 4.0, remaining, until_heartbeat)
                if delay > 0:
                    self._sleep(delay)
        except BaseException as exc:
            failure = exc
            failure_tb = exc.__traceback__
            if getattr(exc, "diagnostics", None) is None:
                try:
                    exc.diagnostics = self._snapshot()
                except BaseException as diag_exc:
                    exc.diagnostics = {"snapshot_error": repr(diag_exc)}
            motion_diagnostics = getattr(exc, "diagnostics", None)
            self.last_diagnostics = motion_diagnostics
            if isinstance(exc, (MotionTimeout, KeyboardInterrupt)):
                try:
                    if motion_diagnostics and motion_diagnostics.get("busy"):
                        self._write_command("stop")
                except BaseException as stop_exc:
                    exc.cleanup_error = repr(stop_exc)
        finally:
            cleanup_status = None
            cleanup_error = None
            if changed_hardware:
                try:
                    self._write_command("disarm")
                    cleanup_status = self._snapshot()
                except BaseException as exc:
                    cleanup_error = exc
            if self._close_requested:
                try:
                    self._close_transport()
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
                self._close_requested = False
            if failure is not None:
                failure.cleanup_status = cleanup_status
                if cleanup_error is not None:
                    failure.cleanup_error = repr(cleanup_error)
            elif cleanup_error is not None:
                failure = CleanupError("failed to DISARM or close after the finite request")
                failure.diagnostics = motion_diagnostics or result
                failure.cleanup_error = repr(cleanup_error)
                failure_tb = failure.__traceback__
            if result is not None:
                result["cleanup_status"] = cleanup_status
                self.last_diagnostics = result
            self._active = False
            self._stop_requested.clear()
            self._release_operation()

        if failure is not None:
            raise failure.with_traceback(failure_tb)
        return result

    def stop(self) -> dict:
        """Request cancellation; an active move's owner thread sends STOP."""
        if self._active:
            self._stop_requested.set()
            return {"requested": True, "deferred_to_owner": True}

        self._acquire_operation("stop")
        diagnostic = None
        failure = None
        try:
            self._ensure_open()
            self._validate_identity()
            diagnostic = self._snapshot()
            if diagnostic["busy"]:
                self._write_command("stop")
                deadline = self._monotonic() + 1.0
                diagnostic = self._wait_until(
                    deadline,
                    lambda snap: snap["cancelled"] or snap["fault"] or not snap["busy"],
                    "STOP did not reach a terminal state",
                )
            if diagnostic["state"] != "DONE":
                self._write_command("disarm")
            return diagnostic
        except BaseException as exc:
            failure = exc
            if getattr(exc, "diagnostics", None) is None:
                try:
                    exc.diagnostics = self._snapshot()
                except BaseException as diag_exc:
                    exc.diagnostics = {"snapshot_error": repr(diag_exc)}
            raise
        finally:
            self._release_operation()

    def clear_fault(self) -> dict:
        """Explicitly disarm and clear a sticky fault; never retries motion."""
        self._acquire_operation("clear_fault")
        changed_hardware = False
        try:
            self._ensure_open()
            self._validate_identity()
            before = self._snapshot()
            if before["busy"]:
                raise BusyError("hardware is busy; refusing to clear fault", diagnostics=before)
            changed_hardware = True
            self._write_command("disarm")
            disarmed = self._snapshot()
            if disarmed["busy"]:
                raise BusyError("hardware became busy while disarming", diagnostics=disarmed)
            if disarmed["fault"] or disarmed["error"]:
                self._write_command("clear")
                deadline = self._monotonic() + 1.0
                cleared = self._wait_until(
                    deadline,
                    lambda snap: not snap["fault"] and snap["error"] == 0,
                    "fault clear did not complete",
                )
                self.last_diagnostics = cleared
                return cleared
            self.last_diagnostics = disarmed
            return disarmed
        finally:
            if changed_hardware:
                try:
                    self._write_command("disarm")
                except BaseException:
                    pass
            self._release_operation()

    def close(self) -> dict:
        """DISARM before closing; defer to the active owner if a move is running."""
        if self._closed:
            return {"closed": True, "already_closed": True}
        if not self._operation_lock.acquire(blocking=False):
            self._close_requested = True
            self._stop_requested.set()
            return {"closed": False, "deferred": True}

        disarm_status = None
        close_error = None
        try:
            if self._closed:
                return {"closed": True, "already_closed": True}
            try:
                self._validate_identity()
            except BaseException as exc:
                close_error = exc
            if close_error is None:
                try:
                    self._write_command("disarm")
                    disarm_status = self._snapshot()
                except BaseException as exc:
                    close_error = exc
            try:
                self._close_transport()
            except BaseException as exc:
                close_error = close_error or exc
        finally:
            self._release_operation()
        if close_error is not None:
            raise close_error
        self.last_diagnostics = disarm_status
        return {"closed": True, "status": disarm_status}

    def _validate_parameters(self, **values: Any) -> dict:
        int_fields = (
            "count",
            "period_cycles",
            "high_cycles",
            "dir_setup_cycles",
            "enable_setup_cycles",
            "lease_cycles",
            "timeout_cycles",
            "direction",
            "request_id",
        )
        for name in int_fields:
            value = values[name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigurationError(f"{name} must be an integer")
        if not 1 <= values["count"] <= MAX_PULSES:
            raise ConfigurationError(f"count must be in [1, {MAX_PULSES}]")
        if values["period_cycles"] <= 1 or not 1 <= values["high_cycles"] < values["period_cycles"]:
            raise ConfigurationError("period_cycles must exceed positive high_cycles")
        for name in ("dir_setup_cycles", "enable_setup_cycles", "lease_cycles", "timeout_cycles"):
            if not 1 <= values[name] <= UINT32_MAX:
                raise ConfigurationError(f"{name} must be in [1, 2^32-1]")
        for name in ("period_cycles", "high_cycles"):
            if not 1 <= values[name] <= UINT32_MAX:
                raise ConfigurationError(f"{name} must fit an unsigned 32-bit register")
        if values["direction"] not in (0, 1):
            raise ConfigurationError("direction must be 0 or 1")
        if not 1 <= values["request_id"] <= UINT32_MAX:
            raise ConfigurationError("request_id must be in [1, 2^32-1]")
        timeout_s = values["timeout_s"]
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
            raise ConfigurationError("timeout_s must be a finite positive number")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ConfigurationError("timeout_s must be a finite positive number")
        result = dict(values)
        result["timeout_s"] = float(timeout_s)
        return result

    def _validate_identity(self) -> None:
        values = self._read_registers()
        version = values["version"]
        clock_hz = values["clock_hz"]
        capabilities = values["capabilities"]
        if version != CONTRACT_VERSION:
            raise IdentityError(
                f"VERSION mismatch: expected 0x{CONTRACT_VERSION:08x}, got 0x{version:08x}"
            )
        if clock_hz != CLOCK_HZ:
            raise IdentityError(f"clock mismatch: expected {CLOCK_HZ}, got {clock_hz}")
        if capabilities & REQUIRED_CAPABILITIES != REQUIRED_CAPABILITIES:
            raise IdentityError(
                f"capabilities 0x{capabilities:08x} omit required mask 0x{REQUIRED_CAPABILITIES:08x}"
            )
        self._identity_validated = True

    def _snapshot(self) -> dict:
        registers = self._read_registers()
        raw_status = registers["status"]
        decoded = {
            "raw_status": raw_status,
            "busy": self._status_bit(raw_status, "busy"),
            "armed": self._status_bit(raw_status, "armed"),
            "done": self._status_bit(raw_status, "done"),
            "cancelled": self._status_bit(raw_status, "cancelled"),
            "fault": self._status_bit(raw_status, "fault"),
            "ready_to_arm": self._status_bit(raw_status, "ready_to_arm"),
            "step": self._status_bit(raw_status, "step"),
            "enable_n": self._status_bit(raw_status, "enable_n"),
            "emitted": registers["emitted"],
            "error": registers["error"],
            "accepted_id": registers["accepted_id"],
            "request_id": registers["request_id"],
            "pulse_count": registers["pulse_count"],
            "epoch": registers["epoch"],
            "version": registers["version"],
            "clock_hz": registers["clock_hz"],
            "capabilities": registers["capabilities"],
        }
        if decoded["fault"]:
            decoded["state"] = "FAULT"
        elif decoded["cancelled"]:
            decoded["state"] = "CANCELLED"
        elif decoded["done"]:
            decoded["state"] = "DONE"
        elif decoded["busy"]:
            decoded["state"] = "HIGH" if decoded["step"] else "LOW"
        elif decoded["armed"]:
            decoded["state"] = "ARMED"
        else:
            decoded["state"] = "IDLE"
        snapshot = dict(decoded)
        snapshot["status"] = dict(decoded)
        snapshot["registers"] = registers
        self.last_diagnostics = snapshot
        return snapshot

    def _read_registers(self) -> dict:
        return {name: int(self._transport.read32(offset)) & UINT32_MAX for name, offset in REGISTERS.items()}

    def _write(self, name: str, value: int) -> None:
        self._transport.write32(REGISTERS[name], value & UINT32_MAX)

    def _write_command(self, name: str) -> None:
        self._write("command", COMMANDS[name])

    def _next_heartbeat(self) -> int:
        return (int(self._transport.read32(REGISTERS["heartbeat"])) + 1) & UINT32_MAX

    def _wait_until(self, deadline: float, predicate: Callable[[dict], bool], message: str,
                    heartbeat_state: Optional[dict] = None) -> dict:
        while True:
            now = self._monotonic()
            if now >= deadline:
                snapshot = self._snapshot()
                raise MotionTimeout(message, diagnostics=snapshot)
            if heartbeat_state and now >= heartbeat_state["next_at"]:
                heartbeat_state["value"] = (heartbeat_state["value"] + 1) & UINT32_MAX
                self._write("heartbeat", heartbeat_state["value"])
                heartbeat_state["next_at"] = now + heartbeat_state["period_s"]
            snapshot = self._snapshot()
            if predicate(snapshot):
                return snapshot
            remaining = deadline - self._monotonic()
            delay = min(self._poll_interval_s, remaining)
            if heartbeat_state:
                delay = min(
                    delay,
                    heartbeat_state["period_s"] / 3.0,
                    heartbeat_state["next_at"] - self._monotonic(),
                )
            if delay > 0:
                self._sleep(delay)

    @staticmethod
    def _status_bit(status: int, name: str) -> bool:
        return bool(status & (1 << STATUS_BITS[name]))

    def _ensure_open(self) -> None:
        if self._closed:
            raise MotorControlError("motor controller is closed")

    def _release_operation(self) -> None:
        """Finish a deferred close while still holding the owner lock."""
        try:
            if self._closed:
                self._close_requested = False
            if self._close_requested and not self._closed:
                self._close_requested = False
                status = None
                errors = []
                try:
                    self._validate_identity()
                    self._write_command("disarm")
                    status = self._snapshot()
                except BaseException as exc:
                    errors.append(repr(exc))
                try:
                    self._close_transport()
                except BaseException as exc:
                    errors.append(repr(exc))
                self.last_cleanup_error = "; ".join(errors) or None
                self.last_diagnostics = {
                    "closed": self._closed,
                    "status_before_close": status,
                    "cleanup_error": self.last_cleanup_error,
                }
        finally:
            self._operation_lock.release()

    def _acquire_operation(self, name: str) -> None:
        if self._closed:
            raise MotorControlError("motor controller is closed")
        if not self._operation_lock.acquire(blocking=False):
            raise BusyError(f"another thread owns the motor transport during {name}")

    def _close_transport(self) -> None:
        if self._closed:
            return
        closer = getattr(self._transport, "close", None)
        if callable(closer):
            closer()
        self._closed = True
