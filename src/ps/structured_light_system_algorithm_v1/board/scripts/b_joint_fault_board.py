"""B5 single-case contract and OFFLINE fault-hook model.

Real execution is deliberately refused before importing PYNQ or attaching: the
current runtime does not expose the owner-bound phase/deadline/lease hooks this
matrix requires. Model output can never count toward actual board acceptance.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src" / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "src" / "python"))

from structured_light.motion.lifecycle import GenerationGate

CASES = ("motion-stop", "motion-lease", "settle-cancel", "capture-timeout", "display-failure")
PHASES = dict(zip(CASES, ("MOVING", "MOVING", "SETTLING", "CAPTURING", "DISPLAYING")))
ACTIONS = dict(zip(CASES, ("owner_stop", "suspend_guard_refresh", "cancel_generation", "shorten_inflight_deadline", "raise_display_error")))
BLOCKING_INTERFACES = (
    "MotionSession has no owner-bound phase observer or cancellation API",
    "MotionSession._await_future refreshes guard unconditionally; no bounded per-task lease policy hook",
    "P2 start_capture has no public accepted-inflight notification paired to a mutable owner deadline",
    "No MotionSession recovery transition confirms dead workers then consumes local recovery token and verifies a no-motion pair before close",
)


class JointFaultError(RuntimeError):
    pass


class JointFaultHookModel:
    """Executable hook DESIGN only; produces intentions, never hardware calls.

    A future real adapter must dispatch these intentions on the current owner,
    through its existing generation gate, and save independent hardware readback.
    Matching a phase label alone cannot trigger an injection.
    """

    def __init__(self, case: str, *, owner: str, gate: GenerationGate) -> None:
        if case not in CASES or not owner or not gate.active:
            raise JointFaultError("one valid case and an active owned generation are required")
        self.case, self.owner, self.gate = case, owner, gate
        self.generation = gate.generation
        self.injected = False
        self.started = False
        self.events: list[dict[str, Any]] = []

    def observe(self, event: Mapping[str, Any], *, owner: str, generation: int) -> str | None:
        if owner != self.owner or generation != self.generation:
            raise JointFaultError("fault hook owner/generation mismatch")
        return self.gate.call(generation, self._observe, dict(event))

    def _observe(self, event: dict[str, Any]) -> str | None:
        if self.injected:
            raise JointFaultError("injection is terminal; no automatic continuation")
        kind = event.get("event")
        if kind == "motion_started":
            if self.started:
                raise JointFaultError("second START refused; each invocation has a 320 pulse cap")
            if event.get("pulse_limit") != 320 or event.get("rpm") != 5:
                raise JointFaultError("B5 model admits exactly 320 pulses at 5 RPM")
            self.started = True
        self.events.append(deepcopy(event))
        if event.get("phase") != PHASES[self.case] or not self.started:
            return None
        motor = event.get("motor", {})
        if self.case in {"motion-stop", "motion-lease"}:
            emitted = motor.get("emitted")
            ready = (motor.get("busy") is True and type(emitted) is int and 0 < emitted < 320
                     and type(motor.get("accepted_id")) is int)
        elif self.case == "settle-cancel":
            ready = (motor.get("done") is True and motor.get("emitted") == 320
                     and motor.get("step_pin") == 0 and motor.get("enable_pin") == 1
                     and event.get("formal_pair_started") is False
                     and event.get("settle_wait_active") is True)
        elif self.case == "capture-timeout":
            capture = event.get("capture", {})
            ready = (capture.get("inflight") is True and capture.get("accepted") is True
                     and type(capture.get("request_id")) is int and capture["request_id"] >= 0
                     and type(capture.get("epoch")) is int and capture["epoch"] >= 0
                     and event.get("settle_passed") is True)
        else:
            capture = event.get("capture", {})
            ready = (capture.get("comparison_passed") is True and capture.get("persisted") is True
                     and type(capture.get("request_id")) is int and capture["request_id"] >= 0
                     and type(capture.get("epoch")) is int and capture["epoch"] >= 0)
        if not ready:
            return None
        self.injected = True
        return ACTIONS[self.case]


def model_case(case: str) -> dict[str, Any]:
    """Replay a deterministic phase boundary; no capture/motor is executed."""
    gate = GenerationGate()
    generation = gate.begin()
    hook = JointFaultHookModel(case, owner="offline-model-owner", gate=gate)
    hook.observe({"event": "motion_started", "phase": "READY", "pulse_limit": 320, "rpm": 5},
                 owner=hook.owner, generation=generation)
    event: dict[str, Any] = {"event": "phase_observation", "phase": PHASES[case]}
    if case.startswith("motion-"):
        event["motor"] = {"busy": True, "emitted": 7, "accepted_id": 0x4D000001}
    elif case == "settle-cancel":
        event.update(motor={"done": True, "emitted": 320, "step_pin": 0, "enable_pin": 1},
                     formal_pair_started=False, settle_wait_active=True)
    elif case == "capture-timeout":
        event.update(capture={"inflight": True, "accepted": True, "request_id": 7, "epoch": 2}, settle_passed=True)
    else:
        event["capture"] = {"comparison_passed": True, "persisted": True, "request_id": 7, "epoch": 2}
    action = hook.observe(event, owner=hook.owner, generation=generation)
    gate.revoke(timeout_s=0)
    return {"schema": "b_joint_fault_model_v1", "case": case, "hardware_evidence": "offline_model_only",
            "action_intention": action, "events": hook.events, "model_triggered": hook.injected,
            "actual_motion_start_writes": 0, "b_joint_fault_verified": False,
            "real_runner_ready": False, "blocking_interfaces": list(BLOCKING_INTERFACES)}


def _write_new(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(dict(payload), stream, indent=2, sort_keys=True)
        stream.write("\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--mode", choices=("inspect", "model", "real"), default="inspect")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attach", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--loaded-overlay-receipt", type=Path)
    parser.add_argument("--motion-confirmation", type=Path)
    parser.add_argument("--driver-proof", type=Path)
    parser.add_argument("--settle-config", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.mode == "model":
        payload = model_case(args.case)
    else:
        payload = {"schema": "b_joint_fault_readiness_v1", "case": args.case, "mode": args.mode,
                   "hardware_evidence": "offline_contract_only", "real_runner_ready": False,
                   "b_joint_fault_verified": False, "actual_motion_start_writes": 0,
                   "board_accessed": False, "blocking_interfaces": list(BLOCKING_INTERFACES)}
        if args.mode == "real":
            # Refuse BEFORE attachment, proof parsing, PYNQ import or MMIO.
            payload.update(refused=True, error="B5 real injection interface is not implemented; do not energize/run this entry point")
    _write_new(args.output, payload)
    return 2 if args.mode == "real" else 0


if __name__ == "__main__":
    raise SystemExit(main())
