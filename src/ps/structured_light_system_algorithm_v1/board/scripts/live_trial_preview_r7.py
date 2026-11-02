"""One explicitly authorized 3200-pulse/5-RPM live PIXEL contour trial.

Attach only. Real Raw/SLC1 and image coordinates are retained; no calibration,
XYZ, axis, dimension or mechanical one-revolution accuracy is asserted.
"""
from __future__ import annotations
import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT/'src/python', ROOT.parent/'structured_light_system_geometry_v1/src/python', ROOT/'board/scripts'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from continuous_no_laser import (_default_runtime_factory, _validate_policy_manifest,
    _record_error, _json_safe, _write_result_new, _camera_idle, _safe_board_snapshot,
    _restore_sensor, RESTORE_ORDER)
from continuous_scan import _restore_row_feature_mode
from structured_light.motion.pynq_runtime import _motion_confirmation, _driver_readonly_proof
from structured_light.motion.rotating_preview import run_rotating_preview
from structured_light.motion.types import MotionConfig
from structured_light.scan3d.stream_backend import NoFaultClearTransport, GenerationCheckedMMIO
from structured_light.scan3d.stream_capture import capture_features, exclusive_capture_generation
from structured_light.scan3d.stream_chunk_writer import ChunkedAsyncWriter
from structured_light.scan3d.stream_writer import WriterFailure
from structured_light.scan3d.pipeline import DEFAULT_STRIPE_CONFIG, DEFAULT_SELECTION

SCOPE = dict(positions=1, pulses_per_position=3200, total_pulses=3200, rpm=5., direction=0)
_QUARANTINED = []


class _StopSignal:
    def __init__(self, event, path):
        self.event, self.path = event, Path(path) if path is not None else None
        self.last_check = 0.
        self.file_stopped = False
    def is_set(self):
        if self.event.is_set() or self.file_stopped:
            return True
        now = time.monotonic()
        if self.path is not None and now-self.last_check >= .1:
            self.last_check = now
            self.file_stopped = self.path.is_file()
        return self.file_stopped


def _fresh(record, ttl, label, now, *, future_uncertainty_s=0.):
    text = record.get('host_utc')
    if not isinstance(text, str):
        raise ValueError(label + ' requires host_utc')
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})', text)
    if match is None:
        raise ValueError(label + ' requires a timezone-aware ISO host_utc')
    # Windows emits seven fractional digits. Python 3.10 accepts at most
    # microseconds; truncate precision without replacing the original time.
    fraction = '.' + match[2][:6] if match[2] is not None else ''
    zone = '+00:00' if match[3] == 'Z' else match[3]
    stamp = dt.datetime.fromisoformat(match[1] + fraction + zone)
    age = now-stamp.timestamp()
    if -future_uncertainty_s <= age < 0:
        age = now+future_uncertainty_s-stamp.timestamp()
    if stamp.tzinfo is None or not 0 <= age <= ttl:
        raise ValueError(label + ' is stale or future dated')


def validate_authority(motion_confirmation, driver_proof, *, boot_id, bit_sha256, now=None):
    """Physical trial authority is separate from formal optical scan admission."""
    now = time.time() if now is None else now
    motion = _motion_confirmation(motion_confirmation)
    driver = _driver_readonly_proof(driver_proof)
    motion_correlation = motion.get('clock_correlation')
    driver_correlation = driver.get('clock_correlation')
    uncertainty = 0.
    if motion_correlation is not None or driver_correlation is not None:
        if (not isinstance(motion_correlation, dict) or not isinstance(driver_correlation, dict)
                or motion_correlation != driver_correlation):
            raise ValueError('motion and driver require identical clock correlation records')
        correlation = motion_correlation
        if correlation.get('boot_id') != boot_id or correlation.get('bit_sha256') != bit_sha256:
            raise ValueError('clock correlation belongs to another boot or bit')
        for key in ('host_unix', 'board_unix', 'host_minus_board_s', 'uncertainty_s'):
            value = correlation.get(key)
            if type(value) not in (float, int) or not math.isfinite(value):
                raise ValueError('clock correlation requires finite ' + key)
        offset = correlation['host_minus_board_s']
        uncertainty = correlation['uncertainty_s']
        if (not 0 <= uncertainty <= 2 or abs(offset) > 3600
                or not math.isclose(offset, correlation['host_unix']-correlation['board_unix'],
                                    rel_tol=0., abs_tol=1e-6)):
            raise ValueError('clock correlation offset or uncertainty is invalid')
        if abs(now-correlation['board_unix']) > 180+uncertainty:
            raise ValueError('clock correlation sample is stale')
        now += offset
    # The human authorized this one attempt during the active preparation.
    # Keep that authority for the preparation window; the independent driver
    # proof is re-read within 180 seconds, and the host claims the attempt once.
    _fresh(motion, 1800, 'motion confirmation', now, future_uncertainty_s=uncertainty)
    _fresh(driver, 180, 'driver proof', now, future_uncertainty_s=uncertainty)
    if motion.get('scope') != SCOPE:
        raise ValueError('only one 3200-pulse/5-RPM/direction0 trial is authorized')
    if motion.get('boot_id') != boot_id or motion.get('bit_sha256') != bit_sha256:
        raise ValueError('trial authority belongs to another boot or bit')
    return motion, driver


def _display(runtime, manifest, output):
    from structured_light.scan3d.trial_pixel_display import TrialPixelDisplay
    return TrialPixelDisplay(runtime, manifest, output)


def _save_durable(path, record):
    payload = json.dumps(_json_safe(record), ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    with path.open('xb') as stream:
        stream.write(payload); stream.flush(); os.fsync(stream.fileno())
    return {'path': str(path), 'sha256': hashlib.sha256(payload).hexdigest()}


def run_trial_preview(manifest, receipt, output, motion_confirmation, driver_proof, *,
                      roi=(300, 230, 380, 455), exposure_lines=60, stop_event=None,
                      preview_only=False, stop_file=None,
                      runtime_factory=None, sccb_factory=None, capture_fn=None,
                      display_factory=None, coordinator_fn=None, boot_id=None):
    """Finite pixel preview; injected factories exist only for offline checks."""
    config = dataclasses.replace(DEFAULT_STRIPE_CONFIG, roi=tuple(roi))
    if config.roi[2] > 752 or config.roi[3] > 480:
        raise ValueError('ROI exceeds the captured 752x480 frame')
    if type(exposure_lines) is not int or not 1 <= exposure_lines <= 32765:
        raise ValueError('exposure_lines must be in 1..32765')
    manifest, output = Path(manifest), Path(output)
    manifest_record, artifacts = _validate_policy_manifest(manifest)
    if manifest_record.get('compact_policy_dma', {}).get('enabled') is not True:
        raise ValueError('trial requires admitted real SLC1')
    if boot_id is None:
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    motion, driver = validate_authority(motion_confirmation, driver_proof, boot_id=boot_id,
        bit_sha256=artifacts['files']['bit']['sha256'])
    output.mkdir(parents=True, exist_ok=False)
    stop = _StopSignal(stop_event or threading.Event(), stop_file)
    report = dict(schema='live_trial_pixel_preview_r7_v1', passed=False, source='live_trial_pixel',
        scope=SCOPE, boot_id=boot_id, bit_sha256=artifacts['files']['bit']['sha256'],
        overlay_download_count=0, laser_command_count=0, measurement_eligible=False,
        guard_arm_count=0, motor_start_count=0, pulse_budget=3200,
        actual_effect_known=False, xyz_available=False, axis_calibrated=False,
        angle_calibrated=False, physical_accuracy_verified=False, measurement_claim='none',
        stripe_config=dataclasses.asdict(config), stripe_config_fingerprint=config.fingerprint,
        frames=[], prewarm_acknowledged=0, failure=None, secondary_errors=[],
        preview_only=preview_only, motion_requested=not preview_only,
        row_feature_dma={'enabled': False, 'format': 'SLC1', 'admitted': False},
        sensor_restore={'attempted': False, 'verified': False, 'writes': []})
    report['authority_record'] = _save_durable(output/'TRIAL_AUTHORITY.json',
        {'motion_confirmation': motion, 'driver_proof': driver, 'boot_id': boot_id, 'scope': SCOPE})
    runtime = display = writer = access = None
    backup = {}
    sensor_modified = sccb_fault = False
    mode_generation = None
    previous = None
    sensor_trace = []
    frame_lock = threading.RLock()
    stage = 'PREWARM - MOTOR DISABLED'
    started = time.monotonic()
    try:
        runtime = (runtime_factory or _default_runtime_factory)(manifest, load=False,
            power_off_proof=None, receipt=receipt, output=output/'runtime', quality_roi=config.roi)
        # A separate human-authorized preview scope uses the existing fixed
        # coordinator; no formal optical scan admission is changed or bypassed.
        runtime.session.config = MotionConfig(positions=1, pulses_per_position=3200,
            rpm=5., direction=0, host_poll_max_s=.010)
        runtime.motor._transport = NoFaultClearTransport(runtime.motor._transport)
        writer = ChunkedAsyncWriter(output/'frames', capacity=16, group_size=4, max_delay_s=.10)
        display = (display_factory or _display)(runtime, manifest, output)
        display.configure()

        def frame_step():
            nonlocal previous
            if stop.is_set():
                raise RuntimeError('trial stop requested')
            index = len(report['frames'])
            if index >= 512:
                raise RuntimeError('finite trial frame limit reached')
            frame = (capture_fn or capture_features)(runtime.controller, timeout_s=2.,
                                                     require_motion_metadata=True)
            raw_hash = hashlib.sha256(frame.raw).hexdigest()
            if (not isinstance(frame.raw, bytes) or len(frame.raw) != 752*480
                    or raw_hash != frame.raw_sha256 or frame.compact_packet is None
                    or not isinstance(frame.profile_payload, bytes)):
                raise RuntimeError('native Raw/SLC1 integrity failed')
            import numpy as np
            from structured_light.scan3d.feature_candidates import select_compact_candidates
            from structured_light.scan3d.stream_compact import compact_quality
            meta = dict(frame.motion_metadata or {})
            if meta.get('request_id') != frame.request_id or meta.get('capture_epoch') != frame.epoch:
                raise RuntimeError('CSM1 does not identify this Raw/SLC1 pair')
            identity = (frame.epoch, frame.request_id, meta.get('fv_ticks'), meta.get('admitted_event_count'))
            if previous is not None and (identity[0] != previous[0] or identity[1] != previous[1]+1
                    or type(identity[2]) is not int or identity[2] <= previous[2]
                    or type(identity[3]) is not int or identity[3] <= previous[3]):
                raise RuntimeError('capture/CSM1 continuity failed')
            previous = identity
            if stage.startswith('PREWARM') and any(meta.get(key) != report['motor_before'][source]
                    for key, source in (('motor_epoch', 'epoch'), ('motor_accepted_id', 'accepted_id'),
                                        ('emitted_command_count', 'emitted'))):
                raise RuntimeError('motor identity changed during prewarm')
            if not frame.compact_packet.configuration.matches(config):
                raise RuntimeError('actual SLC1 configuration differs from declared pixel configuration')
            (centers, accepted, _, reasons), primary = select_compact_candidates(
                frame.compact_packet, np.frombuffer(frame.raw, np.uint8).reshape(480, 752), config, DEFAULT_SELECTION)
            quality = dataclasses.asdict(compact_quality(frame.compact_packet))
            summary = dict(frame_index=index, stage=stage, source='live_trial_pixel',
                quality=quality, valid_rows=int(accepted.sum()), centers_q12=centers.tolist(),
                accepted_rows=accepted.tolist(), reasons=list(reasons), pl_primary_rows=int(primary.sum()),
                stripe_config_fingerprint=config.fingerprint, xyz_available=False,
                actual_effect_known=False, measurement_eligible=False, angle_calibrated=False)
            item = dict(request_id=frame.request_id, capture_epoch=frame.epoch,
                raw=frame.raw, raw_sha256=raw_hash, profile_payload=frame.profile_payload,
                points_camera_mm=None, points_axis_mm=None, summary=summary, motion=meta,
                exposure=dict(exposure_lines=exposure_lines, exposure_revision=0, effect_known=False,
                              measurement_eligible=False, transition=index < 3, observation_only=True))
            try:
                sequence = writer.submit(item)
            except BufferError:
                if not writer.flush(timeout_s=2.).durable:
                    raise TimeoutError('trial writer backpressure did not drain')
                sequence = writer.submit(item)
            shown = display.show(frame, centers, accepted, reasons, stage)
            if (shown.get('hdmi_acknowledged') is not True or shown.get('request_id') != frame.request_id
                    or shown.get('camera_epoch') != frame.epoch or shown.get('same_frame_overlay') is not True
                    or shown.get('rendered') is not True or shown.get('reference_compared') is not False):
                raise RuntimeError('trial requires actual same-frame HDMI acknowledgment')
            row = dict(request_id=frame.request_id, epoch=frame.epoch, index=index,
                raw_sha256=raw_hash, profile_sha256=hashlib.sha256(frame.profile_payload).hexdigest(),
                valid_rows=int(accepted.sum()), render_receipt=shown, stage=stage,
                writer_sequence=sequence, angle_calibrated=False, xyz_available=False)
            with frame_lock:
                report['frames'].append(row)
            status = {'source': 'live_trial_pixel', 'stage': stage, 'frames': len(report['frames']),
                'last_request_id': frame.request_id, 'pixel_rows': int(accepted.sum()),
                'hdmi_acknowledged': True, 'xyz_available': False, 'measurement_eligible': False}
            temporary = output/'LIVE_STATUS.json.tmp'
            temporary.write_text(json.dumps(status), encoding='utf-8')
            os.replace(temporary, output/'LIVE_STATUS.json')
            return row

        with exclusive_capture_generation(runtime):
            mode_generation = runtime.session._gate.generation
            report['guard_before'], report['motor_before'] = _safe_board_snapshot(runtime)
            admitted = runtime.session._gate.call(mode_generation, runtime.transport.admit_compact_policy_dma,
                                                   manifest, workers_joined=True)
            if not admitted or runtime.transport.compact_policy_dma_enabled is not True:
                raise RuntimeError('SLC1 mode2 admission was not confirmed')
            report['row_feature_dma'].update(enabled=True, admitted=True)
            if sccb_factory is None:
                from structured_light.p2.camera_exposure import SCCBAccess
                access = SCCBAccess(GenerationCheckedMMIO(runtime.transport._diag_mmio, runtime.session._gate), sensor_trace, lambda: None)
            else:
                access = sccb_factory(runtime)
            try:
                sensor = {reg: int(access.read(reg)) for reg in
                    (0, 7, 15, 175, 53, 11, 213, 1, 2, 3, 4, 5, 6, 0x0C, 0x0D, 0xB6)}
                if sensor[0] != 0x1324:
                    raise RuntimeError('sensor ID mismatch')
                from structured_light.p2.camera_exposure import manual_plan
                plan = manual_plan(sensor, coarse_rows=exposure_lines, gain_code=16)
                backup = {reg: sensor[reg] for reg in RESTORE_ORDER}
                if isinstance(receipt, (str, Path)):
                    receipt_identity = {'path': str(Path(receipt).resolve()),
                        'sha256': hashlib.sha256(Path(receipt).read_bytes()).hexdigest()}
                else:
                    receipt_identity = {'path': None, 'source': 'injected_receipt_mapping',
                        'sha256': hashlib.sha256(json.dumps(receipt, sort_keys=True).encode('utf-8')).hexdigest()}
                report['sensor_backup'] = _save_durable(output/'SENSOR_BACKUP.json',
                    {'sensor': sensor, 'restore_registers': backup, 'plan': plan, 'boot_id': boot_id,
                     'receipt': receipt_identity, 'effect_known': False, 'measurement_eligible': False})
                _camera_idle(runtime.controller)
                for reg, value in plan:
                    sensor_modified = True
                    access.write_verified(reg, value)
                if {reg: access.read(reg) for reg, _ in plan} != dict(plan):
                    raise RuntimeError('manual exposure readback mismatch')
            except BaseException:
                sccb_fault = True
                raise
            for _ in range(6 if preview_only else 2):
                frame_step()
                report['prewarm_acknowledged'] += 1
            if not writer.flush(timeout_s=3.).durable:
                raise TimeoutError('prewarm records not durable before motion')
        if not preview_only:
            validate_authority(motion_confirmation, driver_proof, boot_id=boot_id,
                               bit_sha256=artifacts['files']['bit']['sha256'])
            if stop.is_set() or report['prewarm_acknowledged'] != 2:
                raise RuntimeError('trial cancelled or missing two actual HDMI acknowledgments')
            stage = 'ROTATING - ONE FINITE 3200 PULSE TRIAL'
            report['motion_receipt'] = (coordinator_fn or run_rotating_preview)(runtime.session, frame_step, stop_event=stop)
            motion_result = report['motion_receipt'].get('motion', {})
            if (report['motion_receipt'].get('passed') is not True or motion_result.get('start_count') != 1
                    or motion_result.get('requested_pulses') != 3200 or motion_result.get('emitted_pulses') != 3200
                    or motion_result.get('restart_count') != 0):
                raise RuntimeError('finite motion receipt did not confirm exactly one 3200-pulse request')
        report['capture_complete'] = True
    except BaseException as exc:
        _record_error(report, 'trial', exc)
    finally:
        workers_joined = runtime is None or not (runtime.session._live_workers()
            or getattr(runtime.session, '_unconfirmed_workers', False))
        writer_joined = True
        if writer is not None:
            try:
                writer_receipt = writer.close(timeout_s=5.)
            except WriterFailure as exc:
                writer_receipt = exc.receipt
            except BaseException as exc:
                writer_receipt = None
                _record_error(report, 'writer_close', exc)
            report['writer'] = dataclasses.asdict(writer_receipt) if writer_receipt else None
            writer_joined = bool(writer_receipt and writer_receipt.worker_joined and not writer_receipt.worker_unconfirmed)
        display_joined = True
        if display is not None:
            try:
                report['display'] = display.close(timeout_s=5.)
                display_joined = report['display'].get('worker_unconfirmed') is False
                if report['display'].get('passed') is not True:
                    _record_error(report, 'display_close', RuntimeError('display close failed'))
            except BaseException as exc:
                display_joined = False
                _record_error(report, 'display_close', exc)
        unconfirmed = not (workers_joined and writer_joined and display_joined)
        report['worker_unconfirmed'] = unconfirmed
        if runtime is not None and not unconfirmed:
            try:
                if not runtime.session._gate.revoke(timeout_s=2.):
                    raise RuntimeError('trial generation revoke unconfirmed')
                if runtime.session._poisoned:
                    raise RuntimeError('trial session is poisoned; retain hardware')
                with exclusive_capture_generation(runtime):
                    mode_generation = runtime.session._gate.generation
                    _camera_idle(runtime.controller)
                    if sensor_modified and not sccb_fault:
                        if sccb_factory is None:
                            from structured_light.p2.camera_exposure import SCCBAccess
                            access = SCCBAccess(GenerationCheckedMMIO(runtime.transport._diag_mmio, runtime.session._gate), sensor_trace, lambda: None)
                        if access.read(0) != 0x1324:
                            raise RuntimeError('fresh sensor identity mismatch before restore')
                        report['sensor_restore'] = _restore_sensor(access, backup, backup_complete=True)
                        if not report['sensor_restore']['verified']:
                            raise RuntimeError('sensor restore was not confirmed')
                    elif sensor_modified:
                        report['sensor_restore']['status'] = 'SCCB fault: no retries or rollback'
                        report['external_recovery_required'] = True
                    _restore_row_feature_mode(runtime, report, generation=mode_generation,
                                              workers_joined=True, writer_joined=True)
                    report['guard_after'] = runtime.guard.status()
                    report['motor_after'] = runtime.motor.status()
                    completed_motion = report.get('motion_receipt', {}).get('motion', {})
                    completed_receipt = completed_motion.get('motor_receipt')
                    if completed_motion.get('done') and completed_receipt is not None:
                        if any(report['motor_after'].get(key) != completed_receipt.get(key)
                               for key in ('accepted_id', 'epoch', 'emitted')):
                            raise RuntimeError('final motor counter identity differs from completed motion receipt')
                    if (report['guard_after']['active'] or report['guard_after']['step_pin']
                            or not report['guard_after']['enable_pin'] or report['motor_after']['busy']
                            or report['motor_after']['step'] or not report['motor_after']['enable_n']):
                        raise RuntimeError('final disabled output readback failed')
                report['cleanup'] = runtime.close()
            except BaseException as exc:
                _record_error(report, 'cleanup', exc)
                report['external_recovery_required'] = True
                unconfirmed = True
        if unconfirmed and runtime is not None:
            runtime.session._poisoned = True
            runtime.session._external_worker_unconfirmed = True
            report.update(worker_unconfirmed=True, quarantined=True, external_recovery_required=True)
            _QUARANTINED.append((runtime, display, writer))
        report['elapsed_s'] = time.monotonic()-started
        report['sccb_fault'] = sccb_fault
        report['sensor_trace'] = sensor_trace
        report['guard_arm_count'] = report.get('motion_receipt', {}).get('guard', {}).get('arm_count', 0)
        report['motor_start_count'] = report.get('motion_receipt', {}).get('motion', {}).get('start_count', 0)
        report['runtime_closed'] = report.get('cleanup', {}).get('closed') is True
        report['capture_handoff'] = report.get('cleanup', {}).get('capture_handoff')
        report['passed'] = bool(report['failure'] is None and report.get('capture_complete')
            and report.get('cleanup', {}).get('closed') is True and report.get('writer', {}).get('durable')
            and not report.get('external_recovery_required') and not unconfirmed)
        with frame_lock:
            result = _json_safe(report)
        _write_result_new(output/'RESULT.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'receipt', 'output', 'motion-confirmation', 'driver-proof'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--roi', nargs=4, type=int, default=(300, 230, 380, 455))
    parser.add_argument('--exposure-lines', type=int, default=60)
    parser.add_argument('--preview-only', action='store_true', help='six powered-attach frames; no guard ARM or motor START')
    parser.add_argument('--stop-file', type=Path, help='creation requests a bounded stop; no automatic restart')
    args = parser.parse_args(argv)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        result = run_trial_preview(args.manifest, args.receipt, args.output, args.motion_confirmation,
            args.driver_proof, roi=tuple(args.roi), exposure_lines=args.exposure_lines, stop_event=stop,
            preview_only=args.preview_only, stop_file=args.stop_file)
    except Exception as exc:
        print(json.dumps({'passed': False, 'stage': 'entry_error', 'error': type(exc).__name__+': '+str(exc)}))
        return 2
    print(json.dumps({k: v for k, v in result.items() if k != 'frames'}, ensure_ascii=False), flush=True)
    if result.get('quarantined'):
        print('Unconfirmed cleanup: retaining hardware ownership; no automatic restart.', flush=True)
        while True:
            time.sleep(1.)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
