"""One-position extended settling diagnostic; motion is opt-in and never retried."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
from numbers import Integral
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "python"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from structured_light.motion.pynq_runtime import (  # noqa: E402
    FRAME_HEIGHT,
    FRAME_WIDTH,
    MeasuredSettleSamples,
    SettleBindingError,
    StageBRuntimeError,
    _p2_capture_identity,
    _settle_noise_thresholds,
    _settle_texture_ok,
    create_stage_b_runtime,
)
from structured_light.motion.types import MotionConfig  # noqa: E402
from structured_light.motion.settle import (  # noqa: E402
    SettleConfig,
    SettleEstimate,
    brightness_normalized_difference,
    phase_correlation_shift,
)


EXTENDED_SAMPLE_SCHEDULE_MS = (100, 250, 500, 1000, 2500, 3000, 5000, 6000, 8000)
SETTLE_DEADLINE_S = 32.0


def _write_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise StageBRuntimeError(f"refusing to overwrite probe output {path}")
    import os

    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_jsonl_exclusive(path: Path, values: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise StageBRuntimeError(f"refusing to overwrite probe output {path}")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        for value in values:
            stream.write(json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str) + "\n")
        stream.flush()
        import os
        os.fsync(stream.fileno())


def _as_gray8(frame: Any):
    import numpy as np

    pixels = getattr(frame, "pixels", None)
    if isinstance(pixels, (bytes, bytearray, memoryview)):
        width = int(getattr(frame, "width", 0))
        height = int(getattr(frame, "height", 0))
        values = np.frombuffer(bytes(pixels), dtype=np.uint8)
        if (width, height) != (FRAME_WIDTH, FRAME_HEIGHT) or values.size != FRAME_WIDTH * FRAME_HEIGHT:
            raise SettleBindingError("extended probe requires complete 752x480 gray8 frames")
        return values.reshape(height, width).copy()
    values = np.asarray(frame)
    if values.shape != (FRAME_HEIGHT, FRAME_WIDTH) or values.dtype != np.uint8:
        raise SettleBindingError("extended probe frames must be 752x480 uint8")
    return np.ascontiguousarray(values).copy()


def _save_frame(directory: Path, stem: str, frame: Any) -> dict[str, Any]:
    import io
    import numpy as np
    from PIL import Image

    values = _as_gray8(frame)
    raw = values.tobytes(order="C")
    raw_path = directory / f"{stem}.gray8.bin"
    png_path = directory / f"{stem}.png"
    import os

    with raw_path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    png_buffer = io.BytesIO()
    Image.fromarray(values).save(png_buffer, format="PNG", compress_level=0)
    png = png_buffer.getvalue()
    with Image.open(io.BytesIO(png)) as decoded:
        if not np.array_equal(np.asarray(decoded.convert("L"), dtype=np.uint8), values):
            raise StageBRuntimeError(f"PNG readback differs from raw frame {stem}")
    with png_path.open("xb") as stream:
        stream.write(png)
        stream.flush()
        os.fsync(stream.fileno())
    with Image.open(png_path) as decoded:
        if not np.array_equal(np.asarray(decoded.convert("L"), dtype=np.uint8), values):
            raise StageBRuntimeError(f"PNG disk readback differs from raw frame {stem}")
    return {
        "raw_file": str(raw_path.name), "raw_bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "png_file": str(png_path.name), "png_bytes": len(png), "png_sha256": hashlib.sha256(png).hexdigest(),
    }


def _roi_slice(frame: Any, roi: tuple[int, int, int, int]):
    x0, y0, x1, y1 = roi
    return frame[y0:y1, x0:x1]


def _stop_receipt_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 0:
        raise SettleBindingError(f"extended probe stop marker {name} is not a nonnegative integer")
    return int(value)


def _select_extended_wait(
    baseline: Sequence[Any], candidates: Sequence[Any], *, actual_elapsed_s: Sequence[float],
    roi: tuple[int, int, int, int], config: SettleConfig, frame_period_s: float,
    baseline_thresholds: tuple[float, float, float, float],
) -> dict[str, Any]:
    if len(candidates) != len(EXTENDED_SAMPLE_SCHEDULE_MS) or len(actual_elapsed_s) != len(candidates):
        raise SettleBindingError("extended probe requires all nine scheduled samples")
    if isinstance(frame_period_s, bool):
        raise SettleBindingError("extended probe requires a positive measured frame period")
    try:
        frame_period_value = float(frame_period_s)
    except (TypeError, ValueError) as exc:
        raise SettleBindingError("extended probe requires a positive measured frame period") from exc
    if not math.isfinite(frame_period_value) or frame_period_value <= 0:
        raise SettleBindingError("extended probe requires a positive measured frame period")
    for index, candidate in enumerate(candidates):
        if not _settle_texture_ok(_roi_slice(candidate, roi), config):
            raise SettleBindingError(f"extended probe sample {EXTENDED_SAMPLE_SCHEDULE_MS[index]} ms lacks settle ROI texture")
    phase_p99, diff_p99, phase_threshold, diff_threshold = baseline_thresholds
    late_2500 = _roi_slice(candidates[4], roi)
    late_3000 = _roi_slice(candidates[5], roi)
    late_6000 = _roi_slice(candidates[7], roi)
    late_8000 = _roi_slice(candidates[8], roi)

    def metric(left: Any, right: Any) -> dict[str, float]:
        return {
            "phase_shift_px": math.hypot(*phase_correlation_shift(left, right)),
            "difference_dn": brightness_normalized_difference(left, right),
        }

    late_pairs = {
        "2500_to_3000": metric(late_2500, late_3000),
        "6000_to_8000": metric(late_6000, late_8000),
    }
    # The historical 2.5/3.0 s pair is recorded for diagnosis only. The
    # extended 6.0/8.0 s pair is the new late-reference gate.
    values = late_pairs["6000_to_8000"]
    if values["phase_shift_px"] > phase_threshold or values["difference_dn"] > diff_threshold:
        raise SettleBindingError("extended probe late reference 6000_to_8000 is not stable")
    candidate_metrics = [metric(_roi_slice(candidate, roi), late_8000) for candidate in candidates]
    selected_index = None
    for index in range(len(candidates)):
        if all(
            values["phase_shift_px"] <= phase_threshold and values["difference_dn"] <= diff_threshold
            for values in candidate_metrics[index:]
        ):
            selected_index = index
            break
    if selected_index is None:
        raise SettleBindingError("extended probe has no stable sustained suffix")
    accepted_wait_s = float(actual_elapsed_s[selected_index]) + frame_period_value
    estimate = SettleEstimate(
        settled=True, elapsed_s=accepted_wait_s, samples=len(baseline) + len(candidates),
        baseline_phase_p99=phase_p99, baseline_diff_p99=diff_p99,
        phase_shift_px=candidate_metrics[selected_index]["phase_shift_px"],
        difference_dn=candidate_metrics[selected_index]["difference_dn"],
        phase_threshold_px=phase_threshold, difference_threshold_dn=diff_threshold,
        reason="extended_stable_same_pose", config_hash="", roi=tuple(roi),
    )
    return {
        "selected_index": selected_index,
        "selected_scheduled_ms": EXTENDED_SAMPLE_SCHEDULE_MS[selected_index],
        "accepted_wait_s": accepted_wait_s,
        "frame_period_s": frame_period_value,
        "baseline_phase_p99": phase_p99,
        "baseline_diff_p99": diff_p99,
        "phase_threshold_px": phase_threshold,
        "difference_threshold_dn": diff_threshold,
        "late_reference_checks": late_pairs,
        "candidate_metrics": candidate_metrics,
        "estimate": estimate,
    }


class ExtendedSettleSampler:
    """Nine-sample diagnostic sampler using one validated baseline cache."""

    settle_deadline_s = SETTLE_DEADLINE_S

    def __init__(
        self, controller: Any, *, baseline_frames: Sequence[Any], baseline_capture_records: Sequence[Mapping[str, Any]],
        output: str | Path, roi: tuple[int, int, int, int], config: SettleConfig, config_hash: str,
        frame_period_s: float, monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if len(baseline_frames) != 20 or len(baseline_capture_records) != 20:
            raise SettleBindingError("extended probe requires exactly twenty baseline frames/records")
        self.controller = controller
        self.guard: Any | None = None
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.config = config
        self.config_hash = str(config_hash)
        if isinstance(frame_period_s, bool):
            raise SettleBindingError("extended probe requires a finite positive frame period")
        try:
            frame_period_value = float(frame_period_s)
        except (TypeError, ValueError) as exc:
            raise SettleBindingError("extended probe requires a finite positive frame period") from exc
        if not math.isfinite(frame_period_value) or frame_period_value <= 0:
            raise SettleBindingError("extended probe requires a finite positive frame period")
        self.frame_period_s = frame_period_value
        self.baseline_frames = [_as_gray8(frame) for frame in baseline_frames]
        height, width = self.baseline_frames[0].shape
        if len(roi) != 4 or any(isinstance(item, bool) or not isinstance(item, Integral) for item in roi):
            raise SettleBindingError("extended probe ROI coordinates must be integral")
        x0, y0, x1, y1 = (int(item) for item in roi)
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise SettleBindingError("extended probe ROI is outside baseline frame geometry")
        self.roi = (x0, y0, x1, y1)
        if any(frame.shape != (height, width) for frame in self.baseline_frames):
            raise SettleBindingError("extended probe baseline frame geometry changed")
        if not _settle_texture_ok(_roi_slice(self.baseline_frames[0], self.roi), config):
            raise SettleBindingError("extended probe baseline ROI has insufficient texture")
        self.baseline_capture_records = [dict(item) for item in baseline_capture_records]
        self._validate_baseline_records()
        self._baseline_thresholds = _settle_noise_thresholds(self.baseline_frames, roi=self.roi, config=config)
        self.output = Path(output)
        if self.output.exists():
            raise StageBRuntimeError(f"extended probe output must be new: {self.output}")
        self.output.mkdir(parents=True)
        self._write_json("sampler_config.json", {
            "schema_version": 1, "diagnostic_only": True,
            "sample_schedule_ms": list(EXTENDED_SAMPLE_SCHEDULE_MS),
            "settle_deadline_s": SETTLE_DEADLINE_S,
            "settle_roi": list(self.roi), "config_hash": self.config_hash,
            "frame_period_s": self.frame_period_s,
            "host_poll_max_s": 0.010,
            "thresholds": {
                "baseline_phase_p99": self._baseline_thresholds[0],
                "baseline_diff_p99": self._baseline_thresholds[1],
                "phase_threshold_px": self._baseline_thresholds[2],
                "difference_threshold_dn": self._baseline_thresholds[3],
            },
        })
        self._write_baseline()
        self._pending_stop: dict[str, Any] | None = None
        self.last_observation: dict[str, Any] | None = None
        self.observations: list[dict[str, Any]] = []

    def _write_json(self, name: str, value: Mapping[str, Any]) -> None:
        _write_exclusive(self.output / name, value)

    def _validate_baseline_records(self) -> None:
        request_ids: list[int] = []
        epochs: list[int] = []
        previous_completed = None
        for index, record in enumerate(self.baseline_capture_records):
            try:
                started = float(record["capture_started_monotonic_s"])
                completed = float(record["capture_completed_monotonic_s"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SettleBindingError(f"baseline timing record {index} is incomplete") from exc
            if not math.isfinite(started) or not math.isfinite(completed) or completed < started:
                raise SettleBindingError(f"baseline timing record {index} has invalid finite ordering")
            if previous_completed is not None and started < previous_completed:
                raise SettleBindingError(f"baseline timing record {index} regresses in time")
            previous_completed = completed
            request_id = record.get("camera_request_id")
            epoch = record.get("camera_epoch")
            if isinstance(request_id, bool) or not isinstance(request_id, Integral) or int(request_id) < 0:
                raise SettleBindingError(f"baseline timing record {index} has invalid camera request ID")
            if isinstance(epoch, bool) or not isinstance(epoch, Integral) or int(epoch) < 0:
                raise SettleBindingError(f"baseline timing record {index} has invalid camera epoch")
            request_ids.append(int(request_id))
            epochs.append(int(epoch))
        if len(set(request_ids)) != len(request_ids):
            raise SettleBindingError("baseline camera request IDs are not unique")
        if len(set(epochs)) != 1:
            raise SettleBindingError("baseline camera epoch changed")
        expected = list(range(request_ids[0], request_ids[0] + len(request_ids)))
        if request_ids != expected:
            raise SettleBindingError("baseline camera request IDs are not consecutive")

    def _write_baseline(self) -> None:
        directory = self.output / "baseline"
        directory.mkdir()
        records = []
        for index, frame in enumerate(self.baseline_frames):
            files = _save_frame(directory, f"baseline_{index:02d}", frame)
            timing = dict(self.baseline_capture_records[index])
            records.append({"frame_index": index, **timing, **files})
        _write_jsonl_exclusive(directory / "baseline.jsonl", records)

    def mark_motor_stopped(self, position_index: int, *, monotonic_s: float, motor_receipt: Mapping[str, Any]) -> None:
        if type(position_index) is not int or position_index != 0:
            raise SettleBindingError("extended probe only accepts stop marker position 0")
        if self._pending_stop is not None:
            raise SettleBindingError("extended probe has more than one pending stop marker")
        receipt = dict(motor_receipt)
        if receipt.get("done") is not True or receipt.get("busy") is not False or receipt.get("fault") or receipt.get("cancelled"):
            raise SettleBindingError("extended probe stop marker is not clean MFP1 DONE/idle")
        request_id = _stop_receipt_int(receipt.get("request_id"), "request_id")
        accepted_id = _stop_receipt_int(receipt.get("accepted_id"), "accepted_id")
        if request_id != accepted_id:
            raise SettleBindingError("extended probe stop marker request/accepted ID mismatch")
        if not isinstance(receipt.get("epoch"), Integral) or isinstance(receipt.get("epoch"), bool) or int(receipt["epoch"]) < 0:
            raise SettleBindingError("extended probe stop marker epoch is invalid")
        emitted = _stop_receipt_int(receipt.get("emitted"), "emitted")
        error = _stop_receipt_int(receipt.get("error"), "error")
        if emitted != 320 or error != 0:
            raise SettleBindingError("extended probe stop marker does not prove 320 pulses/error=0")
        final_status = receipt.get("cleanup_status", receipt.get("final_status", receipt.get("stop_readback")))
        if not isinstance(final_status, Mapping) or final_status.get("busy") is not False or final_status.get("step") is not False or final_status.get("enable_n") is not True:
            raise SettleBindingError("extended probe stop marker lacks MFP idle/EN-high readback")
        if self.guard is None:
            raise SettleBindingError("extended probe requires external BSG guard readback at stop")
        external_guard = dict(self.guard.status())
        if (
            external_guard.get("active") is not True
            or external_guard.get("fault") is not False
            or external_guard.get("step_pin") is not False
            or external_guard.get("enable_pin") is not False
        ):
            raise SettleBindingError("external BSG stop readback is not active, fault-free, STEP-low, physical-EN-low")
        try:
            stop_time = float(monotonic_s)
        except (TypeError, ValueError) as exc:
            raise SettleBindingError("extended probe stop time is not finite or follows baseline") from exc
        if not math.isfinite(stop_time) or stop_time < float(self.baseline_capture_records[-1]["capture_completed_monotonic_s"]):
            raise SettleBindingError("extended probe stop time is not finite or follows baseline")
        self._pending_stop = {
            "position_index": int(position_index),
            "stop_confirmed_monotonic_s": stop_time,
            "marked_at_monotonic_s": float(self.monotonic()),
            "motor_receipt": json.loads(json.dumps(receipt, sort_keys=True, default=str)),
            "external_guard_readback": external_guard,
            "diagnostic_only": True,
        }

    def __call__(self, position_index: int) -> MeasuredSettleSamples:
        if self._pending_stop is None or int(self._pending_stop["position_index"]) != int(position_index):
            raise SettleBindingError("extended probe requires matching DONE stop marker")
        stop = self._pending_stop
        self._pending_stop = None
        directory = self.output / f"position_{int(position_index):03d}"
        directory.mkdir()
        samples: list[Any] = []
        records: list[dict[str, Any]] = []
        try:
            for index, scheduled_ms in enumerate(EXTENDED_SAMPLE_SCHEDULE_MS):
                deadline = float(stop["stop_confirmed_monotonic_s"]) + scheduled_ms / 1000.0
                while self.monotonic() < deadline:
                    self.sleep(min(0.005, max(0.0, deadline - self.monotonic())))
                triggered = float(self.monotonic())
                frame = _as_gray8(self.controller.capture_raw(timeout_s=2.0))
                completed = float(self.monotonic())
                identity = _p2_capture_identity(self.controller)
                files = _save_frame(directory, f"sample_{index:02d}", frame)
                samples.append(frame)
                records.append({
                    "sample_index": index, "scheduled_ms": scheduled_ms,
                    **identity, "planned_trigger_monotonic_s": deadline,
                    "actual_triggered_monotonic_s": triggered,
                    "actual_completed_monotonic_s": completed,
                    "actual_elapsed_s": triggered - float(stop["stop_confirmed_monotonic_s"]),
                    "capture_duration_s": completed - triggered, **files,
                })
            selection = _select_extended_wait(
                self.baseline_frames, samples,
                actual_elapsed_s=[item["actual_elapsed_s"] for item in records],
                roi=self.roi, config=self.config, frame_period_s=self.frame_period_s,
                baseline_thresholds=self._baseline_thresholds,
            )
            estimate = selection["estimate"]
            estimate = SettleEstimate(
                settled=True, elapsed_s=selection["accepted_wait_s"], samples=estimate.samples,
                baseline_phase_p99=estimate.baseline_phase_p99, baseline_diff_p99=estimate.baseline_diff_p99,
                phase_shift_px=estimate.phase_shift_px, difference_dn=estimate.difference_dn,
                phase_threshold_px=estimate.phase_threshold_px,
                difference_threshold_dn=estimate.difference_threshold_dn,
                reason=estimate.reason, config_hash=self.config_hash, roi=self.roi,
            )
            result = {
                "position_index": int(position_index), "diagnostic_only": True,
                "b3_verified": False, "b4_verified": False,
                **stop, "sample_schedule_ms": list(EXTENDED_SAMPLE_SCHEDULE_MS),
                "samples": records, "settled": True,
                "selected_scheduled_ms": selection["selected_scheduled_ms"],
                "accepted_wait_s": selection["accepted_wait_s"],
                "estimate": estimate.as_dict(),
                "late_reference_checks": selection["late_reference_checks"],
                "candidate_metrics": selection["candidate_metrics"],
            }
            _write_jsonl_exclusive(directory / "samples.jsonl", records)
            self._write_json(f"position_{int(position_index):03d}.json", result)
            self.last_observation = result
            self.observations.append(dict(result))
            return MeasuredSettleSamples(samples, estimate)
        except Exception as exc:
            result = {
                "position_index": int(position_index), "diagnostic_only": True,
                "b3_verified": False, "b4_verified": False,
                **stop, "sample_schedule_ms": list(EXTENDED_SAMPLE_SCHEDULE_MS),
                "samples": records, "settled": False,
                "error_type": type(exc).__name__, "error": str(exc),
            }
            if not (directory / f"position_{int(position_index):03d}.json").exists():
                self._write_json(f"position_{int(position_index):03d}.json", result)
            raise


def inspect_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "stage": "extended-settle-probe", "diagnostic_only": True,
        "execute_required": True, "motion_started": False,
        "installation": args.installation,
        "b3_verified": False, "b4_verified": False,
        "sample_schedule_ms": list(EXTENDED_SAMPLE_SCHEDULE_MS),
        "settle_deadline_s": SETTLE_DEADLINE_S,
        "pulses_per_position": 320, "rpm": 5.0,
        "hardware_evidence": "offline_contract_only",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--attach", action="store_true")
    parser.add_argument("--installation")
    parser.add_argument("--loaded-overlay-receipt", type=Path)
    parser.add_argument("--motion-confirmation", type=Path)
    parser.add_argument("--driver-proof", type=Path)
    parser.add_argument("--settle-roi", nargs=4, type=int, default=(200, 180, 456, 308))
    parser.add_argument("--stripe-roi", nargs=4, type=int, default=(340, 40, 450, 420))
    parser.add_argument("--threshold", type=int, default=140)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.execute:
        print(json.dumps(inspect_payload(args), indent=2, sort_keys=True))
        return 0
    required = {
        "--attach": args.attach,
        "--installation": args.installation and args.installation.strip(),
        "--loaded-overlay-receipt": args.loaded_overlay_receipt,
        "--motion-confirmation": args.motion_confirmation,
        "--driver-proof": args.driver_proof,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("extended probe execute requires: " + ", ".join(missing))
    if args.output.exists():
        raise RuntimeError(f"probe output already exists: {args.output}")
    runtime = None
    status = {
        "stage": "extended-settle-probe", "diagnostic_only": True,
        "b3_verified": False, "b4_verified": False, "motion_started": False,
        "motion_start_count": 0, "motion_start_count_known": True,
        "completed": False, "observation_stable": False,
        "installation": args.installation,
        "host_poll_max_s": 0.010,
    }
    exit_code = 1
    motion_attempted = False

    def task_start_count() -> tuple[int | None, bool]:
        task = getattr(getattr(runtime, "session", None), "last_task_receipt", None)
        if not isinstance(task, Mapping):
            return None, False
        value = task.get("motion_start_count", task.get("motor_start_count"))
        if value is None or isinstance(value, bool):
            return None, False
        try:
            count = int(value)
        except (TypeError, ValueError):
            return None, False
        if count < 0:
            return None, False
        return count, True

    try:
        runtime = create_stage_b_runtime(
            args.manifest, stage="settle", attach=True,
            loaded_overlay_receipt=args.loaded_overlay_receipt,
            motion_confirmation=args.motion_confirmation, driver_proof=args.driver_proof,
            output=args.output, settle_roi=tuple(args.settle_roi), stripe_roi=tuple(args.stripe_roi),
            threshold=args.threshold, no_display=True,
            installation=args.installation,
            config=MotionConfig(positions=1, host_poll_max_s=0.010),
        )
        # Signals are a camera-only admission step and must complete
        # before baseline capture or any future ARM path is considered.
        signals = runtime.signals()
        if not isinstance(signals, Mapping) or signals.get("passed") is not True:
            raise RuntimeError("signals admission did not pass")
        frame_period = runtime.frame_period_s
        if frame_period is None or isinstance(frame_period, bool):
            raise RuntimeError("signals did not publish a finite positive frame period")
        try:
            frame_period_value = float(frame_period)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("signals did not publish a finite positive frame period") from exc
        if not math.isfinite(frame_period_value) or frame_period_value <= 0:
            raise RuntimeError("signals did not publish a finite positive frame period")
        status["signals"] = signals
        _write_exclusive(Path(args.output) / "probe_motion_config.json", {
            "schema_version": 1, "diagnostic_only": True,
            "config": asdict(runtime.session.config),
            "installation": args.installation,
            "pulses_per_position": 320, "rpm": 5.0,
            "host_poll_max_s": 0.010, "frame_period_s": frame_period_value,
        })
        baseline: list[Any] = []
        baseline_records: list[dict[str, Any]] = []
        for _index in range(20):
            started = time.monotonic()
            frame = runtime.session.run_static(raw=True)
            completed = time.monotonic()
            baseline.append(frame)
            baseline_records.append({"capture_started_monotonic_s": started, "capture_completed_monotonic_s": completed, **_p2_capture_identity(runtime.controller)})
        sampler = ExtendedSettleSampler(
            runtime.controller, baseline_frames=baseline, baseline_capture_records=baseline_records,
            output=Path(args.output) / "settle_samples", roi=tuple(args.settle_roi),
            config=runtime.session.settle_config, config_hash=runtime.session.settle_config_hash,
            frame_period_s=frame_period_value,
        )
        sampler.guard = runtime.guard
        motion_attempted = True
        runtime.session.run_single(settle_sampler=sampler)
        count, count_known = task_start_count()
        if not count_known:
            raise RuntimeError("motion task receipt unavailable after run_single")
        if count != 1:
            raise RuntimeError(f"probe expected exactly one MFP1 START, observed {count}")
        observations = getattr(sampler, "observations", None)
        if (
            not isinstance(observations, Sequence)
            or isinstance(observations, (str, bytes, bytearray))
            or len(observations) != 1
            or not isinstance(observations[0], Mapping)
            or observations[0].get("settled") is not True
        ):
            raise RuntimeError("probe requires exactly one settled observation")
        status.update({
            "motion_started": True, "motion_start_count": count, "motion_start_count_known": True,
            "settle_observations": list(observations),
            "guard_stop_telemetry": [item.get("external_guard_readback") for item in observations],
            "completed": True,
            "observation_stable": True,
            "passed": False, "reason": "diagnostic_only_no_formal_settle_promotion",
        })
        exit_code = 0
    except Exception as exc:
        count, count_known = task_start_count()
        if not motion_attempted:
            count, count_known = 0, True
        status.update({
            "motion_started": (count > 0) if count_known else None,
            "motion_start_count": count, "motion_start_count_known": count_known,
            "passed": False, "error_type": type(exc).__name__, "error": str(exc),
        })
    finally:
        if runtime is not None:
            try:
                cleanup = runtime.close()
                status["cleanup"] = cleanup
                if not isinstance(cleanup, Mapping) or cleanup.get("closed") is not True:
                    raise RuntimeError("probe close did not confirm clean closure")
            except Exception as exc:
                status["close_error"] = f"{type(exc).__name__}: {exc}"
                status["b3_verified"] = False
                status["b4_verified"] = False
                exit_code = 1
        if args.output.exists() and not (Path(args.output) / "probe_result.json").exists():
            try:
                _write_exclusive(Path(args.output) / "probe_result.json", status)
            except Exception as exc:
                exit_code = 1
                status["status_write_error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
