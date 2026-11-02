"""Finite camera-only runner for no-laser observation and bounded exposure tuning.

The runner never arms the motion guard, starts the motor, or issues a laser
command. Each frame uses the generation-checked Raw+Profile capture pair and
is handed to the bounded asynchronous writer. It is a board runner; its tests
inject all hardware-facing dependencies and never connect to a board.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT / "src/python", ROOT.parent / "structured_light_system_geometry_v1/src/python",
              ROOT / "board/scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from structured_light.scan3d.stream_capture import META_ABI, META_MAGIC, capture_features, exclusive_capture_generation
from structured_light.scan3d.stream_writer import BoundedAsyncWriter


DEFAULT_QUALITY_ROI = (620, 210, 710, 430)
RESTORE_ORDER = (0x35, 0x0B, 0xAF)


def _as_dict(value: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"expected mapping/status object, got {type(value).__name__}")


def _json_default(value: Any) -> Any:
    try:
        import numpy as np
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, np.generic):
            return value.item()
    except ImportError:
        pass
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    raise TypeError(f"cannot encode {type(value).__name__} as JSON")


def _json_safe(value: Any) -> Any:
    """Convert coordinator evidence to strict JSON values before FIFO submit."""
    if dataclasses.is_dataclass(value):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    try:
        import numpy as np
        if isinstance(value, np.ndarray):
            return [_json_safe(child) for child in value.tolist()]
        if isinstance(value, np.generic):
            return _json_safe(value.item())
    except ImportError:
        pass
    return value


def _write_result_new(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, default=_json_default)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _record_error(state: dict[str, Any], phase: str, exc: BaseException) -> None:
    item = {"phase": phase, "type": type(exc).__name__, "message": str(exc)}
    if state.get("failure") is None:
        item["traceback"] = traceback.format_exc() if exc.__traceback__ is not None else None
        state["failure"] = item
    else:
        state.setdefault("secondary_errors", []).append(item)
    state["passed"] = False


def _validate_policy_manifest(manifest_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the existing exact artifact gate, then admit only no-motion CSM1."""
    from structured_light.motion.pynq_runtime import verify_stage_b_artifacts

    artifacts = verify_stage_b_artifacts(manifest_path)
    record = json.loads(manifest_path.read_text(encoding="utf-8"))
    if record.get("preboard_passed") is not True:
        raise ValueError("candidate manifest must have preboard_passed=true")
    if record.get("motion_enabled") is not False:
        raise ValueError("no-laser runner requires motion_enabled=false in the manifest")
    metadata = record.get("frame_motion_metadata")
    if not isinstance(metadata, dict) or metadata.get("magic") != META_MAGIC or metadata.get("abi") != META_ABI:
        raise ValueError("manifest must admit exact CSM1 magic and ABI")
    return record, artifacts


def _validate_operator_mode(*, load: bool, power_off_proof: Any, receipt: Any) -> dict[str, Any]:
    from structured_light.motion.pynq_runtime import _loaded_overlay_receipt, _power_off_confirmed

    if type(load) is not bool:
        raise TypeError("load must be bool")
    if load:
        if receipt is not None:
            raise ValueError("choose --load with 12V-off proof or --receipt, not both")
        if power_off_proof is None or not _power_off_confirmed(power_off_proof):
            raise ValueError("--load requires an explicit proof confirming user 12V is off")
        return {"mode": "load", "power_off_confirmed": True, "loaded_receipt_confirmed": False}
    if receipt is None:
        raise ValueError("attach mode requires a loaded-overlay --receipt")
    _loaded_overlay_receipt(receipt)
    # A supplemental proof may be recorded with --receipt, but it does not
    # authorize or trigger a bitstream download in attach mode.
    if power_off_proof is not None and not _power_off_confirmed(power_off_proof):
        raise ValueError("supplied supplemental power-off proof does not confirm user 12V is off")
    return {"mode": "attach", "power_off_confirmed": power_off_proof is not None,
            "loaded_receipt_confirmed": True}


def _default_runtime_factory(manifest_path: Path, *, load: bool, power_off_proof: Any,
                             receipt: Any, output: Path,
                             quality_roi: tuple[int, int, int, int]) -> Any:
    from structured_light.motion import capture_handoff
    from structured_light.motion.pynq_runtime import create_stage_b_runtime
    from structured_light.scan3d.stream_capture import FeaturePynqTransport

    previous_transport = capture_handoff.HandoffPynqTransport
    capture_handoff.HandoffPynqTransport = FeaturePynqTransport
    try:
        runtime = create_stage_b_runtime(
            manifest_path, stage="static", load=load, attach=not load,
            power_off_confirmation=power_off_proof, loaded_overlay_receipt=receipt,
            output=output, no_display=True, threshold=60, stripe_roi=quality_roi,
        )
    finally:
        capture_handoff.HandoffPynqTransport = previous_transport
    # This capability is enabled only after the manifest/bit/HWH gate above.
    runtime.transport.frame_motion_metadata_supported = True
    return runtime


def _camera_idle(controller: Any) -> dict[str, Any]:
    if getattr(controller, "_inflight", None) is not None:
        raise RuntimeError("capture still has an in-flight camera request")
    status = _as_dict(controller.get_status())
    for field in ("busy", "packer_busy"):
        if status.get(field) is not False:
            raise RuntimeError(f"camera is not idle: {field}={status.get(field)!r}")
    return status


def _safe_board_snapshot(runtime: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    guard = _as_dict(runtime.guard.status())
    motor = _as_dict(runtime.motor.status())
    expected = {"active": False, "fault": False, "motor_busy": False,
                "step_pin": False, "enable_pin": True, "error": 0}
    for field, value in expected.items():
        actual = guard.get(field)
        matches = actual is value if type(value) is bool else (type(actual) is int and actual == value)
        if not matches:
            raise RuntimeError(f"motion guard is not in required idle state: {field}={guard.get(field)!r}")
    motor_expected = {"busy": False, "armed": False, "fault": False,
                      "step": False, "enable_n": True, "error": 0}
    for field, value in motor_expected.items():
        actual = motor.get(field)
        matches = actual is value if type(value) is bool else (type(actual) is int and actual == value)
        if not matches:
            raise RuntimeError(f"motor controller is not in required idle state: {field}={motor.get(field)!r}")
    return guard, motor


def _same_motor_identity(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    fields = ("epoch", "accepted_id", "emitted")
    return all(before.get(field) == after.get(field) for field in fields)


def _check_motion_metadata(metadata: Any, request_id: int, epoch: int,
                           motor_before: Mapping[str, Any],
                           previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise RuntimeError("capture did not provide required CSM1 frame metadata")
    if metadata.get("request_id") != request_id or metadata.get("capture_epoch") != epoch:
        raise RuntimeError("CSM1 identity does not match the captured frame")
    if (metadata.get("motor_epoch") != motor_before.get("epoch")
            or metadata.get("motor_accepted_id") != motor_before.get("accepted_id")
            or metadata.get("emitted_command_count") != motor_before.get("emitted")):
        raise RuntimeError("motor command identity changed during camera-only capture")
    for key in ("fv_ticks", "admitted_event_count"):
        value = metadata.get(key)
        if type(value) is not int or value < 0:
            raise RuntimeError(f"CSM1 {key} is missing or invalid")
        if previous is not None and value <= previous.get(key, -1):
            raise RuntimeError(f"CSM1 {key} did not advance strictly")
    return dict(metadata)


def _quality(raw_bytes: bytes, diagnostics: Any, roi: tuple[int, int, int, int]) -> dict[str, Any]:
    import numpy as np
    from structured_light.scan3d.continuous_quality import assess_quality

    x0, y0, x1, y1 = roi
    gray = np.frombuffer(raw_bytes, dtype=np.uint8).reshape(480, 752)
    rows = [row for row in diagnostics.rows if y0 <= int(row.row) < y1]
    expected = y1 - y0
    if len(rows) != expected:
        raise RuntimeError(f"quality ROI needs {expected} row diagnostics, got {len(rows)}")
    value = assess_quality(
        gray[y0:y1, x0:x1],
        row_valid=[bool(row.valid) for row in rows],
        widths_px=[float(row.width) for row in rows],
        intensities=[float(row.peak) for row in rows],
        candidate_counts=[int(row.candidate_count) for row in rows],
    )
    return dataclasses.asdict(value)


def _restore_sensor(device: Any, backup: Mapping[int, int], *, backup_complete: bool) -> dict[str, Any]:
    outcome: dict[str, Any] = {"attempted": False, "order": [hex(reg) for reg in RESTORE_ORDER],
                               "writes": [], "readback": {}, "errors": [], "passed": False,
                               "verified": False, "external_recovery_required": False}
    if not backup_complete or any(reg not in backup for reg in RESTORE_ORDER):
        outcome["status"] = "not_attempted_backup_incomplete_no_exposure_write"
        return outcome
    outcome["attempted"] = True
    for reg in RESTORE_ORDER:
        expected = int(backup[reg])
        item: dict[str, Any] = {"register": hex(reg), "expected": expected, "write_verified": False}
        try:
            device.write_verified(reg, expected)
            item["write_verified"] = True
        except BaseException as exc:
            item["write_error"] = f"{type(exc).__name__}: {exc}"
            outcome["errors"].append(item["write_error"])
            outcome["writes"].append(item)
            outcome["status"] = "restore_failed_sccb_fault"
            outcome["external_recovery_required"] = True
            return outcome
        try:
            actual = int(device.read(reg))
            outcome["readback"][hex(reg)] = actual
            item["readback"] = actual
            if actual != expected:
                item["readback_error"] = f"{actual:#x} != {expected:#x}"
                outcome["errors"].append(f"{hex(reg)} readback mismatch: {item['readback_error']}")
        except BaseException as exc:
            item["readback_error"] = f"{type(exc).__name__}: {exc}"
            outcome["errors"].append(f"{hex(reg)} readback failed: {item['readback_error']}")
        outcome["writes"].append(item)
        if item.get("readback_error"):
            outcome["status"] = "restore_failed_sccb_fault"
            outcome["external_recovery_required"] = True
            return outcome
        if item.get("readback") != expected:
            outcome["status"] = "restore_failed_readback_mismatch"
            outcome["external_recovery_required"] = True
            return outcome
    outcome["passed"] = not outcome["errors"] and all(
        outcome["readback"].get(hex(reg)) == int(backup[reg]) for reg in RESTORE_ORDER
    )
    outcome["verified"] = outcome["passed"]
    outcome["external_recovery_required"] = not outcome["passed"]
    outcome["status"] = "restored" if outcome["passed"] else "restore_failed"
    return outcome


def _quality_roi(value: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    if (not isinstance(value, tuple) or len(value) != 4
            or any(type(v) is not int for v in value)):
        raise ValueError("quality_roi must be a four-int tuple")
    x0, y0, x1, y1 = value
    if not (0 <= x0 < x1 <= 752 and 0 <= y0 < y1 <= 480):
        raise ValueError("quality_roi is outside the 752x480 frame")
    return value


def _exposure_line_ladder(initial_lines: int) -> tuple[int, ...]:
    """Small auditable ladder; no observation can exceed max(480, starting)."""
    if type(initial_lines) is not int or not 1 <= initial_lines <= 32765:
        raise ValueError("starting coarse exposure must be 1..32765")
    upper = max(480, initial_lines)
    values = {30, 60, 120, 240, 480,
              max(1, initial_lines // 4), max(1, initial_lines // 2), initial_lines,
              min(initial_lines * 2, 480)}
    return tuple(sorted(value for value in values if 1 <= value <= upper))


def _same_frame_qa(runtime: Any, frame: Any,
                   roi: tuple[int, int, int, int]) -> dict[str, Any]:
    from structured_light.frame_io import GrayFrame
    from structured_light.p2.reference import compare_same_frame

    image = GrayFrame.from_pixels(752, 480, frame.raw)
    params = runtime.controller._centroid_parameters()
    comparison = compare_same_frame(image, frame.profile, diagnostics=frame.diagnostics,
                                    roi=roi, **params)
    return dataclasses.asdict(comparison)


def _process_telemetry(cpu_started: float) -> dict[str, Any]:
    import platform
    result = {"cpu_seconds": max(0.0, time.process_time() - cpu_started),
              "cpu_cores": os.cpu_count(), "rss_bytes": None, "rss_source": None}
    try:
        import resource
        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        result["rss_bytes"] = peak if sys.platform == "darwin" else peak * 1024
        result["rss_source"] = "resource.ru_maxrss"
    except (ImportError, OSError, AttributeError):
        try:
            import psutil
            result["rss_bytes"] = int(psutil.Process(os.getpid()).memory_info().rss)
            result["rss_source"] = "psutil.current_rss"
        except (ImportError, OSError, AttributeError):
            result["rss_source"] = f"unavailable:{platform.system()}"
    return result


def run_no_laser(manifest: str | Path, output: str | Path, *, load: bool = False,
                 power_off_proof: Any = None, receipt: Any = None, frames: int = 120,
                 capacity: int = 8, capture_timeout_s: float = 2.0,
                 writer_timeout_s: float = 5.0, hysteresis_frames: int = 3,
                 quality_roi: tuple[int, int, int, int] = DEFAULT_QUALITY_ROI,
                 runtime_factory: Callable[..., Any] | None = None,
                 writer_factory: Callable[..., Any] | None = None,
                 writer_sink: Callable[..., Any] | None = None,
                 capture_fn: Callable[..., Any] | None = None,
                 sccb_factory: Callable[..., Any] | None = None,
                 qa_fn: Callable[[Any, Any, tuple[int, int, int, int]], Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Run a finite no-laser observation batch and save an auditable result.

    ``runtime_factory(manifest, *, load, power_off_proof, receipt, output,
    quality_roi)`` and ``sccb_factory(runtime, trace, save)`` are injection
    points for offline tests. The production defaults retain the Stage B exact
    artifact gate, SCCB access, generation capture, and bounded FIFO writer.
    """
    manifest_path, output_path = Path(manifest), Path(output)
    if type(frames) is not int or not 1 <= frames <= 1000:
        raise ValueError("frames must be in 1..1000")
    if type(capacity) is not int or capacity <= 0:
        raise ValueError("capacity must be a positive integer")
    if type(hysteresis_frames) is not int or hysteresis_frames < 1:
        raise ValueError("hysteresis_frames must be positive")
    if (isinstance(capture_timeout_s, bool) or not isinstance(capture_timeout_s, (int, float))
            or not math.isfinite(float(capture_timeout_s)) or capture_timeout_s <= 0):
        raise ValueError("capture_timeout_s must be finite and positive")
    if (isinstance(writer_timeout_s, bool) or not isinstance(writer_timeout_s, (int, float))
            or not math.isfinite(float(writer_timeout_s)) or writer_timeout_s < 0):
        raise ValueError("writer_timeout_s must be finite and non-negative")
    roi = _quality_roi(quality_roi)

    # Refuse reuse before creating any file. Every run owns a fresh result root.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.mkdir(exist_ok=False)
    state: dict[str, Any] = {
        "schema": "continuous_no_laser_runner_v1",
        "passed": False,
        "output_dir": str(output_path),
        "manifest": str(manifest_path),
        "requested_frames": frames,
        "captured_frames": [],
        "quality_roi": list(roi),
        "mode": "camera_only_no_laser_observation",
        "laser_command_count": 0,
        "guard_arm_count": 0,
        "motor_start_count": 0,
        "actual_effect_known": False,
        "measurement_eligible": False,
        "physical_laser_state_verified": False,
        "failure": None,
        "secondary_errors": [],
        "restore": {"attempted": False, "status": "not_started", "passed": False},
        "writer": None,
        "cleanup": None,
    }
    runtime = scope = writer = device = coordinator = None
    scope_entered = False
    writer_closed = False
    sccb_trace: list[dict[str, Any]] = []
    backup: dict[int, int] = {}
    backup_complete = False
    manifest_record: dict[str, Any] | None = None
    artifacts: dict[str, Any] | None = None
    last_capture_id: int | None = None
    capture_epoch: int | None = None
    previous_motion: Mapping[str, Any] | None = None
    writer_sequences: list[int] = []
    sensor_identity_verified = False
    sccb_fault = False
    writer_joined = False
    capture_count = 0
    started = time.monotonic()
    cpu_started = time.process_time()
    qa_indices = {0, frames // 2, frames - 1}

    def writer_save(_trace: Any = None) -> None:
        # SCCBAccess calls save before and after each bounded command. Its full
        # trace is retained in RESULT.json; frame/exposure payloads use the FIFO.
        return None

    def persist_event(event: Mapping[str, Any], *, label: str) -> None:
        nonlocal writer
        if writer is None:
            return
        try:
            sequence = writer.submit_event(dict(event))
            writer_sequences.append(sequence)
        except BaseException as exc:
            state.setdefault("event_errors", []).append(
                {"label": label, "type": type(exc).__name__, "message": str(exc)})
            raise

    try:
        try:
            manifest_record, artifacts = _validate_policy_manifest(manifest_path)
        except BaseException as exc:
            state["artifact_gate"] = {"passed": False,
                                      "error": f"{type(exc).__name__}: {exc}"}
            raise
        state["artifact_gate"] = {
            "passed": True,
            "bit": artifacts["files"]["bit"],
            "hwh": artifacts["files"]["hwh"],
            "manifest": artifacts["files"]["manifest"],
            "manifest_motion_enabled": manifest_record["motion_enabled"],
            "csm_magic": manifest_record["frame_motion_metadata"]["magic"],
            "csm_abi": manifest_record["frame_motion_metadata"]["abi"],
        }
        state["operator_gate"] = _validate_operator_mode(
            load=load, power_off_proof=power_off_proof, receipt=receipt)

        writer_builder = writer_factory or BoundedAsyncWriter
        writer = writer_builder(output_path / "frames", capacity=capacity, sink=writer_sink)
        runtime_builder = runtime_factory or _default_runtime_factory
        runtime = runtime_builder(
            manifest_path, load=load, power_off_proof=power_off_proof,
            receipt=receipt, output=output_path / "runtime", quality_roi=roi,
        )
        runtime.transport.frame_motion_metadata_supported = True
        scope = exclusive_capture_generation(runtime)
        scope.__enter__()
        scope_entered = True
        state["guard_before"], state["motor_before"] = _safe_board_snapshot(runtime)
        state["guard_after"] = None
        state["motor_after"] = None

        if sccb_factory is None:
            from structured_light.p2.camera_exposure import SCCBAccess
            from structured_light.scan3d.stream_backend import GenerationCheckedMMIO
            guarded_diag = GenerationCheckedMMIO(runtime.transport._diag_mmio, runtime.session._gate)
            device = SCCBAccess(guarded_diag, sccb_trace, writer_save)
        else:
            device = sccb_factory(runtime, sccb_trace, writer_save)

        try:
            sensor_id = device.read(0x00)
        except BaseException:
            sccb_fault = True
            raise
        if sensor_id != 0x1324:
            state["sensor_identity_verified"] = False
            raise RuntimeError(f"unexpected sensor ID {sensor_id!r}; expected 0x1324")
        sensor_identity_verified = True
        state["sensor_identity_verified"] = True
        for reg in (0x07, 0x0F, 0xAF, 0x35, 0x0B, 0xD5):
            try:
                backup[reg] = int(device.read(reg))
            except BaseException:
                sccb_fault = True
                raise
        backup_complete = all(reg in backup for reg in RESTORE_ORDER)
        state["sensor"] = {"id": sensor_id, "initial_registers": {hex(k): v for k, v in backup.items()}}
        if not backup_complete:
            raise RuntimeError("starting exposure registers could not be captured")

        from structured_light.scan3d.continuous_exposure import ExposureController
        from structured_light.scan3d.stream_exposure import ExposureCoordinator

        exposure_policy = ExposureController(
            line_values=_exposure_line_ladder(backup[0x0B]), initial_lines=backup[0x0B],
            hysteresis_frames=hysteresis_frames,
        )
        state["exposure_ladder"] = {
            "line_values": list(exposure_policy.line_values),
            "starting_lines": backup[0x0B],
            "maximum_lines": max(480, backup[0x0B]),
            "direction": "weak_steps_up_saturated_steps_down",
        }
        coordinator = ExposureCoordinator(
            exposure_policy, effect_models={}, allow_unvalidated_observation=True,
            observation_discard_frames=3,
        )
        capture = capture_fn or capture_features
        compare_frame = qa_fn or _same_frame_qa
        for index in range(frames):
            before = time.monotonic()
            frame = capture(runtime.controller, timeout_s=float(capture_timeout_s),
                            require_motion_metadata=True)
            capture_count += 1
            elapsed = time.monotonic() - before
            request_id, epoch = int(frame.request_id), int(frame.epoch)
            if last_capture_id is not None and request_id != last_capture_id + 1:
                raise RuntimeError("captured request_id did not advance exactly by one")
            if capture_epoch is not None and epoch != capture_epoch:
                raise RuntimeError("capture epoch changed inside one writer run")
            if not isinstance(frame.raw, bytes) or not isinstance(frame.profile_payload, bytes):
                raise TypeError("feature capture raw/profile payloads must be immutable bytes")
            raw_hash = hashlib.sha256(frame.raw).hexdigest()
            if frame.raw_sha256 != raw_hash:
                raise RuntimeError("feature capture raw SHA-256 mismatch")
            if len(frame.raw) != 752 * 480:
                raise RuntimeError("feature capture raw frame is not 752x480 gray8")
            motion = _check_motion_metadata(frame.motion_metadata, request_id, epoch,
                                            state["motor_before"], previous_motion)
            previous_motion = motion
            measured_motor = _as_dict(runtime.motor.status())
            if not _same_motor_identity(state["motor_before"], measured_motor):
                raise RuntimeError("motor status changed during camera-only capture")
            quality = _quality(frame.raw, frame.diagnostics, roi)
            same_frame = None
            if index in qa_indices:
                try:
                    same_frame = _json_safe(compare_frame(runtime, frame, roi))
                except BaseException as exc:
                    same_frame = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
                state.setdefault("same_frame_qa", []).append({
                    "frame_index": index, "request_id": request_id, **same_frame,
                })
            exposure = coordinator.capture_metadata(request_id)
            # The runner is observation-only under every circumstance, even
            # when an input accidentally contains evidence-model fields.
            exposure["effect_known"] = False
            exposure["measurement_eligible"] = False
            exposure["quality_status"] = quality["status"]
            record = {
                "request_id": request_id,
                "capture_epoch": epoch,
                "raw": frame.raw,
                "raw_sha256": raw_hash,
                "profile_payload": frame.profile_payload,
                "points_camera_mm": None,
                "points_axis_mm": None,
                "summary": {
                    "frame_index": index,
                    "capture_s": elapsed,
                    "quality": quality,
                    "same_frame_qa": same_frame,
                    "actual_effect_known": False,
                    "measurement_eligible": False,
                },
                "motion": motion,
                "exposure": exposure,
            }
            try:
                sequence = writer.submit(record)
                writer_sequences.append(sequence)
            except BaseException as exc:
                state["unaccepted_capture"] = {
                    "request_id": request_id, "capture_epoch": epoch,
                    "raw_sha256": raw_hash, "raw_bytes": len(frame.raw),
                    "profile_bytes": len(frame.profile_payload),
                    "quality": quality, "reason": f"{type(exc).__name__}: {exc}",
                    "durable": False,
                }
                raise
            state["captured_frames"].append({
                "frame_index": index, "request_id": request_id, "capture_epoch": epoch,
                "raw_sha256": raw_hash, "quality": quality,
                "exposure": exposure, "writer_sequence": sequence,
                "durable": False,
            })
            last_capture_id, capture_epoch = request_id, epoch

            if same_frame is not None and same_frame.get("passed") is not True:
                raise RuntimeError(f"same-frame Raw/Profile/diagnostic QA failed at request_id {request_id}")

            operation = coordinator.observe(quality["status"], request_id)
            if operation is not None:
                # Capture and quality processing are synchronous. Flush accepted
                # prior payloads too, then re-check the hardware's idle status.
                receipt_before_apply = writer.flush(timeout_s=float(writer_timeout_s))
                state["writer_before_exposure"] = dataclasses.asdict(receipt_before_apply)
                if not receipt_before_apply.durable:
                    raise TimeoutError("accepted persistence work did not drain before exposure apply")
                idle_status = _camera_idle(runtime.controller)
                persist_event({
                    "type": "exposure_intent", "request_id": request_id,
                    "revision": operation.revision, "from_lines": coordinator.controller.lines,
                    "to_lines": operation.lines, "quality": quality["status"],
                    "camera_idle": idle_status,
                }, label="exposure_intent")
                intent_receipt = writer.flush(timeout_s=float(writer_timeout_s))
                state["writer_after_exposure_intent"] = dataclasses.asdict(intent_receipt)
                if not intent_receipt.durable:
                    raise TimeoutError("exposure intent did not become durable before SCCB writes")
                tx = coordinator.apply_at_boundary(
                    device, request_id, capture_drained=True,
                    processing_drained=True, camera_idle=True,
                )
                state.setdefault("exposure_transactions", []).append(tx)
                if tx.get("accepted") is not True:
                    sccb_fault = True
                    raise RuntimeError(f"exposure transaction was rejected: {tx.get('error')}")
                persist_event({"type": "exposure_transaction", "request_id": request_id,
                               "transaction": _json_safe(tx), "camera_idle": idle_status},
                              label="exposure_transaction")

        state["capture_complete"] = len(state["captured_frames"]) == frames
    except BaseException as exc:
        _record_error(state, "run", exc)
        if writer is not None:
            try:
                persist_event({"type": "runner_failure", "error_type": type(exc).__name__,
                               "error": str(exc)}, label="runner_failure")
            except BaseException:
                pass
    finally:
        if coordinator is not None:
            state["exposure_history"] = coordinator.history
            state["exposure_trace"] = coordinator.trace
            state["final_exposure_policy"] = {
                "lines": coordinator.controller.lines,
                "revision": coordinator.controller.revision,
                "failure": coordinator.controller.failure,
                "observation_only": True,
                "effect_known": False,
                "measurement_eligible": False,
            }

        if writer is not None and not writer_closed:
            try:
                writer_receipt = writer.close(timeout_s=float(writer_timeout_s))
                state["writer"] = dataclasses.asdict(writer_receipt)
                writer_closed = True
            except BaseException as exc:
                receipt = getattr(exc, "receipt", None)
                state["writer"] = dataclasses.asdict(receipt) if receipt is not None else None
                writer_joined = bool(receipt is not None and receipt.worker_joined)
                _record_error(state, "writer_close", exc)
            else:
                writer_joined = bool(writer_receipt.worker_joined)
                if writer_receipt.durable:
                    for frame_record in state["captured_frames"]:
                        frame_record["durable"] = True

        if device is not None:
            if not sensor_identity_verified:
                state["sensor_restore"] = {
                    "attempted": False, "verified": False,
                    "status": "sensor_identity_unverified",
                    "external_recovery_required": True,
                }
            elif sccb_fault:
                state["sensor_restore"] = {
                    "attempted": False, "verified": False,
                    "status": "prior_sccb_fault_no_rollback_attempted",
                    "external_recovery_required": True,
                    "sccb_trace": sccb_trace,
                }
            elif not writer_joined:
                state["sensor_restore"] = {
                    "attempted": False, "verified": False,
                    "status": "persistence_worker_unjoined_no_rollback_attempted",
                    "external_recovery_required": True,
                }
            else:
                try:
                    state["camera_idle_before_restore"] = _camera_idle(runtime.controller)
                except BaseException as exc:
                    state["sensor_restore"] = {
                        "attempted": False, "verified": False,
                        "status": "camera_not_idle_no_rollback_attempted",
                        "error": f"{type(exc).__name__}: {exc}",
                        "external_recovery_required": True,
                    }
                else:
                    try:
                        state["sensor_restore"] = _restore_sensor(
                            device, backup, backup_complete=backup_complete)
                        if state["sensor_restore"].get("external_recovery_required"):
                            sccb_fault = True
                    except BaseException as exc:
                        sccb_fault = True
                        state["sensor_restore"] = {
                            "attempted": backup_complete, "verified": False,
                            "status": "restore_exception", "error": f"{type(exc).__name__}: {exc}",
                            "external_recovery_required": True,
                        }
            state["restore"] = state["sensor_restore"]
            if state["sensor_restore"].get("external_recovery_required"):
                _record_error(state, "restore", RuntimeError(
                    f"sensor restore is unverified: {state['sensor_restore'].get('status')}"))

        if runtime is not None:
            try:
                state["guard_after"], state["motor_after"] = _safe_board_snapshot(runtime)
                if not _same_motor_identity(state.get("motor_before", {}), state["motor_after"]):
                    raise RuntimeError("motor status changed before cleanup")
            except BaseException as exc:
                _record_error(state, "final_board_snapshot", exc)

        try:
            from continuous_board import close_static_resources
            close_static_resources(
                state, scope=scope if scope_entered else None, display=None, runtime=runtime,
                profiler=None, output=output_path, worker_unconfirmed=False,
            )
        except BaseException as exc:
            _record_error(state, "cleanup", exc)

        cleanup_ok = runtime is None or (
            isinstance(state.get("cleanup"), Mapping) and state["cleanup"].get("closed") is True
            and state.get("cleanup_error") is None
        )
        generation_ok = not scope_entered or state.get("generation_close_error") is None
        restore_ok = (not backup_complete) if device is None else state.get("sensor_restore", {}).get("verified") is True
        writer_ok = writer is None or (
            isinstance(state.get("writer"), Mapping) and state["writer"].get("durable") is True
            and state["writer"].get("worker_joined") is True
        )
        state["actual_effect_known"] = False
        state["measurement_eligible"] = False
        state["sccb_fault"] = bool(sccb_fault)
        state["external_recovery_required"] = bool(state.get("sensor_restore", {}).get("external_recovery_required"))
        state["capture_count"] = capture_count
        state["elapsed_s"] = max(0.0, time.monotonic() - started)
        state["capture_fps"] = capture_count / state["elapsed_s"] if state["elapsed_s"] > 0 else None
        state.update(_process_telemetry(cpu_started))
        state["passed"] = bool(
            state.get("failure") is None and state.get("capture_complete") is True
            and restore_ok and cleanup_ok and generation_ok and writer_ok
        )
        state["durable"] = bool(writer_ok and writer is not None)
        state["sccb_trace"] = sccb_trace
        if writer_sequences:
            state["writer_sequences"] = writer_sequences
        _write_result_new(output_path / "RESULT.json", state)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--load", action="store_true", help="download only after explicit 12V-off proof")
    mode.add_argument("--receipt", type=Path, help="attach to the exact currently loaded Overlay")
    parser.add_argument("--power-off-proof", type=Path,
                        help="JSON confirmation required with --load")
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--capacity", type=int, default=8)
    parser.add_argument("--capture-timeout", type=float, default=2.0)
    parser.add_argument("--writer-timeout", type=float, default=5.0)
    parser.add_argument("--hysteresis-frames", type=int, default=3)
    args = parser.parse_args(argv)
    if args.load and args.receipt is not None:
        parser.error("select --load or --receipt")
    if args.load and args.power_off_proof is None:
        parser.error("--load requires --power-off-proof")
    result = run_no_laser(
        args.manifest, args.output, load=args.load,
        power_off_proof=args.power_off_proof, receipt=args.receipt,
        frames=args.frames, capacity=args.capacity,
        capture_timeout_s=args.capture_timeout, writer_timeout_s=args.writer_timeout,
        hysteresis_frames=args.hysteresis_frames,
    )
    print(json.dumps({k: v for k, v in result.items() if k not in {"captured_frames", "sccb_trace", "exposure_trace"}},
                     ensure_ascii=False, indent=2, default=_json_default))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
