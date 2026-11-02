"""Stage B board entry point with an offline-only default inspection.

``--stage inspect`` validates bytes, manifest, and HWH metadata without
importing PYNQ or loading an Overlay.  Signals/first-frame/static may either
explicitly ``--load`` with a current physical-power-off confirmation or
``--attach`` to an already loaded Overlay.  Motion stages only attach and
require fresh motion/driver confirmations.  This entry point never falls back
to Mock hardware and never performs a hidden retry.
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

from structured_light.motion.pynq_runtime import (  # noqa: E402
    OVERLAY_MODE,
    RuntimeAdmissionError,
    SETTLE_PROTOCOL_IDS,
    create_stage_b_runtime,
    verify_stage_b_artifacts,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        choices=("inspect", "signals", "first-frame", "static", "raw-static", "profile-static", "recovery", "settle", "single", "sequence", "close"),
        default="inspect",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--load", action="store_true", help="explicitly load the signed Overlay after power-off confirmation")
    parser.add_argument("--attach", action="store_true", help="attach to an already loaded signed Overlay without downloading")
    parser.add_argument("--power-off-confirmation", type=Path, default=None)
    parser.add_argument("--loaded-overlay-receipt", type=Path, default=None)
    parser.add_argument("--motion-confirmation", type=Path, default=None)
    parser.add_argument("--driver-proof", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--settle-config", type=Path, default=None)
    parser.add_argument("--settle-protocol", choices=SETTLE_PROTOCOL_IDS, default="legacy_3s_v1")
    parser.add_argument("--count", type=int, default=None)
    parser.add_argument("--target", default="pynq_z2")
    parser.add_argument("--installation", default="stage_b_candidate")
    parser.add_argument(
        "--settle-roi", "--roi", dest="settle_roi", nargs=4, type=int,
        metavar=("X0", "Y0", "X1", "Y1"), default=(0, 0, 752, 480),
        help="settling texture ROI (the legacy --roi name is an alias)",
    )
    parser.add_argument(
        "--stripe-roi", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"),
        default=(0, 0, 752, 480), help="P2 stripe extraction ROI",
    )
    parser.add_argument("--threshold", type=int, default=30, help="P2 stripe threshold")
    parser.add_argument("--no-display", action="store_true")
    parser.add_argument("--mode", choices=("real", "delegated"), default="real")
    parser.add_argument("--recovery-runner", type=Path, default=None)
    parser.add_argument("--recovery-args", nargs=argparse.REMAINDER, default=())
    return parser


def inspect_payload(manifest: str | Path) -> dict[str, Any]:
    artifacts = verify_stage_b_artifacts(manifest)
    return {
        "stage": "inspect",
        "overlay_loaded": False,
        "motion_started": False,
        "network_accessed": False,
        "hardware_evidence": "offline_contract_only",
        "overlay": artifacts["overlay"],
        "overlay_mode": artifacts["overlay_mode"],
        "carrier_mode": artifacts["carrier_mode"],
        "part": artifacts["part"],
        "frame": [artifacts["frame_width"], artifacts["frame_height"]],
        "addresses": artifacts["addresses"],
        "files": artifacts["files"],
        "preboard_passed": artifacts["preboard_passed"],
        "hwh_motion": artifacts["hwh_motion"],
    }


def _delegated_recovery(args: argparse.Namespace) -> dict[str, Any]:
    if args.mode != "delegated":
        raise RuntimeAdmissionError("recovery delegation requires explicit --mode delegated")
    if args.recovery_runner is None:
        raise RuntimeAdmissionError("recovery delegation requires --recovery-runner")
    runner = args.recovery_runner.resolve()
    if not runner.is_file():
        raise FileNotFoundError(runner)
    artifacts = verify_stage_b_artifacts(args.manifest)
    if artifacts["overlay_mode"] != OVERLAY_MODE or runner.name != "p2_closeout_board.py":
        raise RuntimeAdmissionError("delegated recovery requires the compatible P2 profile-HDMI runner")
    # The source-compatible existing P2 runner remains the owner of its exact
    # recovery policy.  Do not duplicate or silently retry its operations.
    command = [sys.executable, str(runner), *args.recovery_args]
    completed = subprocess.run(command, cwd=str(PROJECT_ROOT), check=False, capture_output=True, text=True)
    return {
        "stage": "recovery",
        "delegated": True,
        "source_compatible": True,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "motion_started": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.stage == "inspect":
        if args.load:
            raise RuntimeAdmissionError("inspect is offline-only; use a real stage for explicit --load")
        print(json.dumps(inspect_payload(args.manifest), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.stage == "recovery" and args.mode == "delegated":
        payload = _delegated_recovery(args)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload["returncode"] == 0 else int(payload["returncode"])
    if args.mode != "real":
        raise RuntimeAdmissionError("real Stage B stages require --mode real; no Mock fallback exists")
    load_stages = {"signals", "first-frame", "static", "raw-static", "profile-static"}
    motion_stages = {"settle", "single", "sequence"}
    if args.stage in load_stages and not args.load and not args.attach:
        raise RuntimeAdmissionError("signals/first-frame/static require --load or --attach")
    if args.stage in motion_stages and (args.load or not args.attach):
        raise RuntimeAdmissionError("motion stages require --attach to an already loaded Overlay; --load is poweroff-only")
    if args.stage == "close" and not args.attach:
        raise RuntimeAdmissionError("close requires --attach to the loaded Overlay owner")
    runtime = None
    try:
        runtime = create_stage_b_runtime(
            args.manifest, stage=args.stage, load=args.load, attach=args.attach,
            power_off_confirmation=args.power_off_confirmation,
            loaded_overlay_receipt=args.loaded_overlay_receipt,
            motion_confirmation=args.motion_confirmation,
            driver_proof=args.driver_proof,
            output=args.output, settle_config_path=args.settle_config,
            settle_protocol=args.settle_protocol,
            target=args.target, installation=args.installation, roi=tuple(args.settle_roi),
            stripe_roi=tuple(args.stripe_roi), threshold=args.threshold,
            no_display=args.no_display,
        )
        if args.stage == "signals":
            payload = runtime.signals()
        elif args.stage == "first-frame":
            result = runtime.first_frame()
            payload = {"stage": "first-frame", "request_id": result.request_id, "epoch": result.epoch, "motion_started": False}
        elif args.stage in {"static", "profile-static", "raw-static"}:
            mode = "raw" if args.stage == "raw-static" else "profile"
            payload = runtime.static(count=args.count if args.count is not None else 1000, mode=mode)
        elif args.stage == "settle":
            payload = runtime.settle_diagnostic(positions=10)
        elif args.stage == "single":
            result = runtime.single()
            task_receipt = getattr(runtime.session, "last_task_receipt", None)
            payload = {
                "stage": "single", "result": result.as_dict(), "motion_started": True,
                "task_receipt": task_receipt,
                "motion_configuration": task_receipt.get("motion_config") if isinstance(task_receipt, dict) else None,
            }
        elif args.stage == "sequence":
            results = runtime.sequence()
            task_receipt = getattr(runtime.session, "last_task_receipt", None)
            payload = {
                "stage": "sequence", "count": len(results),
                "results": [item.as_dict() for item in results], "motion_started": True,
                "task_receipt": task_receipt,
                "motion_configuration": task_receipt.get("motion_config") if isinstance(task_receipt, dict) else None,
            }
        elif args.stage == "recovery":
            payload = runtime.recovery()
        elif args.stage == "close":
            payload = {"stage": "close", **runtime.close()}
            runtime = None
        else:  # pragma: no cover - argparse constrains this
            raise ValueError(args.stage)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 1 if payload.get("passed") is False else 0
    finally:
        if runtime is not None:
            cleanup = runtime.close()
            if isinstance(cleanup, dict) and cleanup.get("closed") is False:
                raise RuntimeError("Stage B cleanup left an unconfirmed worker; DMA/HDMI ownership was retained")


if __name__ == "__main__":
    raise SystemExit(main())
