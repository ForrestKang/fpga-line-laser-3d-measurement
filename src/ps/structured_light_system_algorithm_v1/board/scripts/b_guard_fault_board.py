"""Bounded BSG1/MFP1 fault-propagation acceptance entry.

The preset contains exactly twenty finite cases: five STOP cases, five BSG1
host-lease expiries, five BSG1 task-timeout cases, and five MFP1 heartbeat
fault-propagation cases.  The field-power proof must be a current read-only
USB measurement below 6 V; this entry never enables field power and never
issues MFP1 START.  It reports guard logic evidence only and deliberately
does not claim joint or physical motion-fault acceptance.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "python"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from structured_light.motion.guard import COMMANDS as GUARD_COMMANDS  # noqa: E402
from structured_light.motion.guard import GuardController, GuardError  # noqa: E402
from structured_light.motion.lifecycle import HardwareProcessLock  # noqa: E402
from structured_light.motion.motor_control import REGISTERS as MFP_REGISTERS  # noqa: E402
from structured_light.motion.motor_control import COMMANDS as MFP_COMMANDS  # noqa: E402
from structured_light.motion.pynq_runtime import (  # noqa: E402
    MOTION_ADDRESSES,
    OVERLAY_MODE,
    MMIO32,
    RuntimeAdmissionError,
    verify_loaded_overlay_receipt,
    verify_stage_b_artifacts,
)


GUARD_ERRORS = {
    "none": 0, "invalid_config": 1, "lease_expired": 2, "cancelled": 3,
    "task_timeout": 4, "motor_fault": 5, "field_power_lost": 6,
    "duplicate_request": 7, "invalid_command": 8,
}
MFP_ERRORS = {
    "none": 0, "invalid_config": 1, "not_armed": 2, "duplicate_request": 3,
    "heartbeat_timeout": 4, "motion_timeout": 5, "config_changed_busy": 6,
    "invalid_command": 7,
}
MFP_REGISTERS = dict(MFP_REGISTERS)
MFP_COMMANDS = dict(MFP_COMMANDS)
GUARD_REGISTERS = {
    "version": 0x00, "status": 0x04, "command": 0x08, "request_id": 0x0C,
    "heartbeat": 0x10, "lease_cycles": 0x14, "timeout_ms": 0x18,
    "error": 0x1C, "accepted_id": 0x20, "carrier_mode": 0x24,
    "heartbeat_half_cycles": 0x28, "clock_hz": 0x2C, "capabilities": 0x30,
}


class GuardFaultBoardError(RuntimeError):
    pass


def _read_json(value: str | Path | Mapping[str, Any], label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        loaded = json.loads(Path(value).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuardFaultBoardError(f"cannot read {label}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise GuardFaultBoardError(f"{label} must contain a JSON object")
    return loaded


def verify_power_off_proof(value: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    proof = _read_json(value, "USB power-off proof")
    voltage = proof.get("bus_voltage_v", proof.get("voltage_v"))
    try:
        voltage_value = float(voltage)
    except (TypeError, ValueError) as exc:
        raise GuardFaultBoardError("USB power-off proof must contain bus_voltage_v") from exc
    if proof.get("read_only") is not True or proof.get("power_off_confirmed") is not True:
        raise GuardFaultBoardError("USB proof must be an explicit read-only power-off confirmation")
    if not 0.0 <= voltage_value < 6.0:
        raise GuardFaultBoardError("USB power-off proof must measure below 6 V")
    return {**proof, "bus_voltage_v": voltage_value}


class MockGuardPlant:
    """Small register-level BSG1 model using the RTL contract values."""

    base_address = 0x43C50000
    address_range = 0x10000

    def __init__(self, *, tick_cycles: int = 100_000) -> None:
        self.tick_cycles = tick_cycles
        self.reg = {
            GUARD_REGISTERS["version"]: 0x42534731,
            GUARD_REGISTERS["carrier_mode"]: 0,
            GUARD_REGISTERS["heartbeat_half_cycles"]: 500_000,
            GUARD_REGISTERS["clock_hz"]: 100_000_000,
            GUARD_REGISTERS["capabilities"]: 7,
            GUARD_REGISTERS["accepted_id"]: 0,
        }
        self.active = False
        self.fault = False
        self.error = 0
        self.request_id = 0
        self.lease_remaining = 0
        self.task_remaining = 0
        self.motor_fault = False
        self.start_writes = 0

    def _tick(self) -> None:
        if not self.active:
            return
        self.lease_remaining -= self.tick_cycles
        self.task_remaining -= self.tick_cycles
        if self.motor_fault:
            self.active, self.fault, self.error = False, True, GUARD_ERRORS["motor_fault"]
        elif self.task_remaining <= 0:
            self.active, self.fault, self.error = False, True, GUARD_ERRORS["task_timeout"]
        elif self.lease_remaining <= 0:
            self.active, self.fault, self.error = False, True, GUARD_ERRORS["lease_expired"]

    def advance_cycles(self, cycles: int) -> None:
        if self.active:
            self.lease_remaining -= int(cycles)
            self.task_remaining -= int(cycles)
            self._tick()

    def read32(self, offset: int) -> int:
        if offset == GUARD_REGISTERS["status"]:
            self._tick()
            return (
                (1 if self.active else 0)
                | (2 if self.fault else 0)
                | (8 if self.active and self.motor_fault else 0)
                | (32 if self.active else 0)
                | (64)
            )
        if offset == GUARD_REGISTERS["error"]:
            return self.error
        if offset == GUARD_REGISTERS["request_id"]:
            return self.request_id
        if offset == GUARD_REGISTERS["lease_cycles"]:
            return max(0, self.lease_remaining)
        if offset == GUARD_REGISTERS["timeout_ms"]:
            return max(0, self.task_remaining // 100_000)
        return int(self.reg.get(offset, 0))

    def write32(self, offset: int, value: int) -> None:
        value = int(value) & 0xFFFF_FFFF
        if offset == GUARD_REGISTERS["request_id"]:
            self.request_id = value
        elif offset == GUARD_REGISTERS["lease_cycles"]:
            self.lease_remaining = value
        elif offset == GUARD_REGISTERS["timeout_ms"]:
            self.task_remaining = value * 100_000
        elif offset == GUARD_REGISTERS["heartbeat"]:
            if self.active:
                self.lease_remaining = self.reg.get(GUARD_REGISTERS["lease_cycles"], 0)
        elif offset == GUARD_REGISTERS["command"]:
            self._command(value)
        else:
            self.reg[offset] = value

    def _command(self, value: int) -> None:
        if value == GUARD_COMMANDS["arm"]:
            self.active = True
            self.fault = False
            self.error = GUARD_ERRORS["none"]
            self.reg[GUARD_REGISTERS["lease_cycles"]] = max(self.lease_remaining, 1)
        elif value == GUARD_COMMANDS["stop"]:
            self.active = False
            if not self.fault:
                self.error = GUARD_ERRORS["cancelled"]
        elif value == GUARD_COMMANDS["disarm"]:
            self.active = False
            if not self.fault:
                self.error = GUARD_ERRORS["none"]
        elif value == GUARD_COMMANDS["clear"]:
            if not self.active and not self.motor_fault:
                self.fault = False
                self.error = GUARD_ERRORS["none"]

    def close(self) -> None:
        return None


class MockMFP1Model:
    """MFP1 register model for the known invalid-command boundary."""

    base_address = 0x43C40000
    address_range = 0x10000

    def __init__(self) -> None:
        self.reg = {MFP_REGISTERS["version"]: 0x4D465031, MFP_REGISTERS["clock_hz"]: 100_000_000, MFP_REGISTERS["capabilities"]: 7}
        self.error = 0
        self.motor_fault = False
        self.start_writes = 0
        self.ready_to_arm = False
        self.armed = False
        self.writes = []

    def read32(self, offset: int) -> int:
        if offset == MFP_REGISTERS["error"]:
            return self.error
        if offset == MFP_REGISTERS["status"]:
            return (int(self.motor_fault) << 4) | (int(self.ready_to_arm) << 5) | (int(self.armed) << 1)
        return int(self.reg.get(offset, 0))

    def write32(self, offset: int, value: int) -> None:
        value = int(value) & 0xFFFF_FFFF
        self.writes.append((offset, value))
        if offset == MFP_REGISTERS["command"]:
            if value == MFP_COMMANDS["start"]:
                self.start_writes += 1
            elif value == MFP_COMMANDS["disarm"]:
                self.ready_to_arm = True
                self.armed = False
                self.error = 0
            elif value == MFP_COMMANDS["arm"]:
                if not self.ready_to_arm or self.armed:
                    self.error = MFP_ERRORS["invalid_command"]
                    raise GuardFaultBoardError("MFP1 ARM requires prior DISARM/readback")
                self.ready_to_arm = False
                self.armed = True
                self.error = 0
            elif value not in MFP_COMMANDS.values():
                self.error = MFP_ERRORS["invalid_command"]
        elif offset == MFP_REGISTERS["heartbeat"]:
            self.reg[offset] = value
        else:
            self.reg[offset] = value

    def close(self) -> None:
        return None


def _clear_idle_guard(guard: GuardController) -> dict[str, Any]:
    status = guard.status()
    if status["active"] or status["motor_busy"]:
        raise GuardFaultBoardError("CLEAR attempted while BSG1 was not idle")
    guard.transport.write32(GUARD_REGISTERS["command"], GUARD_COMMANDS["clear"])
    after = guard.status()
    if after["fault"] or after["error"]:
        raise GuardFaultBoardError("BSG1 CLEAR did not return idle error-free state")
    return {"before": status, "after": after}


def _mock_case(kind: str, index: int) -> dict[str, Any]:
    guard_plant = MockGuardPlant()
    guard = GuardController(guard_plant, carrier_mode="direct_wire", poll_interval_s=0.001)
    request_id = 0xB5000000 + index
    result: dict[str, Any] = {
        "index": index, "kind": kind, "arm_count": 0,
        "motion_start_writes": 0, "passed": False,
    }
    try:
        result["arm_request_id"] = guard.arm(
            request_id=request_id,
            lease_cycles=1_000_000 if kind == "host_lease_expiry" else 100_000_000,
            timeout_ms=1_000 if kind == "task_timeout" else 300_000,
        )
        result["arm_count"] = 1
        if kind == "stop":
            guard.stop()
            result["expected_error"] = GUARD_ERRORS["cancelled"]
        elif kind == "host_lease_expiry":
            guard_plant.advance_cycles(1_000_001)
            result["expected_error"] = GUARD_ERRORS["lease_expired"]
        elif kind == "task_timeout":
            guard_plant.advance_cycles(100_000_001)
            result["expected_error"] = GUARD_ERRORS["task_timeout"]
        elif kind == "mfp1_fault_propagation":
            mfp = MockMFP1Model()
            _mfp_arm_without_start(mfp, request_id=0x4D600000 + index)
            # MFP1 heartbeat expiry is the RTL-confirmed no-START fault path.
            mfp.motor_fault = True
            guard_plant.motor_fault = mfp.motor_fault
            guard_plant.advance_cycles(1)
            result["mfp_error"] = MFP_ERRORS["heartbeat_timeout"]
            result["expected_error"] = GUARD_ERRORS["motor_fault"]
            result["motion_start_writes"] = mfp.start_writes
        else:  # pragma: no cover - matrix construction is explicit
            raise ValueError(kind)
        snapshot = guard.status()
        result["status"] = snapshot
        result["passed"] = (
            snapshot["active"] is False
            and snapshot["error"] == result["expected_error"]
            and result["motion_start_writes"] == 0
        )
        if not result["passed"]:
            raise GuardFaultBoardError(f"unexpected BSG1 result for {kind}: {snapshot}")
        if kind == "mfp1_fault_propagation":
            # Clear the source MFP1 fault first; BSG1 CLEAR is legal only once
            # the propagated motor-fault input has returned safe.
            guard_plant.motor_fault = False
        result["clear"] = _clear_idle_guard(guard)
        return result
    finally:
        try:
            guard.revoke()
        finally:
            guard.close()


def run_mock_fault_matrix(*, power_proof: Mapping[str, Any] | None = None) -> dict[str, Any]:
    proof = verify_power_off_proof(power_proof or {
        "read_only": True, "power_off_confirmed": True, "bus_voltage_v": 0.0,
        "source": "mock_usb_read_only",
    })
    cases: list[dict[str, Any]] = []
    kinds = ("stop", "host_lease_expiry", "task_timeout", "mfp1_fault_propagation")
    for index in range(1, 21):
        case = _mock_case(kinds[(index - 1) // 5], index)
        cases.append(case)
    return {
        "schema": "b_guard_fault_matrix_v1", "case_count": len(cases),
        "cases": cases, "guard_logic_verified20": len(cases) == 20 and all(item["passed"] for item in cases),
        "joint_motion_fault_verified": False, "physical_motion_verified": False,
        "motion_start_writes": sum(int(item.get("motion_start_writes", 0)) for item in cases),
        "power_off_proof": proof, "hardware": "offline_mock",
    }


def _write_no_overwrite(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise GuardFaultBoardError(f"refusing to overwrite existing receipt {path}")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")
        stream.flush()
        import os
        os.fsync(stream.fileno())


def run_real_fault_matrix(manifest: str | Path, receipt: str | Path | Mapping[str, Any], power_proof: str | Path | Mapping[str, Any], output: str | Path) -> dict[str, Any]:
    """Attach and run the same finite cases; real hardware remains 12V-off."""

    artifacts = verify_stage_b_artifacts(manifest)
    proof = verify_power_off_proof(power_proof)
    lock = HardwareProcessLock(label="b_guard_fault_board")
    lock.acquire()
    overlay = None
    try:
        try:
            from pynq import MMIO, Overlay  # type: ignore
        except ImportError as exc:
            raise GuardFaultBoardError("real guard fault stage requires PYNQ on the target") from exc
        overlay = Overlay(artifacts["bit"], download=False)
        verify_loaded_overlay_receipt(receipt, artifacts=artifacts, overlay=overlay)
        class RecordingMMIO:
            def __init__(self, raw: Any) -> None:
                self.raw, self.writes = raw, []

            def read(self, offset: int) -> int:
                return int(self.raw.read(offset))

            def write(self, offset: int, value: int) -> None:
                self.writes.append((int(offset), int(value) & 0xFFFF_FFFF))
                self.raw.write(offset, value)

        guard_raw = RecordingMMIO(MMIO(MOTION_ADDRESSES["bsg1_guard"], 0x10000))
        mfp_raw = RecordingMMIO(MMIO(MOTION_ADDRESSES["motor_controller"], 0x10000))
        guard = GuardController(MMIO32(guard_raw, base_address=MOTION_ADDRESSES["bsg1_guard"], name="BSG1"), carrier_mode="direct_wire", manifest=_read_json(manifest, "manifest"))
        guard.validate_identity()
        # MFP1 is read back for ABI identity; the fault preset never writes START.
        motor = MMIO32(mfp_raw, base_address=MOTION_ADDRESSES["motor_controller"], name="MFP1")
        if motor.read32(MFP_REGISTERS["version"]) != 0x4D465031:
            raise GuardFaultBoardError("MFP1 VERSION mismatch")
        cases: list[dict[str, Any]] = []
        case_dir = Path(output).with_suffix("").with_name(Path(output).stem + "_cases")
        case_dir.mkdir(parents=True, exist_ok=False)
        request_seed = int(guard.status()["accepted_id"])
        kinds = ("stop", "host_lease_expiry", "task_timeout", "mfp1_fault_propagation")
        for index in range(1, 21):
            kind = kinds[(index - 1) // 5]
            case = {"index": index, "kind": kind, "arm_count": 0, "motion_start_writes": 0, "passed": False}
            try:
                guard.arm(
                    request_id=((request_seed + index) & 0xFFFFFFFF) or 1,
                    lease_cycles=1_000_000 if kind == "host_lease_expiry" else 100_000_000,
                    timeout_ms=1_000 if kind == "task_timeout" else 300_000,
                )
                case["arm_count"] = 1
                if kind == "stop":
                    guard.stop()
                    expected = GUARD_ERRORS["cancelled"]
                elif kind == "host_lease_expiry":
                    expected = GUARD_ERRORS["lease_expired"]
                elif kind == "task_timeout":
                    expected = GUARD_ERRORS["task_timeout"]
                elif kind == "mfp1_fault_propagation":
                    _mfp_arm_without_start(motor, request_id=0x4D600000 + index)
                    expected = GUARD_ERRORS["motor_fault"]
                else:  # pragma: no cover
                    raise ValueError(kind)
                snapshot = _wait_guard_error(guard, expected)
                case["status"] = snapshot
                case["expected_error"] = expected
                case["motion_start_writes"] = sum(value == MFP_COMMANDS["start"] for offset, value in mfp_raw.writes)
                if case["motion_start_writes"] or snapshot["error"] != expected or snapshot["active"]:
                    raise GuardFaultBoardError(f"unexpected real BSG1 case result: {snapshot}")
                if snapshot["step_pin"] or snapshot["heartbeat_pin"] or not snapshot["enable_pin"]:
                    raise GuardFaultBoardError(f"fault did not establish safe direct-wire pins: {snapshot}")
                if kind == "mfp1_fault_propagation":
                    motor.write32(MFP_REGISTERS["command"], MFP_COMMANDS["clear"])
                case["clear"] = _clear_idle_guard(guard)
                case["passed"] = True
                cases.append(case)
                _write_no_overwrite(case_dir / f"{index:02d}.json", case)
            except Exception as exc:
                case.update(error_type=type(exc).__name__, error=str(exc), passed=False)
                cases.append(case)
                _write_no_overwrite(case_dir / f"{index:02d}.json", case)
                try:
                    guard.revoke()
                finally:
                    raise
        return {
            "schema": "b_guard_fault_matrix_v1", "case_count": len(cases), "cases": cases,
            "guard_logic_verified20": len(cases) == 20 and all(item["passed"] for item in cases),
            "joint_motion_fault_verified": False, "physical_motion_verified": False,
            "motion_start_writes": sum(int(item.get("motion_start_writes", 0)) for item in cases),
            "power_off_proof": proof, "hardware": "real_pynq_z2_attach_only",
        }
    finally:
        lock.release()


def _wait_guard_error(guard: GuardController, expected: int, timeout_s: float = 2.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = guard.status()
        if latest["error"] == expected and not latest["active"]:
            return latest
        if expected == GUARD_ERRORS["task_timeout"] and latest["active"]:
            # Isolate task deadline from host lease expiry. The explicit
            # finite test owner refreshes only this case; no background daemon.
            try:
                guard.refresh()
            except GuardError:
                # The task can expire between the first status sample and
                # refresh's admission read. Accept only the expected terminal
                # state, never revive a permit or retry the test.
                terminal = guard.status()
                if terminal["error"] == expected and not terminal["active"]:
                    return terminal
                raise
        time.sleep(0.001)
    raise GuardFaultBoardError(f"bounded guard fault deadline expected={expected}, got={latest}")


def _mfp_arm_without_start(motor: MMIO32, *, request_id: int) -> None:
    # The unchanged MFP1 powers up with ready_to_arm=0. Its actual host
    # contract requires explicit DISARM and readback before every ARM; a
    # denied ARM is AXI SLVERR and becomes SIGBUS on the PYNQ host.
    if motor.read32(MFP_REGISTERS["status"]) & 1:
        raise GuardFaultBoardError("MFP1 is busy; no fault injection admitted")
    motor.write32(MFP_REGISTERS["command"], MFP_COMMANDS["disarm"])
    if not motor.read32(MFP_REGISTERS["status"]) & (1 << 5):
        raise GuardFaultBoardError("MFP1 DISARM did not establish ready_to_arm")
    values = {
        "request_id": request_id, "pulse_count": 320, "period_cycles": 375_000,
        "high_cycles": 187_500, "dir_setup_cycles": 10_000_000,
        "enable_setup_cycles": 10_000_000, "lease_cycles": 1_000_000,
        "timeout_cycles": 100_000_000, "direction": 0,
    }
    for name, value in values.items():
        motor.write32(MFP_REGISTERS[name], value)
    motor.write32(MFP_REGISTERS["heartbeat"], 1)
    motor.write32(MFP_REGISTERS["command"], MFP_COMMANDS["arm"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("inspect", "run"), default="inspect")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--loaded-overlay-receipt", type=Path)
    parser.add_argument("--power-proof", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.stage == "inspect":
        artifacts = verify_stage_b_artifacts(args.manifest)
        _write_no_overwrite(args.output, {
            "stage": "inspect", "hardware": "offline_contract_only",
            "motion_start_writes": 0, "guard_logic_verified20": False,
            "joint_motion_fault_verified": False, "artifacts": artifacts,
        })
        return 0
    if args.loaded_overlay_receipt is None or args.power_proof is None:
        raise GuardFaultBoardError("run requires loaded-overlay receipt and USB power-off proof")
    try:
        result = run_real_fault_matrix(args.manifest, args.loaded_overlay_receipt, args.power_proof, args.output)
    except Exception as exc:
        _write_no_overwrite(args.output, {
            "stage": "run", "passed": False, "guard_logic_verified20": False,
            "joint_motion_fault_verified": False, "error_type": type(exc).__name__, "error": str(exc),
        })
        raise
    _write_no_overwrite(args.output, result)
    return 0 if result.get("guard_logic_verified20") else 1


if __name__ == "__main__":
    raise SystemExit(main())

