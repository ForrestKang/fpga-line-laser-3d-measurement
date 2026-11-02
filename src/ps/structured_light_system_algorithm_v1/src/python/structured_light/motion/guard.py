"""Host-side BSG1 batch-safety guard.

The guard is intentionally a small register adapter.  It owns the permit for
one motion task and exposes no background heartbeat thread: the orchestrator's
single bounded polling loop calls :meth:`refresh` while the expected stage is
still inside its deadline.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping

from .types import CarrierMode


CONTRACT_VERSION = 0x42534731  # BSG1
CLOCK_HZ = 100_000_000
BASE_ADDRESS = 0x43C5_0000
ADDRESS_RANGE = 0x1_0000
HEARTBEAT_HALF_CYCLES = 500_000
CAPABILITIES = 0x7
UINT32_MAX = 0xFFFF_FFFF

REGISTERS = {
    "version": 0x00,
    "status": 0x04,
    "command": 0x08,
    "request_id": 0x0C,
    "heartbeat": 0x10,
    "lease_cycles": 0x14,
    "timeout_ms": 0x18,
    "error": 0x1C,
    "accepted_id": 0x20,
    "carrier_mode": 0x24,
    "heartbeat_half_cycles": 0x28,
    "clock_hz": 0x2C,
    "capabilities": 0x30,
}
COMMANDS = {"arm": 1, "disarm": 2, "stop": 4, "clear": 8}
STATUS_BITS = {
    "active": 0,
    "fault": 1,
    "heartbeat_pin": 2,
    "motor_busy": 3,
    "step_pin": 4,
    "enable_pin": 5,
    "field_power_good": 6,
}
ERRORS = {
    "none": 0,
    "invalid_config": 1,
    "lease_expired": 2,
    "cancelled": 3,
    "task_timeout": 4,
    "motor_fault": 5,
    "field_power_lost": 6,
    "duplicate_request": 7,
    "invalid_command": 8,
}


class GuardError(RuntimeError):
    """Base error raised by the BSG1 adapter."""

    def __init__(self, message: str, *, diagnostics: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


class GuardIdentityError(GuardError):
    pass


class GuardBusyError(GuardError):
    pass


class GuardFault(GuardError):
    pass


class GuardTimeout(GuardError, TimeoutError):
    pass


def _u32(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= UINT32_MAX:
        raise ValueError(f"{name} must be an unsigned 32-bit integer >= {minimum}")
    return value


def _load_manifest(manifest: str | Path | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if manifest is None:
        return None
    if isinstance(manifest, Mapping):
        return dict(manifest)
    path = Path(manifest)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuardIdentityError(f"cannot read guard manifest {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise GuardIdentityError("guard manifest must contain a JSON object")
    return loaded


class GuardController:
    """One-owner, bounded BSG1 host controller."""

    def __init__(
        self,
        transport: Any,
        *,
        carrier_mode: CarrierMode | str | int = CarrierMode.DIRECT_WIRE,
        manifest: str | Path | Mapping[str, Any] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        poll_interval_s: float = 0.005,
    ) -> None:
        if not callable(getattr(transport, "read32", None)) or not callable(getattr(transport, "write32", None)):
            raise TypeError("guard transport must provide read32(offset) and write32(offset, value)")
        if not 0 < poll_interval_s <= 0.025:
            raise ValueError("poll_interval_s must be in (0,0.025]")
        self.transport = transport
        self.carrier_mode = self._mode(carrier_mode)
        self.manifest = _load_manifest(manifest)
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._poll_interval_s = float(poll_interval_s)
        self._lock = threading.RLock()
        self._closed = False
        self._active_request: int | None = None
        self._heartbeat_value: int | None = None
        self._heartbeat_values: set[int] = set()
        self._validated_mode: int | None = None
        self.last_diagnostics: dict[str, Any] | None = None

    @staticmethod
    def _mode(value: CarrierMode | str | int) -> CarrierMode:
        if isinstance(value, CarrierMode):
            return value
        if value in (0, "0", "direct_wire"):
            return CarrierMode.DIRECT_WIRE
        if value in (1, "1", "expansion_board"):
            return CarrierMode.EXPANSION_BOARD
        raise ValueError("carrier_mode must be direct_wire/0 or expansion_board/1")

    def _ensure_open(self) -> None:
        if self._closed:
            raise GuardError("guard controller is closed")

    @property
    def active(self) -> bool:
        return bool(self.status().get("active", False))

    @property
    def max_active_positions(self) -> int:
        return int(getattr(self.transport, "max_active_positions", 0))

    def _read(self, name: str) -> int:
        return int(self.transport.read32(REGISTERS[name])) & UINT32_MAX

    def _write(self, name: str, value: int) -> None:
        self.transport.write32(REGISTERS[name], int(value) & UINT32_MAX)

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            raw = self._read("status")
            values = {name: self._read(name) for name in REGISTERS if name not in {"status", "command"}}
            decoded = {name: bool(raw & (1 << bit)) for name, bit in STATUS_BITS.items()}
            result: dict[str, Any] = {
                "raw_status": raw,
                **decoded,
                **values,
                "state": "FAULT" if decoded["fault"] else ("ACTIVE" if decoded["active"] else "IDLE"),
                "carrier_mode_name": "direct_wire" if values["carrier_mode"] == 0 else "expansion_board",
            }
            self.last_diagnostics = result
            return result

    def validate_identity(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            snapshot = self.status()
            checks = (
                (snapshot["version"], CONTRACT_VERSION, "BSG1 VERSION"),
                (snapshot["clock_hz"], CLOCK_HZ, "BSG1 clock"),
                (snapshot["capabilities"] & CAPABILITIES, CAPABILITIES, "BSG1 capabilities"),
                (snapshot["heartbeat_half_cycles"], HEARTBEAT_HALF_CYCLES, "BSG1 heartbeat_half_cycles"),
                (snapshot["carrier_mode"], self.carrier_mode.contract_value, "immutable carrier mode"),
            )
            for actual, expected, label in checks:
                if actual != expected:
                    raise GuardIdentityError(f"{label} mismatch: expected {expected!r}, got {actual!r}", diagnostics=snapshot)
            advertised_base = getattr(self.transport, "base_address", BASE_ADDRESS)
            advertised_range = getattr(self.transport, "address_range", ADDRESS_RANGE)
            if int(advertised_base) != BASE_ADDRESS or int(advertised_range) != ADDRESS_RANGE:
                raise GuardIdentityError("BSG1 address map mismatch", diagnostics=snapshot)
            if self.carrier_mode is CarrierMode.EXPANSION_BOARD:
                if self.manifest is None:
                    raise GuardIdentityError("expansion_board requires a separate HWH/manifest")
                manifest_mode = self.manifest.get("carrier_mode", self.manifest.get("mode"))
                if manifest_mode not in ("expansion_board", 1, "1"):
                    raise GuardIdentityError("expansion_board manifest does not advertise expansion_board mode")
                confirmed = self.manifest.get("hardware_confirmed", self.manifest.get("confirmed"))
                if confirmed is not True:
                    raise GuardIdentityError("expansion_board manifest requires explicit confirmed=true")
                hwh = self.manifest.get("hwh") if isinstance(self.manifest.get("hwh"), Mapping) else {}
                manifest_base = self.manifest.get("guard_base_address", self.manifest.get("base_address", hwh.get("base_address")))
                manifest_range = self.manifest.get("guard_address_range", self.manifest.get("address_range", hwh.get("address_range")))
                try:
                    manifest_base = int(manifest_base, 0) if isinstance(manifest_base, str) else int(manifest_base)
                    manifest_range = int(manifest_range, 0) if isinstance(manifest_range, str) else int(manifest_range)
                except (TypeError, ValueError):
                    raise GuardIdentityError("expansion_board manifest lacks exact guard address identity")
                if manifest_base != BASE_ADDRESS or manifest_range != ADDRESS_RANGE:
                    raise GuardIdentityError("expansion_board HWH/manifest guard address mismatch")
            if self._validated_mode is None:
                self._validated_mode = snapshot["carrier_mode"]
            elif snapshot["carrier_mode"] != self._validated_mode:
                raise GuardIdentityError("carrier mode changed after identity validation", diagnostics=snapshot)
            return snapshot

    def arm(
        self,
        *,
        request_id: int,
        lease_cycles: int = 10_000_000,
        timeout_ms: int = 120_000,
        deadline_s: float = 2.0,
    ) -> int:
        with self._lock:
            self._ensure_open()
            self.validate_identity()
            request_id = _u32(request_id, "request_id", minimum=1)
            lease_cycles = _u32(lease_cycles, "lease_cycles", minimum=1_000_000)
            timeout_ms = _u32(timeout_ms, "timeout_ms", minimum=1_000)
            if lease_cycles > 100_000_000:
                raise ValueError("lease_cycles exceeds BSG1 maximum")
            if timeout_ms > 300_000:
                raise ValueError("timeout_ms exceeds BSG1 maximum")
            before = self.status()
            if before["active"] or before["motor_busy"]:
                raise GuardBusyError("BSG1 is already active or motor busy", diagnostics=before)
            if before["fault"]:
                raise GuardFault("BSG1 reports a fault", diagnostics=before)
            if request_id <= before["accepted_id"]:
                raise GuardBusyError("BSG1 request_id is not newer than accepted_id", diagnostics=before)
            self._active_request = request_id
            self._write("request_id", request_id)
            self._write("lease_cycles", lease_cycles)
            self._write("timeout_ms", timeout_ms)
            # A permit begins with a fresh non-zero heartbeat.  The value is
            # lifetime-unique, not a freely repeated toggle.
            self._next_heartbeat(write=True)
            self._write("command", COMMANDS["arm"])
            deadline = self._monotonic() + float(deadline_s)
            next_heartbeat = self._monotonic() + min(0.025, lease_cycles / CLOCK_HZ / 3.0)
            try:
                while True:
                    current = self.status()
                    if current["fault"]:
                        raise GuardFault("BSG1 faulted while arming", diagnostics=current)
                    if current["error"]:
                        raise GuardFault("BSG1 rejected ARM", diagnostics=current)
                    if current["active"]:
                        return request_id
                    if self._monotonic() >= deadline:
                        raise GuardTimeout("BSG1 ARM acknowledgement timed out", diagnostics=current)
                    now = self._monotonic()
                    if now >= next_heartbeat and current["active"]:
                        self._next_heartbeat(write=True)
                        next_heartbeat = now + min(0.025, lease_cycles / CLOCK_HZ / 3.0)
                    remaining = max(0.0, deadline - self._monotonic())
                    delay = min(self._poll_interval_s, remaining)
                    until_heartbeat = next_heartbeat - self._monotonic()
                    if until_heartbeat > 0:
                        delay = min(delay, until_heartbeat)
                    if delay > 0:
                        self._sleep(delay)
            except Exception:
                self._active_request = None
                raise

    def refresh(self) -> int:
        """Issue one unique heartbeat; callers own the polling deadline."""

        with self._lock:
            self._ensure_open()
            current = self.status()
            if self._active_request is None or not current["active"]:
                raise GuardError("cannot refresh an inactive BSG1 permit", diagnostics=current)
            if current["fault"]:
                raise GuardFault("cannot refresh a faulted BSG1 permit", diagnostics=current)
            return self._next_heartbeat(write=True)

    def revoke(self, *, deadline_s: float = 2.0) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            before = self.status()
            if before["active"] or before["fault"] or self._active_request is not None:
                self._write("command", COMMANDS["disarm"])
            deadline = self._monotonic() + float(deadline_s)
            while True:
                current = self.status()
                if not current["active"]:
                    self._active_request = None
                    return current
                if self._monotonic() >= deadline:
                    raise GuardTimeout("BSG1 revoke timed out", diagnostics=current)
                self._sleep(min(self._poll_interval_s, max(0.0, deadline - self._monotonic())))

    def clear_faults(self, *, deadline_s: float = 0.5) -> dict[str, Any]:
        """Acknowledge an idle fault without ARM, heartbeat, or motor writes.

        The original fault must be recorded by the caller first. A latched
        motor fault requires separate motor-health diagnosis; it is never
        hidden here by trying a potentially rejected AXI command.
        """
        if isinstance(deadline_s, bool) or not math.isfinite(deadline_s) or not 0 < deadline_s <= 2:
            raise ValueError('guard clear deadline must be finite in (0,2] seconds')
        with self._lock:
            self._ensure_open()
            before = self.validate_identity()
            if before['active'] or before['motor_busy'] or before['step_pin'] or not before['enable_pin']:
                raise GuardBusyError('guard clear requires inactive, motor idle, STEP low and disabled EN', diagnostics=before)
            if before['error'] == ERRORS['motor_fault']:
                raise GuardFault('unresolved motor fault requires separate health proof', diagnostics=before)
            if self.carrier_mode is CarrierMode.EXPANSION_BOARD and not before['field_power_good']:
                raise GuardFault('field power is not healthy', diagnostics=before)
            if not before['fault'] and before['error'] == 0:
                return before
            self._write('command', COMMANDS['clear'])
            deadline = self._monotonic() + deadline_s
            while True:
                current = self.status()
                if (not current['active'] and not current['fault'] and current['error'] == 0
                        and not current['motor_busy'] and not current['step_pin'] and current['enable_pin']):
                    self._active_request = None
                    return current
                if self._monotonic() >= deadline:
                    raise GuardTimeout('BSG1 fault clear acknowledgement timed out', diagnostics=current)
                self._sleep(min(self._poll_interval_s, max(0.0, deadline-self._monotonic())))

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            current = self.status()
            if current["active"]:
                self._write("command", COMMANDS["stop"])
            return self.status()

    def close(self) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                return {"closed": True, "already_closed": True}
            failure: Exception | None = None
            status: dict[str, Any] | None = None
            try:
                status = self.revoke()
            except Exception as exc:
                failure = exc
            try:
                closer = getattr(self.transport, "close", None)
                if callable(closer):
                    closer()
            except Exception as exc:
                failure = failure or exc
            self._closed = True
            if failure:
                raise failure
            return {"closed": True, "status": status}

    def _next_heartbeat(self, *, write: bool) -> int:
        previous = self._heartbeat_value
        if previous is None:
            # DISARM/STOP/CLEAR retain the hardware heartbeat value.  A new
            # controller must change it to establish a fresh permit.
            previous = self._read("heartbeat")
        candidate = (previous + 1) & UINT32_MAX
        if candidate == 0:
            candidate = 1
        if candidate in self._heartbeat_values:
            raise GuardError("BSG1 heartbeat value would be reused")
        if write:
            self._write("heartbeat", candidate)
            self._heartbeat_value = candidate
            self._heartbeat_values.add(candidate)
        return candidate


# Descriptive aliases used by board-side integrations.
BatchGuard = GuardController
BSG1Guard = GuardController


__all__ = [
    "ADDRESS_RANGE", "BASE_ADDRESS", "CAPABILITIES", "CLOCK_HZ", "COMMANDS",
    "CONTRACT_VERSION", "GuardBusyError", "GuardController", "GuardError",
    "GuardFault", "GuardIdentityError", "GuardTimeout", "HEARTBEAT_HALF_CYCLES",
    "REGISTERS", "STATUS_BITS", "BatchGuard", "BSG1Guard",
]
