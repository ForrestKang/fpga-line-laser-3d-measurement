"""Bounded PYNQ runner for same-frame raw/Profile evidence.

The runner deliberately performs one START per request and never retries a
failed request.  ``P2Controller`` owns transport recovery and
``SessionRecorder`` preserves completed but rejected packets; this script
adds the board-level signal gate and the per-request evidence canvas.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/python"))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _cfg_int(config: dict, name: str, default: int) -> int:
    value = config.get(name, default)
    return int(value, 0) if isinstance(value, str) else int(value)


def run_profile_stage(config: dict, args: argparse.Namespace, output: Path) -> dict:
    """Run profile captures on the already selected PYNQ target.

    ``laser_mode`` is an evidence policy only.  It does not switch a laser
    output or modify the camera wiring; the operator must power the laser
    manually when the physical stripe gate is entered.
    """
    from verify_p2_board import (
        _check_gray_gate_a,
        _check_gray_target_identity,
        validate_overlay_contract,
        release_camera,
    )
    from gray_capture_board import verify_camera_control
    from structured_light.p2.camera_evidence import assess_signals, run_monitor
    from structured_light.p2.controller import P2Controller
    from structured_light.p2.pynq_transport import PynqTransport
    from structured_light.p2.pynq_transport import validate_runtime_contract
    from structured_light.p2.pair_evidence import export_pair_evidence
    from structured_light.p2.session import SessionRecorder
    from pynq import MMIO, Overlay

    # Create the result directory before any gate is checked.  Identity, Gate
    # A, ABI, or camera-control failures must leave a usable summary rather
    # than disappearing as an uncaught board-side traceback.
    output.mkdir(parents=True, exist_ok=True)
    raw_config_path = getattr(args, "config", None)
    if raw_config_path is None:
        raise ValueError("profile stage requires the path to the evidence-bound config")
    config_path = Path(raw_config_path).resolve()

    # A paired Profile overlay may only be loaded after the target and the
    # previously measured camera Gate A have been checked.  These checks bind
    # the request to the exact PYNQ-Z2 and hashed monitor evidence; they do not
    # silently create a new Gate A from this run.
    identity = _check_gray_target_identity(config)
    gate_a = _check_gray_gate_a(config, config_path)
    _write_json(output / "deployment_gate.json", {"target_identity": identity, "gate_a": gate_a})

    mode = str(config.get("overlay_mode", "structured_light_profile"))
    if mode not in {"structured_light_profile", "structured_light_profile_hdmi"}:
        raise ValueError("profile stage requires structured_light_profile or structured_light_profile_hdmi")
    validation = validate_overlay_contract(config, mode)
    _write_json(output / "artifact_validation.json", validation)

    overlay = Overlay(validation["bitfile"], download=True)
    diag = MMIO(0x43C10000, 0x10000)
    control = MMIO(0x43C00000, 0x10000)
    # Read the live ABI/capability word before touching camera reset or SCCB.
    # A stale raw/monitor Overlay must fail here, before any camera operation.
    runtime = validate_runtime_contract(control, diag, overlay_mode=mode)
    _write_json(output / "runtime_contract.json", runtime)
    release_camera(diag)
    # Reuse the bounded camera-control helper.  It writes camera_control.json
    # incrementally, including a failure snapshot if the first read fails.
    camera_control = verify_camera_control(diag, output)
    id_reads = list(camera_control["id_reads"])
    pclk_hz = list(camera_control["pclk_hz"])
    width = _cfg_int(config, "width", _cfg_int(config, "frame_width", 752))
    height = _cfg_int(config, "height", _cfg_int(config, "frame_height", 480))
    snapshot = run_monitor(diag, timeout_s=6.0)
    signal = assess_signals(snapshot, width=width, height=height, pclk_hz=min(pclk_hz), frames=16)
    _write_json(output / "camera_gate.json", {"id_reads": id_reads, "pclk_hz": pclk_hz, "signals": signal})
    if not signal["camera_signal_verified"]:
        raise RuntimeError("FV/LV geometry gate failed; no Profile request was started")

    transport = None
    controller = None
    run_records: list[dict] = []
    count = int(getattr(args, "count", 10))
    if count <= 0 or count > 1000:
        raise ValueError("count must be in 1..1000")
    laser_mode = bool(getattr(args, "laser_mode", False))
    threshold = _cfg_int(config, "threshold", 30)
    roi = tuple(config.get("roi", (0, 0, width, height)))
    min_width = _cfg_int(config, "min_width", 1)
    max_width = _cfg_int(config, "max_width", 32)
    min_sum = _cfg_int(config, "min_sum", 30)
    min_peak = _cfg_int(config, "min_peak", 15)
    recorder = SessionRecorder(output / "sessions", metadata={
        "stage": "real_laser_profile" if laser_mode else "ordinary_profile",
        "frame_width": width, "frame_height": height,
        "same_frame": True, "hardware": True,
    })
    try:
        transport = PynqTransport(validation["bitfile"], validation["hwh"], validation["manifest"],
                                   frame_width=width, frame_height=height,
                                   overlay_mode=mode, overlay=overlay)
        controller = P2Controller(transport, recorder=recorder,
                                  session_id=recorder.session_id,
                                  default_timeout_s=max(2.0, 3 * signal["frame_period_s"] + 1.0))
        controller.set_threshold(threshold)
        controller.configure_roi(*roi)
        controller.configure_centroid(min_width=min_width, max_width=max_width,
                                       min_sum=min_sum, min_peak=min_peak)
        for index in range(count):
            record = {
                "index": index, "started_utc": datetime.now(timezone.utc).isoformat(),
                "stage": "real_laser_profile" if laser_mode else "ordinary_profile",
                "passed": False,
            }
            try:
                result = controller.capture_pair(timeout_s=controller.default_timeout_s)
                evidence_dir = output / f"pair_{index:03d}_e{result.epoch:08x}_r{result.request_id:08x}"
                summary = export_pair_evidence(result, evidence_dir, threshold=threshold,
                                                roi=roi, min_width=min_width,
                                                max_width=max_width, min_sum=min_sum,
                                                min_peak=min_peak,
                                                label="REAL LASER PROFILE" if laser_mode else "ORDINARY SCENE PROFILE")
                record.update({"passed": bool(summary["comparison_passed"]),
                               "request_id": result.request_id, "epoch": result.epoch,
                               "evidence": str(evidence_dir.relative_to(output)),
                               "summary": summary})
                # P2Controller retains completed results in a bounded queue
                # for callers that want deferred consumption.  This runner
                # exports each result immediately, so explicitly drain it
                # before the next START; otherwise the ninth request would
                # correctly reject a full queue and look like a DMA failure.
                record["controller_queue_before_drain"] = controller.pending_results
                record["controller_queue_drained"] = len(controller.drain_results())
            except Exception as error:
                # P2Controller's recorder keeps a completed mismatch payload
                # or a cancellation event.  Preserve this request separately
                # so an error can never be mistaken for a skipped iteration.
                record.update({"error_type": type(error).__name__, "error": str(error),
                               "request_id": getattr(getattr(controller, "_inflight", None), "request_id", None)})
            record["finished_utc"] = datetime.now(timezone.utc).isoformat()
            run_records.append(record)
            _write_json(output / "pair_records.json", run_records)
            if not record.get("passed"):
                # Stop on the first failed request.  A later successful frame
                # must never hide a partial capture, timeout, or mismatch.
                raise RuntimeError(
                    f"Profile request {index} failed; partial evidence is in pair_records.json"
                )
        successful = [r for r in run_records if r.get("passed")]
        valid_counts = [int(r["summary"]["valid_single_candidate_rows"]) for r in successful if "summary" in r]
        fractional = [bool(r["summary"]["fractional_center_present"]) for r in successful if "summary" in r]
        if laser_mode:
            # Real-laser acceptance is deliberately strict: all requested
            # requests must have passed, at least ten requests are required,
            # and the >=50-row and fractional-center evidence must occur in
            # the same successful frame.
            laser_gate = (
                count >= 10
                and len(successful) == count
                and any(
                    int(record["summary"].get("valid_single_candidate_rows", 0)) >= 50
                    and bool(record["summary"].get("fractional_center_present"))
                    for record in successful
                )
            )
        else:
            # Ordinary capture is digital-only evidence.  It may legitimately
            # have no laser support; this is recorded and never promoted to a
            # real-laser pass.
            laser_gate = False
        final = {
            "schema": "profile_capture_board_v1",
            "run_id": uuid.uuid4().hex,
            "stage": "real_laser_profile" if laser_mode else "ordinary_profile",
            "count_requested": count, "count_completed": len(successful),
            "camera_signal_verified": True, "camera_id_reads": id_reads,
            "pclk_hz": pclk_hz, "width": width, "height": height,
            "same_frame_profile_verified": len(successful) == count,
            "real_laser_profile_verified": laser_gate,
            "ordinary_all_invalid_is_digital_only": (not laser_mode and not any(valid_counts)),
            "fractional_center_present": any(fractional),
            "records": run_records,
            "hardware": "real_pynq_z2",
            "no_hidden_retry": True,
            "runtime_contract": runtime,
        }
        _write_json(output / "summary.json", final)
        return final
    finally:
        recorder.close()
        if controller is not None:
            controller.close()
        elif transport is not None:
            transport.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bounded same-frame raw/Profile capture on PYNQ-Z2")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--laser-mode", action="store_true", help="apply the real-stripe >=50-row gate")
    args = parser.parse_args()
    import yaml
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("board config must be a mapping")
        result = run_profile_stage(config, args, output)
    except Exception as error:
        # Preserve all artifacts that were produced before the failure.  This
        # includes the camera helper's partial ID/register snapshot and any
        # completed pair records; no retry is attempted here.
        partial: dict[str, object] = {}
        for name in ("deployment_gate.json", "artifact_validation.json", "runtime_contract.json",
                     "camera_control.json", "camera_gate.json", "pair_records.json"):
            path = output / name
            if path.is_file():
                try:
                    partial[name] = json.loads(path.read_text(encoding="utf-8"))
                except Exception as read_error:
                    partial[name] = {"read_error": str(read_error)}
        result = {
            "schema": "profile_capture_board_v1",
            "passed": False,
            "stage": "real_laser_profile" if args.laser_mode else "ordinary_profile",
            "count_requested": args.count,
            "error_type": type(error).__name__,
            "error": str(error),
            "partial": partial,
            "no_hidden_retry": True,
        }
        _write_json(output / "summary.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["same_frame_profile_verified"] and (not args.laser_mode or result["real_laser_profile_verified"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
