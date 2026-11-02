"""Stage B motion/capture CLI.

The default command is a read-only contract inspection.  PYNQ and Overlay are
loaded only by a future explicitly injected real adapter; this script never
loads a board image as part of ``inspect`` or mock/replay stages.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "python"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from structured_light.motion import MotionSession, SettleConfig, diagnostic_settle_schedule, estimate_settle
from structured_light.motion.mock import MockMotionHardware


def _contract(name: str) -> dict[str, Any]:
    path = PROJECT_ROOT / "src" / "contracts" / name
    return json.loads(path.read_text(encoding="utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", choices=("inspect", "settle", "single", "sequence", "static", "recovery"),
        default="inspect", help="explicit stage; inspect is read-only and the default",
    )
    parser.add_argument("--mode", choices=("mock", "replay", "real"), default="mock")
    parser.add_argument("--positions", type=int, default=10)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--roi", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"))
    parser.add_argument("--target", default="pynq_z2")
    parser.add_argument("--installation", default="stage_b_candidate")
    parser.add_argument("--profile", default="SLP2")
    parser.add_argument("--replay", type=Path, default=None, help="explicit replay source for replay mode")
    parser.add_argument("--p2-script", type=Path, default=None, help="explicit existing P2 script for recovery delegation")
    parser.add_argument("--p2-args", nargs=argparse.REMAINDER, default=())
    parser.add_argument("--failure", action="append", default=(), help="offline mock failure injection")
    return parser


def inspect_payload(*, mode: str = "mock") -> dict[str, Any]:
    guard = _contract("b_motion_session.json")
    motor = _contract("motor_interface.json")
    return {
        "stage": "inspect",
        "mode": mode,
        "overlay_loaded": False,
        "motion_started": False,
        "network_accessed": False,
        "hardware_evidence": "offline_contract_only",
        "carrier_mode": "direct_wire",
        "expansion_mode_requires_separate_hwh_manifest": True,
        "guard": {
            "name": guard["name"],
            "version": guard["version"],
            "base_address": guard["base_address"],
            "carrier_modes": guard["carrier_modes"],
            "heartbeat_half_cycles": guard["heartbeat_half_cycles"],
            "host_poll_max_s": guard["host_poll_max_s"],
            "default_lease_cycles": guard["default_lease_cycles"],
            "default_timeout_ms": guard["default_timeout_ms"],
        },
        "motor": {
            "version": motor["version"],
            "axi_base_address": motor["axi_base_address"],
            "capabilities": motor["capabilities"],
            "board_admission_requires": motor["board_admission_requires"],
        },
        "angle": {"raw": 0, "valid": False, "calibrated": False},
    }


def _frames(height: int = 32, width: int = 40):
    import numpy as np

    y, x = np.mgrid[:height, :width]
    base = ((x * 7 + y * 11 + ((x * y) % 13) * 3) % 251).astype(np.uint8)
    return [base.copy() for _ in range(23)]


def _build_mock(args: argparse.Namespace) -> tuple[MockMotionHardware, MotionSession]:
    output = args.output or (PROJECT_ROOT / "board" / "results" / "b_motion_mock")
    hardware = MockMotionHardware(output=output, width=40, height=32, failures=args.failure)
    components = hardware.components()
    session = MotionSession(
        **components,
        output=output / "records",
        settle_roi=tuple(args.roi) if args.roi else (0, 0, 40, 32),
        target=args.target,
        installation=args.installation,
        profile=args.profile,
        mode="offline_mock",
    )
    return hardware, session


def _run_delegated_p2(args: argparse.Namespace) -> dict[str, Any]:
    if args.p2_script is None:
        raise RuntimeError("recovery delegation requires explicit --p2-script")
    script = args.p2_script.resolve()
    if not script.is_file():
        raise FileNotFoundError(script)
    command = [sys.executable, str(script), *args.p2_args]
    completed = subprocess.run(command, cwd=str(PROJECT_ROOT), check=False, capture_output=True, text=True)
    return {
        "stage": "recovery",
        "delegated": True,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "motion_started": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.stage == "inspect":
        print(json.dumps(inspect_payload(mode=args.mode), indent=2, sort_keys=True))
        return 0
    if args.mode == "real":
        # Keep PYNQ import lazy and fail closed until the parent supplies an
        # exact HWH/manifest adapter.  No import or Overlay load occurs here.
        raise RuntimeError("real mode requires an injected PYNQ HWH/manifest adapter; no board operation was attempted")
    if args.stage == "recovery" and args.p2_script is not None:
        print(json.dumps(_run_delegated_p2(args), indent=2, sort_keys=True))
        return 0
    if args.mode == "replay":
        raise RuntimeError("replay mode requires a future replay hardware factory; use --mode mock for offline execution")
    if args.positions < 1:
        raise ValueError("--positions must be positive")
    hardware, session = _build_mock(args)
    frames = _frames()
    try:
        if args.stage == "settle":
            estimate = estimate_settle(
                frames, roi=session.settle_roi, config=session.settle_config,
                config_hash=session.settle_config_hash,
            )
            payload = {
                "stage": "settle", "estimate": estimate.as_dict(),
                "schedule_ms": list(diagnostic_settle_schedule(positions=args.positions)),
                "positions": args.positions, "motion_started": False,
                "hardware_evidence": "offline_mock",
            }
        elif args.stage == "single":
            result = session.run_single(settle_frames=frames)
            payload = {"stage": "single", "result": result, "motion_started": True, "hardware_evidence": "offline_mock"}
        elif args.stage == "sequence":
            results = session.run_sequence(positions=args.positions, settle_frames=frames)
            payload = {"stage": "sequence", "count": len(results), "results": results, "motion_started": True, "hardware_evidence": "offline_mock"}
        elif args.stage == "static":
            result = session.run_static()
            payload = {"stage": "static", "result": result, "motion_started": False, "hardware_evidence": "offline_mock"}
        elif args.stage == "recovery":
            payload = session.run_recovery()
            payload["hardware_evidence"] = "offline_mock"
        else:  # pragma: no cover - argparse constrains this
            raise ValueError(args.stage)
        print(json.dumps(payload, indent=2, sort_keys=True, default=lambda value: value.as_dict() if hasattr(value, "as_dict") else str(value)))
        return 0
    finally:
        try:
            session.close()
        except Exception as exc:
            # The primary stage result remains on stdout; close failures are
            # reported once and are never retried in a second operation.
            print(json.dumps({"cleanup_error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
