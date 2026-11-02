"""Bounded parallel full-feature camera-only observation, with drained AE barriers.

No motion/laser commands are issued. Every Raw/Profile payload is retained;
transition captures stay observation-only and do not drive exposure policy.
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
import threading
import time
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT / "src/python", ROOT.parent / "structured_light_system_geometry_v1/src/python",
              ROOT / "board/scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from continuous_no_laser import (
    DEFAULT_QUALITY_ROI, RESTORE_ORDER, _camera_idle, _check_motion_metadata,
    _default_runtime_factory, _exposure_line_ladder, _json_default, _json_safe,
    _process_telemetry, _quality, _quality_roi, _record_error, _restore_sensor,
    _safe_board_snapshot, _same_motor_identity, _validate_operator_mode,
    _validate_policy_manifest, _write_result_new,
)
from structured_light.scan3d.stream_capture import capture_features, exclusive_capture_generation, read_row_diagnostics
from structured_light.scan3d.stream_writer import BoundedAsyncWriter, _default_sink, profile_payload_filename


thread_cpu_time = getattr(time, "thread_time", time.process_time)


def timing_report(started, loop_start, loop_end, drain_end, cleanup_start, events, samples, count,
                  durable_completed_at=None):
    import statistics
    def duration(a, b):
        return None if a is None or b is None else max(0.0, b - a)
    loop = duration(loop_start, loop_end)
    durable = duration(loop_start, durable_completed_at)
    stages = {}
    for name, values in samples.items():
        ordered = sorted(values)
        stages[name] = {"count": len(values), "sum_s": sum(values),
            "mean_s": statistics.mean(values), "median_s": statistics.median(values),
            "p95_s": ordered[min(len(ordered)-1, math.ceil(len(ordered)*0.95)-1)],
            "max_s": ordered[-1]}
    event_hz = None
    if len(events) >= 2:
        first, last = events[0], events[-1]
        clock = first.get("clock_hz")
        if clock and last.get("clock_hz") == clock and last["fv_ticks"] > first["fv_ticks"]:
            event_hz = (len(events)-1) * clock / (last["fv_ticks"]-first["fv_ticks"])
    return {"init_s": duration(started, loop_start), "loop_s": loop,
        "drain_s": duration(loop_end, drain_end),
        "cleanup_s": duration(cleanup_start, time.monotonic()),
        "camera_event_hz": event_hz,
        "host_capture_completion_hz": ((len(events)-1) / (events[-1]["completed_at_s"]-events[0]["completed_at_s"])
            if len(events)>1 and events[-1]["completed_at_s"]>events[0]["completed_at_s"] else None),
        "loop_frames_per_s": count / loop if loop else None,
        "durable_frames_per_s": count / durable if durable else None,
        "durable_elapsed_s": durable, "stages": stages}


def run_no_laser_fast(manifest: str | Path, output: str | Path, *, load: bool = False,
                 power_off_proof: Any = None, receipt: Any = None, frames: int = 240,
                 capacity: int = 4, capture_timeout_s: float = 2.0,
                 writer_timeout_s: float = 5.0, hysteresis_frames: int = 3,
                 quality_roi: tuple[int, int, int, int] = DEFAULT_QUALITY_ROI,
                 legacy_row_features: bool = False,
                 full_row_features: bool = False,
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
    if type(frames) is not int or not 1 <= frames <= 10000:
        raise ValueError("frames must be in 1..10000")
    if type(legacy_row_features) is not bool or type(full_row_features) is not bool or (legacy_row_features and full_row_features):
        raise ValueError('legacy/full row feature options must be mutually exclusive bools')
    if type(capacity) is not int or not 2 <= capacity <= 4:
        raise ValueError("capacity must be in 2..4")
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
        "schema": "continuous_no_laser_parallel_runner_v1",
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
    runtime = scope = writer = device = coordinator = pipeline = None
    worker_unconfirmed = False
    lock = threading.RLock()
    stage_samples = {}
    accepted_payloads = {}
    chunked_persistence = writer_factory is None and writer_sink is None
    capture_events = []
    transition_remaining = 0
    loop_started = loop_ended = drain_ended = cleanup_started = None
    durable_completed_at = None
    scope_entered = False
    capture_generation = None
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

        def timed_sink(directory, item):
            before = time.monotonic()
            cpu_before = thread_cpu_time()
            try:
                (writer_sink or _default_sink)(directory, item)
                if item.kind == "frame":
                    with lock:
                        accepted_payloads.pop(item.payload["request_id"], None)
            finally:
                with lock:
                    stage_samples.setdefault("durable_io", []).append(time.monotonic() - before)
                    stage_samples.setdefault("writer_thread_cpu", []).append(thread_cpu_time() - cpu_before)

        if chunked_persistence:
            from structured_light.scan3d.stream_chunk_writer import ChunkedAsyncWriter
            writer = ChunkedAsyncWriter(output_path / "frames", capacity=16,
                                        group_size=4, max_delay_s=0.40)
            state["persistence"] = {
                "mode": "grouped_append_payloads_and_committed_index",
                "payload_file": "frames/payloads.bin", "writer_capacity": 16,
                "group_size": 4, "max_delay_s": 0.40,
                "processing_capacity": capacity,
            }
        else:
            writer_builder = writer_factory or BoundedAsyncWriter
            writer = writer_builder(output_path / "frames", capacity=capacity, sink=timed_sink)
            state["persistence"] = {"mode": "injected_per_item_sink",
                "writer_capacity": capacity, "processing_capacity": capacity}

        def release_committed_payloads():
            if chunked_persistence:
                # Query the writer without our publication lock: its worker
                # owns an independent condition, so never nest those locks.
                pending_ids = set(writer.pending_request_ids)
                with lock:
                    for request_id in list(accepted_payloads):
                        if request_id not in pending_ids:
                            accepted_payloads.pop(request_id, None)
        runtime_builder = runtime_factory or _default_runtime_factory
        runtime = runtime_builder(
            manifest_path, load=load, power_off_proof=power_off_proof,
            receipt=receipt, output=output_path / "runtime", quality_roi=roi,
        )
        runtime.transport.frame_motion_metadata_supported = True
        scope = exclusive_capture_generation(runtime)
        scope.__enter__()
        scope_entered = True
        capture_generation = runtime.session._gate.generation
        if (manifest_record.get('compact_policy_dma',{}).get('enabled') is True
                and not legacy_row_features and not full_row_features):
            state['row_feature_dma'] = {'format':'SLC1','enabled':False,'supported':True,
                'compact':True,'admission_attempted':True,'parity_qa_performed':False}
            runtime.session._gate.call(capture_generation,runtime.transport.admit_compact_policy_dma,
                                       manifest_path,workers_joined=True)
            state['row_feature_dma'] = {'format':'SLC1','enabled':True,'supported':True,
                'compact':True,'parity_qa_performed':False}
        elif manifest_record.get('row_feature_dma', {}).get('enabled') is True:
            state['row_feature_dma'] = {'format':'SLP2' if legacy_row_features else 'SLF1',
                'enabled':False,'supported':True,'admission_attempted':True,'parity_qa_performed':False}
            runtime.session._gate.call(capture_generation,runtime.transport.admit_row_feature_dma,
                manifest_path,workers_joined=True,enable=not legacy_row_features)
            state['row_feature_dma'] = {'format': 'SLP2' if legacy_row_features else 'SLF1',
                'enabled': not legacy_row_features, 'supported': True,
                'legacy_row_features_requested': legacy_row_features, 'parity_qa_performed': False}
        else:
            state['row_feature_dma'] = {'format': 'SLP2', 'enabled': False}
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
        compare_frame = qa_fn
        if compare_frame is None:
            # Snapshot checked static parameters before the camera owner starts.
            # The processing worker must never read camera MMIO concurrently.
            from structured_light.frame_io import GrayFrame
            from structured_light.p2.reference import compare_same_frame
            centroid_parameters = runtime.controller._centroid_parameters()
            def compare_frame(_runtime, frame, qa_roi):
                image = GrayFrame.from_pixels(752, 480, frame.raw)
                return dataclasses.asdict(compare_same_frame(
                    image, frame.profile, diagnostics=frame.diagnostics,
                    roi=qa_roi, **centroid_parameters))

        def sample(name, elapsed):
            with lock:
                stage_samples.setdefault(name, []).append(float(elapsed))

        def capture_one(ticket):
            nonlocal capture_count, last_capture_id, capture_epoch, previous_motion
            before = time.monotonic()
            cpu_before = thread_cpu_time()
            timing = {}
            frame = capture(runtime.controller, timeout_s=float(capture_timeout_s),
                            require_motion_metadata=True, timing_s=timing)
            # A timed-out worker may return a valid payload after cleanup has
            # quarantined its generation. Reject it before publishing timing,
            # counters, coordinator watermarks or any result evidence.
            runtime.session._gate.check(ticket.generation)
            completed = time.monotonic()
            capture_cpu_elapsed = thread_cpu_time() - cpu_before
            request_id, epoch = int(frame.request_id), int(frame.epoch)
            if last_capture_id is not None and request_id != last_capture_id + 1:
                raise RuntimeError("captured request_id did not advance exactly by one")
            if capture_epoch is not None and epoch != capture_epoch:
                raise RuntimeError("capture epoch changed inside one writer run")
            if not isinstance(frame.raw, bytes) or not isinstance(frame.profile_payload, bytes):
                raise TypeError("feature capture raw/profile payloads must be immutable bytes")
            from structured_light.scan3d.payload_digest import payload_sha256
            raw_hash = payload_sha256(frame.raw)
            if frame.raw_sha256 != raw_hash:
                raise RuntimeError("feature capture raw SHA-256 mismatch")
            if len(frame.raw) != 752 * 480:
                raise RuntimeError("feature capture raw frame is not 752x480 gray8")
            row_qa_evidence = None
            row_qa_elapsed = None
            if state['row_feature_dma']['enabled'] and ticket.index in qa_indices:
                # Explicit first/mid/last QA belongs to the camera owner, before
                # its next START. Processing workers never read live row MMIO.
                qa_started = time.monotonic()
                try:
                    row_qa = runtime.session._gate.call(ticket.generation, read_row_diagnostics,
                        runtime.transport._diag_mmio, width=752, height=480)
                    runtime.session._gate.check(ticket.generation)
                    row_qa_evidence = {'passed': False, 'request_id': request_id,
                        'capture_epoch': epoch, 'diagnostics': dataclasses.asdict(row_qa)}
                    if getattr(frame,'compact_packet',None) is not None:
                        from structured_light.scan3d.stream_capture import read_compact_diagnostic_extension
                        from structured_light.scan3d.stream_compact import materialize_compact_qa
                        extension = runtime.session._gate.call(ticket.generation,read_compact_diagnostic_extension,
                            runtime.transport._diag_mmio,height=480,request_id=request_id,capture_epoch=epoch,
                            capability_confirmed=getattr(runtime.transport,'compact_policy_dma_enabled',False))
                        runtime.session._gate.check(ticket.generation)
                        frame = materialize_compact_qa(frame,row_qa,extension)
                        runtime.session._gate.check(ticket.generation)
                        row_qa_evidence['compact_raw_extension_passed'] = True
                    if ((row_qa.request_id, row_qa.epoch) != (request_id, epoch)
                            or row_qa.rows != frame.diagnostics.rows):
                        raise RuntimeError('independent AXI row QA differs from SLF1 capture')
                    row_qa_evidence['passed'] = True
                except BaseException as exc:
                    # A late return from revoked IO must publish no report or
                    # payload state. A current-generation QA failure still owns
                    # a completed raw/Profile pair and must preserve it first.
                    runtime.session._gate.check(ticket.generation)
                    preserve_unaccepted(frame, ticket, exc, row_table_qa=row_qa_evidence)
                    raise
                runtime.session._gate.check(ticket.generation)
                row_qa_elapsed = time.monotonic() - qa_started
            motion = _check_motion_metadata(frame.motion_metadata, request_id, epoch,
                                            state["motor_before"], previous_motion)
            runtime.session._gate.check(ticket.generation)
            ticket.metadata["capture_s"] = completed - before
            if row_qa_evidence is not None:
                ticket.metadata['row_table_qa'] = row_qa_evidence
                sample('independent_row_table_qa', row_qa_elapsed)
            sample("capture", completed - before)
            sample("capture_thread_cpu", capture_cpu_elapsed)
            for name, elapsed in timing.items():
                sample("capture_" + name, elapsed)
            capture_count += 1
            previous_motion = motion
            last_capture_id, capture_epoch = request_id, epoch
            with lock:
                # Account exactly one completed capture in coordinator order;
                # the published revision/transition was fixed at admission.
                coordinator.capture_metadata(request_id)
                capture_events.append({"frame_index": ticket.index,
                    "request_id": request_id, "completed_at_s": completed - started,
                    "profile_packet_bytes": len(frame.profile_payload),
                    "profile_wire_format": state['row_feature_dma']['format'],
                    "production_axi_full_row_reads": getattr(frame, 'row_table_full_reads', None),
                    "fv_ticks": motion["fv_ticks"],
                    "clock_hz": motion.get("clock_hz"),
                    "admitted_event_count": motion["admitted_event_count"]})
            return frame

        def preserve_unaccepted(frame, ticket, exc, *, row_table_qa=None):
            evidence = {"frame_index": ticket.index, "request_id": frame.request_id,
                        "capture_epoch": frame.epoch, "raw_sha256": frame.raw_sha256,
                        "raw_bytes": len(frame.raw), "profile_bytes": len(frame.profile_payload),
                        "reason": f"{type(exc).__name__}: {exc}", "durable": False}
            if row_table_qa is not None:
                evidence['row_table_qa'] = row_table_qa
            try:
                directory = output_path / "uncommitted" / f"frame_{frame.request_id:010d}"
                directory.mkdir(parents=True, exist_ok=False)
                for name, payload in (("raw.gray8.bin", frame.raw),
                                      (profile_payload_filename(frame.profile_payload), frame.profile_payload)):
                    with (directory / name).open("xb") as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                evidence["payloads_preserved"] = True
                _write_result_new(directory / "failure.json", evidence)
            except BaseException as preserve_error:
                evidence["preservation_error"] = f"{type(preserve_error).__name__}: {preserve_error}"
            with lock:
                state.setdefault("unaccepted_captures", []).append(evidence)

        def process_one(ticket, frame):
            before = time.monotonic()
            cpu_before = thread_cpu_time()
            request_id, epoch = int(frame.request_id), int(frame.epoch)
            try:
                q_started = time.monotonic()
                if getattr(frame,'compact_packet',None) is not None:
                    from structured_light.scan3d.stream_compact import compact_quality
                    quality = dataclasses.asdict(compact_quality(frame.compact_packet))
                else:
                    quality = _quality(frame.raw, frame.diagnostics, roi)
                sample("quality", time.monotonic() - q_started)
                same_frame = None
                if ticket.index in qa_indices:
                    qa_started = time.monotonic()
                    try:
                        same_frame = _json_safe(compare_frame(runtime, frame, roi))
                    except BaseException as exc:
                        same_frame = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
                    sample("same_frame_qa", time.monotonic() - qa_started)
                    with lock:
                        state.setdefault("same_frame_qa", []).append({
                            "frame_index": ticket.index, "request_id": request_id, **same_frame})
                        if state['row_feature_dma']['enabled']:
                            state['row_feature_dma']['parity_qa_performed'] = True
                            state.setdefault('row_table_qa', []).append(ticket.metadata.get('row_table_qa'))
                exposure = dict(ticket.metadata["exposure"])
                exposure["quality_status"] = quality["status"]
                record = {
                    "request_id": request_id, "capture_epoch": epoch,
                    "raw": frame.raw, "raw_sha256": frame.raw_sha256,
                    "profile_payload": frame.profile_payload,
                    "points_camera_mm": None, "points_axis_mm": None,
                    "summary": {"frame_index": ticket.index,
                        "capture_s": ticket.metadata["capture_s"], "quality": quality,
                        "pclk_hz": getattr(frame.diagnostics, "pclk_hz", None),
                        "last_frame": getattr(frame.diagnostics, "last_frame", None),
                        "same_frame_qa": same_frame,
                        "profile_wire_format": state['row_feature_dma']['format'],
                        "profile_packet_bytes": len(frame.profile_payload),
                        "production_axi_full_row_reads": getattr(frame, 'row_table_full_reads', None),
                        "row_table_qa": ticket.metadata.get('row_table_qa'),
                        "actual_effect_known": False, "measurement_eligible": False},
                    "motion": dict(frame.motion_metadata), "exposure": exposure}
                submit_started = time.monotonic()
                release_committed_payloads()
                # Retain only writer-pending immutable payload references so
                # a failed sink can be salvaged after its worker joins.
                with lock:
                    accepted_payloads[request_id] = record
                sequence = writer.submit(record)
                sample("writer_snapshot_submit", time.monotonic() - submit_started)
                with lock:
                    writer_sequences.append(sequence)
                    state["captured_frames"].append({
                        "frame_index": ticket.index, "request_id": request_id,
                        "capture_epoch": epoch, "raw_sha256": frame.raw_sha256,
                        "quality": quality, "exposure": exposure,
                        "profile_wire_format": state['row_feature_dma']['format'],
                        "profile_packet_bytes": len(frame.profile_payload),
                        "production_axi_full_row_reads": getattr(frame, 'row_table_full_reads', None),
                        "writer_sequence": sequence, "durable": False})
                if same_frame is not None and same_frame.get("passed") is not True:
                    raise RuntimeError(f"same-frame QA failed at request_id {request_id}")
                with lock:
                    # Transition captures are retained, but cannot drive AE.
                    if not exposure["transition"]:
                        coordinator.observe(quality["status"], request_id)
                return request_id
            except BaseException as exc:
                with lock:
                    unaccepted = not any(row["request_id"] == request_id
                                         for row in state["captured_frames"])
                    if unaccepted:
                        accepted_payloads.pop(request_id, None)
                if unaccepted:
                    preserve_unaccepted(frame, ticket, exc)
                raise
            finally:
                sample("processing", time.monotonic() - before)
                sample("processing_thread_cpu", thread_cpu_time() - cpu_before)

        def apply_pending_boundary():
            nonlocal transition_remaining, sccb_fault
            barrier_started = time.monotonic()
            pipeline.drain(float(writer_timeout_s))
            receipt_before_apply = writer.flush(timeout_s=float(writer_timeout_s))
            state["writer_before_exposure"] = dataclasses.asdict(receipt_before_apply)
            if not receipt_before_apply.durable:
                raise TimeoutError("persistence did not drain before exposure apply")
            idle_status = _camera_idle(runtime.controller)
            operation = coordinator.pending
            boundary = last_capture_id
            persist_event({"type": "exposure_intent", "request_id": boundary,
                "revision": operation.revision, "from_lines": coordinator.controller.lines,
                "to_lines": operation.lines, "camera_idle": idle_status}, label="exposure_intent")
            intent_receipt = writer.flush(timeout_s=float(writer_timeout_s))
            state["writer_after_exposure_intent"] = dataclasses.asdict(intent_receipt)
            if not intent_receipt.durable:
                raise TimeoutError("exposure intent not durable before SCCB writes")
            try:
                tx = coordinator.apply_at_boundary(device, boundary, capture_drained=True,
                    processing_drained=True, camera_idle=True)
            except BaseException:
                # The coordinator re-raises cancellation after recording its
                # failed transaction. Any such interruption can follow a
                # partial SCCB batch and must forbid all restoration writes.
                sccb_fault = True
                raise
            state.setdefault("exposure_transactions", []).append(tx)
            if tx.get("accepted") is not True:
                sccb_fault = True
                raise RuntimeError(f"exposure transaction rejected: {tx.get('error')}")
            transition_remaining = int(tx["discard_admitted_frames"])
            persist_event({"type": "exposure_transaction", "request_id": boundary,
                "transaction": _json_safe(tx), "camera_idle": idle_status},
                label="exposure_transaction")
            sample("exposure_barrier", time.monotonic() - barrier_started)

        from structured_light.scan3d.stream_parallel import BoundedParallelFrames
        pipeline = BoundedParallelFrames(capture_one, process_one,
                                         runtime.session._gate, capacity=capacity)
        loop_started = time.monotonic()
        state["init_s"] = loop_started - started
        last_ticket = None
        for index in range(frames):
            # Never queue two camera operations. Processing may lag by at most
            # capacity tickets, while its FIFO and the writer preserve order.
            if last_ticket is not None:
                last_ticket.captured.result(timeout=float(capture_timeout_s) + 0.25)
            for ticket in pipeline.tickets:
                if ticket.processed.done():
                    ticket.processed.result()
            with lock:
                pending = coordinator.has_pending
            if pending:
                apply_pending_boundary()
            # Capacity pressure waits explicitly for the oldest processing job;
            # the deadline fails the run rather than dropping or replacing it.
            outstanding = [t for t in pipeline.tickets if not t.processed.done()]
            if len(outstanding) >= capacity:
                pressure_started = time.monotonic()
                state["processing_capacity_waits"] = state.get("processing_capacity_waits", 0) + 1
                outstanding[0].processed.result(timeout=float(writer_timeout_s))
                sample("processing_backpressure", time.monotonic() - pressure_started)
            with lock:
                # A proposal may have arrived while waiting for capacity. Do
                # not admit against it: drain/apply at the top of this retry.
                if coordinator.has_pending:
                    pending = True
                else:
                    pending = False
                    exposure = {"exposure_lines": coordinator.controller.lines,
                        "exposure_revision": coordinator.controller.revision,
                        "effect_known": False, "measurement_eligible": False,
                        "transition": transition_remaining > 0,
                        "observation_only": True, "exposure_failure": coordinator.controller.failure,
                        "effect_evidence_sha256": None, "effect_evidence_reference": None}
                    if transition_remaining:
                        transition_remaining -= 1
                    last_ticket = pipeline.submit(index, {"exposure": exposure})
            if pending:
                # No frame/index is skipped: use the same boundary routine on
                # a retry by waiting for all work before admitting this index.
                apply_pending_boundary()
                with lock:
                    exposure = {"exposure_lines": coordinator.controller.lines,
                        "exposure_revision": coordinator.controller.revision,
                        "effect_known": False, "measurement_eligible": False,
                        "transition": transition_remaining > 0,
                        "observation_only": True, "exposure_failure": coordinator.controller.failure,
                        "effect_evidence_sha256": None, "effect_evidence_reference": None}
                    if transition_remaining:
                        transition_remaining -= 1
                    last_ticket = pipeline.submit(index, {"exposure": exposure})
        if last_ticket is not None:
            last_ticket.captured.result(timeout=float(capture_timeout_s) + 0.25)
        loop_ended = time.monotonic()
        pipeline.drain(float(writer_timeout_s))
        drain_receipt = writer.flush(timeout_s=float(writer_timeout_s))
        if not drain_receipt.durable:
            raise TimeoutError("final writer drain incomplete")
        durable_completed_at = time.monotonic()
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
        if loop_started is not None and loop_ended is None:
            loop_ended = time.monotonic()
        if pipeline is not None:
            join_started = time.monotonic()
            state["workers"] = pipeline.close(float(writer_timeout_s))
            stage_samples.setdefault("worker_join", []).append(time.monotonic() - join_started)
            worker_unconfirmed = not (state["workers"]["capture_joined"] and
                                      state["workers"]["processing_joined"])
            if worker_unconfirmed:
                _record_error(state, "workers", RuntimeError("parallel worker did not join; quarantine allocation"))
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
            writer_close_started = time.monotonic()
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
                    if durable_completed_at is None:
                        durable_completed_at = time.monotonic()
                    for frame_record in state["captured_frames"]:
                        frame_record["durable"] = True
            finally:
                stage_samples.setdefault("writer_close", []).append(time.monotonic() - writer_close_started)

        drain_ended = time.monotonic()
        cleanup_started = drain_ended

        if writer is not None and chunked_persistence:
            release_committed_payloads()
            state["writer_batches"] = list(writer.batch_stats)
            with lock:
                for batch in state["writer_batches"]:
                    for field in ("payload_fsync_s", "index_fsync_s", "batch_s"):
                        stage_samples.setdefault("writer_" + field[:-2], []).append(
                            float(batch[field]))
                    stage_samples.setdefault("durable_io_batch", []).append(float(batch["io_s"]))
                    stage_samples.setdefault("writer_thread_cpu", []).append(float(batch["thread_cpu_s"]))

        with lock:
            uncommitted_payloads = list(accepted_payloads.values())
        if writer_joined and uncommitted_payloads:
            # A writer failure stops its FIFO. Preserve every accepted but
            # uncommitted payload, including items behind the failed sink.
            # Salvage is explicit failure evidence, never a session commit.
            for record in uncommitted_payloads:
                evidence = {"request_id": record["request_id"],
                    "capture_epoch": record["capture_epoch"], "durable": False,
                    "raw_sha256": record["raw_sha256"], "payloads_preserved": False}
                try:
                    directory = output_path / "uncommitted" / f"frame_{record['request_id']:010d}"
                    directory.mkdir(parents=True, exist_ok=False)
                    for name, payload in (("raw.gray8.bin", record["raw"]),
                                          (profile_payload_filename(record['profile_payload']), record["profile_payload"])):
                        with (directory / name).open("xb") as stream:
                            stream.write(payload)
                            stream.flush()
                            os.fsync(stream.fileno())
                    evidence["payloads_preserved"] = True
                    _write_result_new(directory / "failure.json", evidence)
                except BaseException as exc:
                    evidence["preservation_error"] = f"{type(exc).__name__}: {exc}"
                state.setdefault("uncommitted_accepted_captures", []).append(evidence)

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
            elif worker_unconfirmed:
                state["sensor_restore"] = {
                    "attempted": False, "verified": False,
                    "status": "parallel_worker_unjoined_no_rollback_attempted",
                    "external_recovery_required": True,
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
                        # A fresh identity read is required at the actual restore boundary.
                        try:
                            restore_sensor_id = device.read(0x00)
                        except BaseException:
                            sccb_fault = True
                            raise
                        if restore_sensor_id != 0x1324:
                            raise RuntimeError("fresh sensor identity mismatch before restore")
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

        if runtime is not None and not worker_unconfirmed:
            try:
                state["guard_after"], state["motor_after"] = _safe_board_snapshot(runtime)
                if not _same_motor_identity(state.get("motor_before", {}), state["motor_after"]):
                    raise RuntimeError("motor status changed before cleanup")
            except BaseException as exc:
                _record_error(state, "final_board_snapshot", exc)

        if (runtime is not None and not worker_unconfirmed and writer_joined
                and (getattr(runtime.transport, 'row_feature_dma_enabled', False)
                     or getattr(runtime.transport,'compact_policy_dma_enabled',False)
                     or getattr(runtime.transport,'feature_mode_restore_required',False))):
            try:
                if not scope_entered or getattr(runtime.session, '_poisoned', False):
                    raise RuntimeError('feature mode restore requires an admitted, unpoisoned generation')
                runtime.session._gate.check(capture_generation)
                _camera_idle(runtime.controller)
                if (getattr(runtime.transport,'compact_policy_dma_enabled',False)
                        or getattr(runtime.transport,'feature_mode_restore_required',False)):
                    runtime.session._gate.call(capture_generation,runtime.transport.set_feature_mode,0,workers_joined=True)
                else:
                    runtime.session._gate.call(capture_generation,runtime.transport.set_row_feature_mode,False,workers_joined=True)
                state['row_feature_dma']['restored_mode_zero'] = True
            except BaseException as exc:
                _record_error(state, 'feature_mode_restore', exc)
                state['external_recovery_required'] = True

        try:
            from continuous_board import close_static_resources
            close_static_resources(
                state, scope=scope if scope_entered else None, display=None, runtime=runtime,
                profiler=None, output=output_path, worker_unconfirmed=worker_unconfirmed,
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
        state["external_recovery_required"] = bool(state.get('external_recovery_required')
            or state.get("sensor_restore", {}).get("external_recovery_required"))
        state["capture_count"] = capture_count
        state["capture_events"] = capture_events
        state["workers_unconfirmed"] = worker_unconfirmed
        state["model_display_fps"] = None
        state["model_display_status"] = "unavailable_no_display_or_metric_reconstruction"
        with lock:
            timing_samples = {name: list(values) for name, values in stage_samples.items()}
            timing_events = list(capture_events)
        state["timing"] = timing_report(started, loop_started, loop_ended,
                                         drain_ended, cleanup_started, timing_events,
                                         timing_samples, len(state["captured_frames"]),
                                         durable_completed_at)
        state["thread_cpu_source"] = ("time.thread_time" if hasattr(time, "thread_time")
                                        else "process_time_fallback_shared_across_threads")
        state["elapsed_s"] = max(0.0, time.monotonic() - started)
        state["capture_fps"] = capture_count / state["elapsed_s"] if state["elapsed_s"] > 0 else None
        state.update(_process_telemetry(cpu_started))
        state["passed"] = bool(
            state.get("failure") is None and state.get("capture_complete") is True
            and restore_ok and cleanup_ok and generation_ok and writer_ok and not worker_unconfirmed
        )
        state["durable"] = bool(writer_ok and writer is not None)
        state["sccb_trace"] = sccb_trace
        if writer_sequences:
            state["writer_sequences"] = writer_sequences
        # Workers that could not join still close over `state`. Return and
        # persist the same detached JSON snapshot so a late processing result
        # cannot mutate the already finalized caller-visible evidence.
        with lock:
            result = _json_safe(state)
        _write_result_new(output_path / "RESULT.json", result)
    return result



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--load", action="store_true", help="download only after explicit 12V-off proof")
    mode.add_argument("--receipt", type=Path, help="attach to the exact currently loaded Overlay")
    parser.add_argument("--power-off-proof", type=Path,
                        help="JSON confirmation required with --load")
    parser.add_argument("--frames", type=int, default=240)
    parser.add_argument("--capacity", type=int, default=4)
    parser.add_argument("--capture-timeout", type=float, default=2.0)
    parser.add_argument("--writer-timeout", type=float, default=5.0)
    parser.add_argument("--hysteresis-frames", type=int, default=3)
    feature_mode = parser.add_mutually_exclusive_group()
    feature_mode.add_argument('--legacy-row-features', action='store_true',
                        help='use mode0 SLP2 plus AXI row reads on the same admitted R4 release')
    feature_mode.add_argument('--full-row-features',action='store_true',help='use mode1 SLF1 for comparison')
    args = parser.parse_args(argv)
    if args.load and args.receipt is not None:
        parser.error("select --load or --receipt")
    if args.load and args.power_off_proof is None:
        parser.error("--load requires --power-off-proof")
    result = run_no_laser_fast(
        args.manifest, args.output, load=args.load,
        power_off_proof=args.power_off_proof, receipt=args.receipt,
        frames=args.frames, capacity=args.capacity,
        capture_timeout_s=args.capture_timeout, writer_timeout_s=args.writer_timeout,
        hysteresis_frames=args.hysteresis_frames,
        legacy_row_features=args.legacy_row_features,
        full_row_features=args.full_row_features,
    )
    print(json.dumps({k: v for k, v in result.items() if k not in {"captured_frames", "sccb_trace", "exposure_trace"}},
                     ensure_ascii=False, indent=2, default=_json_default))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
