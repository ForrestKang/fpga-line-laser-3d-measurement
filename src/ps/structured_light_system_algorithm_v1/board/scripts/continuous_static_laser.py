"""Finite live laser observation: real SLC1/Raw -> production static camera XYZ.

The operator supplies laser illumination and confirms motor 12V is off.
This attach-only entry fixes exposure with a drained SCCB transaction, restores
the starting sensor registers after joined workers, and never admits motion.
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

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT / 'src/python', ROOT.parent / 'structured_light_system_geometry_v1/src/python',
              ROOT / 'board/scripts'):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from continuous_no_laser import (
    _camera_idle, _check_motion_metadata, _default_runtime_factory, _json_safe,
    _record_error, _safe_board_snapshot, _same_motor_identity, _validate_operator_mode,
    _validate_policy_manifest, _write_result_new, _restore_sensor, RESTORE_ORDER,
)
from continuous_scan import _restore_row_feature_mode
from continuous_board import close_static_resources
from structured_light.scan3d.continuous_processor import ContinuousProcessor
from structured_light.scan3d.pipeline import ScanCalibration, DEFAULT_STRIPE_CONFIG, DEFAULT_SELECTION
from structured_light.scan3d.stream_capture import capture_features, exclusive_capture_generation
from structured_light.scan3d.stream_chunk_writer import ChunkedAsyncWriter
from structured_light.scan3d.stream_parallel import BoundedParallelFrames
from structured_light.scan3d.stream_writer import WriterFailure, profile_payload_filename


def _display(runtime, manifest, output, processor):
    from structured_light.p2.hdmi_owner import RemoteHDMIOutput
    from structured_light.scan3d.processed_display import ProcessedObserver
    os.environ['SL_HDMI_PROCESSED_CANVAS'] = '1'
    sink = RemoteHDMIOutput(manifest, owner_log=output / 'hdmi_owner.log', timeout_s=5.,
        startup_timeout_s=60., preview_only=True,
        hardware_lock_fd=runtime.hardware_lock._handle.fileno())
    observer = ProcessedObserver(source='live_static', processor=processor, sink=sink,
                                 asynchronous=True, capacity=1)
    return observer, sink.configure


def _same_frame_qa(runtime, frame, generation):
    """Read the small QA sample before the capture owner issues another START."""
    from structured_light.scan3d.stream_capture import read_row_diagnostics, read_compact_diagnostic_extension
    from structured_light.scan3d.stream_compact import materialize_compact_qa
    rows = runtime.session._gate.call(generation, read_row_diagnostics,
        runtime.transport._diag_mmio, width=752, height=480)
    extension = runtime.session._gate.call(generation, read_compact_diagnostic_extension,
        runtime.transport._diag_mmio, height=480, request_id=frame.request_id,
        capture_epoch=frame.epoch, capability_confirmed=runtime.transport.compact_policy_dma_enabled)
    checked = materialize_compact_qa(frame, rows, extension)
    if ((rows.request_id, rows.epoch) != (frame.request_id, frame.epoch)
            or rows.rows != checked.diagnostics.rows):
        raise RuntimeError('same-frame compact/AXI row QA differs')
    return checked, {'passed': True, 'request_id': frame.request_id, 'capture_epoch': frame.epoch,
                     'compact_raw_extension_passed': True}


def run_static_laser(manifest, output, *, receipt, power_off_proof, camera=None, plane=None,
                     frames=60, capacity=4, hdmi=False, exposure_lines=480,
                     capture_timeout_s=2., writer_timeout_s=5.,
                     runtime_factory=None, capture_fn=None, sccb_factory=None,
                     writer_factory=None, display_factory=None, qa_fn=None,
                     calibration=None, stripe_config=DEFAULT_STRIPE_CONFIG, roi=None):
    """Return a detached RESULT record after bounded joined cleanup.

    Injection points support hardware-free tests. Production always validates
    exact artifacts, the attach receipt and the explicit motor-12V-off proof.
    No scan bundle is loaded: its PENDING axis/timing/volume remain PENDING.
    """
    if type(frames) is not int or not 1 <= frames <= 2000:
        raise ValueError('frames must be in 1..2000')
    if type(capacity) is not int or not 2 <= capacity <= 4:
        raise ValueError('capacity must be in 2..4')
    if type(exposure_lines) is not int or not 1 <= exposure_lines <= 32765:
        raise ValueError('exposure_lines must be in 1..32765')
    if roi is not None:
        if not isinstance(roi, (tuple, list)) or len(roi) != 4 or any(type(v) is not int for v in roi):
            raise ValueError('roi must contain four integer coordinates')
        stripe_config = dataclasses.replace(DEFAULT_STRIPE_CONFIG, roi=tuple(roi))
        if stripe_config.roi[2] > 752 or stripe_config.roi[3] > 480:
            raise ValueError('roi must stay within the 752x480 captured frame')
    for name, value in (('capture_timeout_s', capture_timeout_s), ('writer_timeout_s', writer_timeout_s)):
        if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
            raise ValueError(name + ' must be finite and positive')
    manifest, output = Path(manifest), Path(output)
    record, artifacts = _validate_policy_manifest(manifest)
    if record.get('compact_policy_dma', {}).get('enabled') is not True:
        raise ValueError('live static laser entry requires admitted real SLC1')
    if power_off_proof is None:
        raise ValueError('static laser observation requires explicit motor 12V-off proof')
    operator = _validate_operator_mode(load=False, receipt=receipt, power_off_proof=power_off_proof)
    calibration = calibration or ScanCalibration.load(camera, plane)
    output.mkdir(parents=True, exist_ok=False)
    state = dict(schema='continuous_static_laser_runner_v1', passed=False, source='live_static',
        mode='static_camera_xyz_observation', requested_frames=frames, captured_frames=[],
        manifest=str(manifest), output_dir=str(output), operator_gate=operator,
        artifact_gate={'passed': True, 'bit': artifacts['files']['bit']},
        camera_sha256=calibration.camera_sha256, plane_sha256=calibration.plane_sha256,
        stripe_config_fingerprint=stripe_config.fingerprint,
        stripe_config=dataclasses.asdict(stripe_config), quality_roi=list(stripe_config.roi),
        roi_overridden=roi is not None,
        overlay_download_count=0, laser_command_count=0, guard_arm_count=0, motor_start_count=0,
        sensor_write_count=0, physical_laser_state_verified=False, laser_source='operator_supplied',
        actual_effect_known=False, measurement_eligible=False, physical_accuracy_verified=False,
        axis_validated=False, timing_validated=False, volume_validated=False, fusion_performed=False,
        observation_only=True, failure=None, secondary_errors=[], hdmi_requested=hdmi,
        row_feature_dma={'enabled': False, 'format': 'SLC1', 'admitted': False},
        sensor_restore={'attempted': False, 'status': 'not_started', 'writes': []})
    runtime = scope = pipeline = writer = display = access = None
    generation = None
    lock = threading.RLock()
    pending = {}
    submitted = set()
    samples = {}
    trace = []
    previous_id = previous_epoch = previous_motion = None
    started = time.monotonic()
    capture_count = 0
    backup = {}
    sensor_identity_verified = sccb_fault = sensor_modified = False
    qa_indices = {0, frames // 2, frames - 1}
    last_status_at = 0.

    def live_status(last=None, *, finished=False):
        nonlocal last_status_at
        now = time.monotonic()
        if not finished and last is not None and last['frame_index'] % 10 and now - last_status_at < 1.:
            return
        display_status = display.summary() if display is not None else {}
        status = dict(schema='continuous_static_live_status_v1', source='live_static',
            finished=finished, elapsed_s=now-started, capture_count=capture_count,
            processed_count=len(state['captured_frames']),
            last_request_id=last.get('request_id') if last else None,
            last_valid_point_count=last.get('valid_point_count') if last else None,
            last_quality=last.get('quality') if last else None,
            hdmi_acknowledged=display_status.get('hdmi_acknowledged', 0),
            display_rendered=display_status.get('rendered', 0),
            display_dropped=display_status.get('dropped', 0),
            measurement_eligible=False, actual_effect_known=False,
            physical_accuracy_verified=False)
        path = output / 'LIVE_STATUS.json'
        temporary = output / 'LIVE_STATUS.json.tmp'
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(status, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
        os.replace(temporary, path)
        last_status_at = now

    def release_committed():
        if writer is None:
            return
        waiting = set(writer.pending_request_ids)
        with lock:
            for request_id in list(submitted - waiting):
                pending.pop(request_id, None)
                submitted.discard(request_id)

    try:
        writer = (writer_factory or ChunkedAsyncWriter)(output / 'frames', capacity=16,
                                                       group_size=4, max_delay_s=.40)
        runtime = (runtime_factory or _default_runtime_factory)(manifest, load=False,
            power_off_proof=power_off_proof, receipt=receipt, output=output / 'runtime',
            quality_roi=stripe_config.roi)
        runtime.transport.frame_motion_metadata_supported = True
        pending_scope = exclusive_capture_generation(runtime)
        pending_scope.__enter__()
        scope = pending_scope
        generation = runtime.session._gate.generation
        state['guard_before'], state['motor_before'] = _safe_board_snapshot(runtime)
        mode = state['row_feature_dma']
        mode['admission_attempted'] = True
        admitted = runtime.session._gate.call(generation, runtime.transport.admit_compact_policy_dma,
                                            manifest, workers_joined=True)
        if not admitted or runtime.transport.compact_policy_dma_enabled is not True:
            raise RuntimeError('real SLC1 mode2 admission was not confirmed')
        mode.update(enabled=True, admitted=True)
        if sccb_factory is None:
            from structured_light.p2.camera_exposure import SCCBAccess
            from structured_light.scan3d.stream_backend import GenerationCheckedMMIO
            access = SCCBAccess(GenerationCheckedMMIO(runtime.transport._diag_mmio, runtime.session._gate),
                                trace, lambda: None)
        else:
            access = sccb_factory(runtime, trace, lambda: None)
        try:
            sensor = {reg: int(access.read(reg)) for reg in
                      (0, 7, 15, 175, 53, 11, 213, 1, 2, 3, 4, 5, 6, 0x0C, 0x0D, 0xB6)}
        except BaseException:
            sccb_fault = True
            raise
        state['sensor'] = {'id': sensor[0], 'initial_registers': {str(k): v for k, v in sensor.items()},
                           'exposure_revision': 0, 'read_only': False}
        if sensor[0] != 0x1324:
            raise ValueError('actual sensor identity mismatch')
        sensor_identity_verified = True
        backup = {reg: sensor[reg] for reg in RESTORE_ORDER}
        from structured_light.p2.camera_exposure import manual_plan
        plan = manual_plan(sensor, coarse_rows=exposure_lines, gain_code=16)
        _camera_idle(runtime.controller)
        state['exposure_transaction'] = {'plan': plan, 'capture_drained': True,
            'processing_drained': True, 'camera_idle': True, 'effect_known': False,
            'measurement_eligible': False, 'readback_verified': False}
        if isinstance(receipt, (str, Path)):
            receipt_path = Path(receipt).resolve()
            receipt_bytes = receipt_path.read_bytes()
            receipt_identity = {'path': str(receipt_path), 'sha256': hashlib.sha256(receipt_bytes).hexdigest(),
                                'source': 'loaded_receipt_file'}
        else:
            receipt_bytes = json.dumps(_json_safe(receipt), sort_keys=True, allow_nan=False).encode('utf-8')
            receipt_identity = {'path': None, 'sha256': hashlib.sha256(receipt_bytes).hexdigest(),
                                'source': 'injected_receipt_mapping'}
        boot_path = Path('/proc/sys/kernel/random/boot_id')
        boot_id = boot_path.read_text().strip() if boot_path.is_file() else getattr(runtime, 'boot_id', None)
        if not boot_id:
            raise RuntimeError('current boot ID is unavailable before SCCB writes')
        sensor_backup = dict(schema='continuous_static_sensor_backup_v1', sensor_id=sensor[0],
            sensor_snapshot={str(reg): value for reg, value in sensor.items()},
            original_restore_registers={str(reg): value for reg, value in backup.items()},
            manual_plan=[{'register': reg, 'value': value} for reg, value in plan],
            receipt=receipt_identity, boot_id=boot_id, manifest=str(manifest.resolve()),
            bit_sha256=artifacts['files']['bit']['sha256'],
            effect_known=False, measurement_eligible=False, sensor_write_started=False)
        backup_bytes = json.dumps(sensor_backup, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
        backup_path = output / 'SENSOR_BACKUP.json'
        with backup_path.open('xb') as stream:
            stream.write(backup_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        state['sensor_backup'] = {'path': str(backup_path), 'sha256': hashlib.sha256(backup_bytes).hexdigest(),
                                  'durable_before_sensor_write': True, 'boot_id': boot_id}
        try:
            for reg, value in plan:
                sensor_modified = True
                state['sensor_write_count'] += 1
                access.write_verified(reg, value)
            fixed = {reg: int(access.read(reg)) for reg in (175, 53, 11)}
            if fixed != dict(plan):
                raise RuntimeError('fixed exposure readback differs from plan')
            state['exposure_transaction']['readback_verified'] = True
        except BaseException:
            sccb_fault = True
            raise
        exposure = dict(exposure_lines=exposure_lines, exposure_revision=0, gain_code=16,
            effect_known=False, actual_effect_known=False, measurement_eligible=False,
            transition=False, observation_only=True)
        processor = ContinuousProcessor(calibration=calibration, stripe_config=stripe_config,
            selection=DEFAULT_SELECTION, mode='static', exposure_lines=exposure_lines, exposure_revision=0)
        parameters = runtime.controller._centroid_parameters()
        expected = dict(threshold=stripe_config.fixed_threshold, min_width=stripe_config.min_width,
            max_width=stripe_config.max_width, min_sum=stripe_config.min_sum, min_peak=stripe_config.min_peak)
        if parameters != expected or runtime.controller._roi() != stripe_config.roi:
            raise ValueError('actual PL stripe configuration differs from static processor')
        if hdmi:
            display, configure = (display_factory or _display)(runtime, manifest, output, processor)
            configure()

        def capture_one(ticket):
            nonlocal previous_id, previous_epoch, previous_motion, capture_count
            begin = time.monotonic()
            timing = {}
            frame = (capture_fn or capture_features)(runtime.controller, timeout_s=capture_timeout_s,
                require_motion_metadata=True, timing_s=timing)
            runtime.session._gate.check(ticket.generation)
            from structured_light.scan3d.payload_digest import payload_sha256
            if (not isinstance(frame.raw, bytes) or len(frame.raw) != 752 * 480
                    or not isinstance(frame.profile_payload, bytes)
                    or payload_sha256(frame.raw) != frame.raw_sha256):
                raise ValueError('capture Raw/payload integrity failed')
            # Own completed bytes before validating metadata or QA, so a
            # rejected pair can still be saved after joined workers stop.
            release_committed()
            with lock:
                pending[frame.request_id] = dict(request_id=frame.request_id, capture_epoch=frame.epoch,
                    raw=frame.raw, raw_sha256=frame.raw_sha256, profile_payload=frame.profile_payload,
                    points_camera_mm=None, points_axis_mm=None, summary={},
                    motion=dict(frame.motion_metadata or {}), exposure=dict(exposure))
            if frame.compact_packet is None:
                raise ValueError('capture did not contain a real SLC1 packet')
            if previous_id is not None and (frame.request_id != previous_id + 1 or frame.epoch != previous_epoch):
                raise ValueError('capture identity discontinuity')
            motion = _check_motion_metadata(frame.motion_metadata, frame.request_id, frame.epoch,
                                             state['motor_before'], previous_motion)
            if ticket.index in qa_indices:
                frame, qa = (qa_fn or _same_frame_qa)(runtime, frame, ticket.generation)
                ticket.metadata['same_frame_qa'] = qa
                if qa.get('passed') is not True:
                    raise RuntimeError('same-frame QA failed')
            runtime.session._gate.check(ticket.generation)
            frame_exposure = {**exposure, 'transition': ticket.index < 3}
            with lock:
                pending[frame.request_id]['exposure'] = frame_exposure
            frame = dataclasses.replace(frame, motion_metadata={**motion, **frame_exposure,
                'stripe_config_fingerprint': stripe_config.fingerprint})
            previous_id, previous_epoch, previous_motion = frame.request_id, frame.epoch, motion
            with lock:
                capture_count += 1
                ticket.metadata['capture_s'] = time.monotonic() - begin
                ticket.metadata['capture_timing_s'] = timing
            return frame

        def process_one(ticket, frame):
            begin = time.monotonic()
            result = processor.process(frame, feature_config_fingerprint=stripe_config.fingerprint)
            if result.fused or result.registered or result.mode != 'static':
                raise RuntimeError('static processor attempted registration/fusion')
            summary = {**result.summary(), 'source': 'live_static', 'frame_index': ticket.index,
                'actual_effect_known': False, 'measurement_eligible': False, 'observation_only': True,
                'same_frame_qa': ticket.metadata.get('same_frame_qa'),
                'capture_s': ticket.metadata['capture_s'], 'profile_wire_format': 'SLC1',
                'profile_packet_bytes': len(frame.profile_payload),
                'production_axi_full_row_reads': getattr(frame, 'row_table_full_reads', None),
                'capture_timing_s': ticket.metadata['capture_timing_s']}
            with lock:
                record = pending[frame.request_id]
                record.update(points_camera_mm=result.points_camera_mm[result.valid_points], summary=summary)
            # The writer accepts only finite strict XYZ. Complete row masks,
            # Q12 centers and reasons preserve the rejected-row provenance.
            summary.update(valid_points=result.valid_points.tolist(), reasons=list(result.reasons),
                           rows=result.rows.tolist(), center_q12=result.center_q12.tolist())
            try:
                sequence = writer.submit(record)
            except BufferError:
                receipt_before_retry = writer.flush(timeout_s=writer_timeout_s)
                if not receipt_before_retry.durable:
                    raise TimeoutError('writer did not drain at static backpressure')
                sequence = writer.submit(record)
            with lock:
                submitted.add(frame.request_id)
                state['captured_frames'].append({**result.summary(), 'frame_index': ticket.index,
                    'raw_sha256': frame.raw_sha256, 'writer_sequence': sequence,
                    'same_frame_qa': ticket.metadata.get('same_frame_qa'),
                    'actual_effect_known': False, 'measurement_eligible': False,
                    'capture_s': ticket.metadata['capture_s'], 'processing_s': time.monotonic() - begin})
                samples.setdefault('processing_s', []).append(time.monotonic() - begin)
            if display is not None:
                display.consume(frame, result)
            live_status(state['captured_frames'][-1])
            return frame.request_id

        pipeline = BoundedParallelFrames(capture_one, process_one, runtime.session._gate, capacity=capacity)
        loop_started = time.monotonic()
        last_ticket = None
        for index in range(frames):
            if last_ticket is not None:
                last_ticket.captured.result(timeout=capture_timeout_s + .25)
            for ticket in pipeline.tickets:
                if ticket.processed.done():
                    ticket.processed.result()
            outstanding = [t for t in pipeline.tickets if not t.processed.done()]
            if len(outstanding) >= capacity:
                outstanding[0].processed.result(timeout=writer_timeout_s)
            last_ticket = pipeline.submit(index, {})
        pipeline.drain(writer_timeout_s)
        state['loop_s'] = time.monotonic() - loop_started
        state['capture_complete'] = len(state['captured_frames']) == frames
    except BaseException as exc:
        _record_error(state, 'run', exc)
    finally:
        workers_joined = True
        if pipeline is not None:
            state['workers'] = pipeline.close(writer_timeout_s)
            workers_joined = state['workers']['capture_joined'] and state['workers']['processing_joined']
        writer_joined = True
        if writer is not None:
            try:
                writer_receipt = writer.close(timeout_s=writer_timeout_s)
            except WriterFailure as exc:
                writer_receipt = exc.receipt
            except BaseException as exc:
                writer_receipt = None
                _record_error(state, 'writer_close', exc)
            writer_joined = bool(writer_receipt and writer_receipt.worker_joined and not writer_receipt.worker_unconfirmed)
            state['writer'] = dataclasses.asdict(writer_receipt) if writer_receipt else None
            if writer_joined:
                release_committed()
        display_joined = True
        if display is not None:
            try:
                state['display'] = display.close(timeout_s=5.)
                display_joined = state['display']['worker_unconfirmed'] is False
                if not state['display'].get('passed'):
                    _record_error(state, 'display_close', RuntimeError('display completion failed'))
            except BaseException as exc:
                display_joined = False
                _record_error(state, 'display_close', exc)
        worker_unconfirmed = not (workers_joined and writer_joined and display_joined)
        state['workers_unconfirmed'] = worker_unconfirmed
        if worker_unconfirmed:
            _record_error(state, 'workers', RuntimeError('worker join unconfirmed; retain shared hardware'))
            state['external_recovery_required'] = True
        if display is not None:
            if worker_unconfirmed or state.get('display', {}).get('passed') is not True:
                state['last_hdmi_canvas'] = {'saved': False, 'status': 'skipped_unconfirmed_or_failed_cleanup'}
            else:
                try:
                    snapshot = display.last_rendered_canvas()
                    if snapshot is None:
                        state['last_hdmi_canvas'] = {'saved': False, 'status': 'no_rendered_canvas'}
                    else:
                        import cv2
                        canvas_path = output / 'LAST_HDMI_CANVAS.png'
                        if canvas_path.exists():
                            raise FileExistsError(str(canvas_path))
                        if not cv2.imwrite(str(canvas_path), snapshot['canvas']):
                            raise OSError('last HDMI canvas PNG encoding failed')
                        with canvas_path.open('rb+') as stream:
                            os.fsync(stream.fileno())
                        metadata = _json_safe({k: v for k, v in snapshot.items() if k != 'canvas'})
                        metadata.update(png_path=str(canvas_path),
                            png_sha256=hashlib.sha256(canvas_path.read_bytes()).hexdigest(),
                            source='exact_last_processed_hdmi_canvas', captured_after_joined_cleanup=True)
                        metadata_path = output / 'LAST_HDMI_CANVAS.json'
                        _write_result_new(metadata_path, metadata)
                        state['last_hdmi_canvas'] = {**metadata, 'saved': True,
                            'metadata_path': str(metadata_path),
                            'metadata_sha256': hashlib.sha256(metadata_path.read_bytes()).hexdigest()}
                except BaseException as exc:
                    state['last_hdmi_canvas'] = {'saved': False, 'status': 'snapshot_export_failed',
                                                  'error': type(exc).__name__ + ': ' + str(exc)}
                    _record_error(state, 'last_hdmi_canvas', exc)
        if workers_joined and writer_joined:
            with lock:
                unresolved = list(pending.values())
            for item in unresolved:
                evidence = {'request_id': item['request_id'], 'durable': False, 'payloads_preserved': False}
                try:
                    directory = output / 'uncommitted' / f"frame_{item['request_id']:010d}"
                    directory.mkdir(parents=True, exist_ok=False)
                    for name, payload in (('raw.gray8.bin', item['raw']),
                            (profile_payload_filename(item['profile_payload']), item['profile_payload'])):
                        with (directory / name).open('xb') as stream:
                            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
                    if item['points_camera_mm'] is not None:
                        import numpy as np
                        with (directory / 'points_camera_mm.npy').open('xb') as stream:
                            np.save(stream, item['points_camera_mm'], allow_pickle=False)
                            stream.flush(); os.fsync(stream.fileno())
                    _write_result_new(directory / 'record.json', {k: v for k, v in item.items()
                        if k not in ('raw', 'profile_payload', 'points_camera_mm', 'points_axis_mm')})
                    evidence['payloads_preserved'] = True
                except BaseException as exc:
                    evidence['preservation_error'] = str(exc)
                state.setdefault('uncommitted_captures', []).append(evidence)
        if runtime is not None and not worker_unconfirmed:
            try:
                _camera_idle(runtime.controller)
                state['guard_after'], state['motor_after'] = _safe_board_snapshot(runtime)
                if not _same_motor_identity(state['motor_before'], state['motor_after']):
                    raise RuntimeError('motor pulse count or identity changed during static observation')
                state['motor_identity_unchanged'] = True
            except BaseException as exc:
                _record_error(state, 'final_readback', exc)
            if access is not None and sensor_modified:
                if sccb_fault:
                    state['sensor_restore'].update(status='prior_sccb_fault_no_rollback_attempted',
                        verified=False, external_recovery_required=True)
                    state['external_recovery_required'] = True
                elif sensor_identity_verified:
                    try:
                        _camera_idle(runtime.controller)
                        if access.read(0) != 0x1324:
                            raise RuntimeError('fresh sensor identity mismatch before restore')
                        state['sensor_restore'] = _restore_sensor(access, backup, backup_complete=True)
                        state['sensor_write_count'] += len(state['sensor_restore']['writes'])
                        if not state['sensor_restore']['verified']:
                            raise RuntimeError('sensor restore was not verified')
                    except BaseException as exc:
                        _record_error(state, 'sensor_restore', exc)
                        state['external_recovery_required'] = True
            _restore_row_feature_mode(runtime, state, generation=generation,
                                      workers_joined=workers_joined and display_joined, writer_joined=writer_joined)
        close_static_resources(state, scope=scope, display=None, runtime=runtime, profiler=None,
                               output=output, worker_unconfirmed=worker_unconfirmed)
        state.update(capture_count=capture_count, sensor_trace=trace,
            elapsed_s=time.monotonic() - started, timing_samples=samples, sccb_fault=sccb_fault)
        cleanup = state.get('cleanup', {})
        state['runtime_closed'] = cleanup.get('closed') is True
        state['capture_handoff'] = cleanup.get('capture_handoff')
        state['passed'] = bool(state['failure'] is None and state.get('capture_complete')
            and state['runtime_closed'] and state.get('writer', {}).get('durable')
            and not worker_unconfirmed and state.get('generation_close_error') is None
            and state.get('cleanup_error') is None and not state.get('external_recovery_required'))
        with lock:
            result = _json_safe(state)
        _write_result_new(output / 'RESULT.json', result)
        if workers_joined:
            try:
                live_status(state['captured_frames'][-1] if state['captured_frames'] else None, finished=True)
            except BaseException:
                # RESULT is the final durable record; status is advisory only.
                pass
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'receipt', 'power-off-proof', 'camera', 'plane', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--frames', type=int, default=60)
    parser.add_argument('--capacity', type=int, default=4)
    parser.add_argument('--exposure-lines', type=int, default=480)
    parser.add_argument('--roi', type=int, nargs=4, metavar=('X0', 'Y0', 'X1', 'Y1'),
                        help='explicit observation ROI; default remains 620 210 710 430')
    parser.add_argument('--capture-timeout', type=float, default=2.)
    parser.add_argument('--writer-timeout', type=float, default=5.)
    display = parser.add_mutually_exclusive_group()
    display.add_argument('--hdmi', action='store_true')
    display.add_argument('--no-display', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = run_static_laser(args.manifest, args.output, receipt=args.receipt,
            power_off_proof=args.power_off_proof, camera=args.camera, plane=args.plane,
            frames=args.frames, capacity=args.capacity, hdmi=args.hdmi, exposure_lines=args.exposure_lines,
            roi=tuple(args.roi) if args.roi is not None else None,
            capture_timeout_s=args.capture_timeout, writer_timeout_s=args.writer_timeout)
    except Exception as exc:
        print(json.dumps({'passed': False, 'stage': 'entry_error', 'error': type(exc).__name__ + ': ' + str(exc)}))
        return 2
    print(json.dumps({k: v for k, v in result.items() if k not in ('captured_frames', 'sensor_trace')},
                     ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
