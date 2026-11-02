"""Finite attach-only laser-OFF camera corpus; no display or motor commands."""
from pathlib import Path
import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import sys
import time

SNAPSHOT_REGISTERS = (0, 7, 15, 175, 53, 11, 213, 0xBA, 0xBB, 0x0D, 1, 2, 3, 4, 5, 6)
WINDOW_MODE_REGISTERS = (0, 7, 15, 0xD5, 0x0D, 1, 2, 3, 4, 5, 6)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def validate_limits(exposure_lines, max_observations, period_s, duration_s, ready_timeout_s):
    for name, value, upper in [('exposure-lines', exposure_lines, 480),
                               ('max-observations', max_observations, 60)]:
        if type(value) is not int or not 1 <= value <= upper:
            raise ValueError(name+' must be an integer in 1..'+str(upper))
    for name, value, lower, upper in [('period-s', period_s, .5, 5.),
            ('duration-s', duration_s, 0., 45.), ('ready-timeout-s', ready_timeout_s, 0., 120.)]:
        if (type(value) not in (int, float) or not math.isfinite(value)
                or value > upper or value < lower or (lower == 0 and value == 0)):
            raise ValueError(name+' is outside its finite allowed range')


def validate_operator_state(value):
    if not isinstance(value, dict) or value.get('laser_off') is not True or value.get('motor_12v_off') is not True:
        raise ValueError('operator state requires explicit laser_off=true and motor_12v_off=true')
    if not isinstance(value.get('statement'), str) or not value['statement'].strip():
        raise ValueError('operator state requires the actual confirmation statement')
    return value


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'manifest', 'receipt', 'output', 'operator-state'):
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--exposure-lines', type=int, required=True)
    p.add_argument('--max-observations', type=int, default=60)
    p.add_argument('--period-s', type=float, default=.5)
    p.add_argument('--duration-s', type=float, default=45.)
    p.add_argument('--ready-timeout-s', type=float, default=120.)
    return p


def dependencies(source):
    sys.path.insert(0, str(source/'board/scripts'))
    import continuous_no_laser as base
    from continuous_board import close_static_resources
    from structured_light.scan3d.stream_capture import capture_features, exclusive_capture_generation
    from structured_light.scan3d.stream_backend import GenerationCheckedMMIO
    from structured_light.p2.camera_exposure import SCCBAccess, manual_plan
    from types import SimpleNamespace
    return SimpleNamespace(base=base, close=close_static_resources, capture=capture_features,
        scope=exclusive_capture_generation, gated_mmio=GenerationCheckedMMIO,
        access=SCCBAccess, manual_plan=manual_plan)


def run_capture(args, *, deps=None, clock=time.monotonic, sleep=time.sleep):
    validate_limits(args.exposure_lines, args.max_observations, args.period_s, args.duration_s, args.ready_timeout_s)
    operator = validate_operator_state(json.loads(args.operator_state.read_text(encoding='utf-8')))
    deps = deps or dependencies(args.source)
    base = deps.base
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    state = dict(schema='r9.camera_intrinsics_capture.v1', passed=False, frames=[], discarded=[],
        capture_kind='current_camera_intrinsics', capture_batch_id=output.parent.name,
        physical_accuracy_verified=False,
        levels=[args.exposure_lines], measurement_eligible=False, actual_exposure_effect_verified=False,
        motor_start_count=0, guard_arm_count=0, overlay_download_count=0, display_requested=False,
        physical_motor_12v='OFF per bound operator statement', physical_laser='OFF per bound operator statement',
        operator_state=operator, operator_state_sha256=digest(args.operator_state),
        operator_state_path=str(args.operator_state), exposure_lines=args.exposure_lines,
        max_observations=args.max_observations, period_s=args.period_s, duration_s=args.duration_s,
        ready_timeout_s=args.ready_timeout_s, transition_count=3, failure=None, phase='initializing')
    runtime = scope = access = None
    backup = {}; trace = []; modified = False; sccb_fault = False
    last_id = previous = None

    def save(path, value):
        with path.open('x', encoding='utf-8') as stream:
            json.dump(base._json_safe(value), stream, indent=2, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())

    def status():
        path = output/'STATUS.json'; temporary = output/'STATUS.json.tmp'
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(dict(phase=state['phase'], observations=len(state['frames']),
                discarded=len(state['discarded']), failure=state['failure']), stream)
            stream.flush(); os.fsync(stream.fileno())
        temporary.replace(path)

    def check_motor():
        guard, motor = base._safe_board_snapshot(runtime)
        if not base._same_motor_identity(state['motor_before'], motor):
            raise RuntimeError('motor command identity changed')
        return guard, motor

    def read_snapshot():
        nonlocal sccb_fault
        try:
            return {reg: int(access.read(reg)) for reg in SNAPSHOT_REGISTERS}
        except BaseException:
            sccb_fault = True
            raise

    try:
        base._validate_policy_manifest(args.manifest)
        runtime = base._default_runtime_factory(args.manifest, load=False, power_off_proof=None,
            receipt=args.receipt, output=output/'runtime', quality_roi=(260, 350, 470, 465))
        scope = deps.scope(runtime); scope.__enter__()
        state['guard_before'], state['motor_before'] = base._safe_board_snapshot(runtime)
        last_id = int(runtime.transport.mmio.request_id)
        expected_epoch = int(runtime.transport.mmio.epoch)
        state['initial_capture_identity'] = dict(request_id=last_id, epoch=expected_epoch)
        access = deps.access(deps.gated_mmio(runtime.transport._diag_mmio, runtime.session._gate), trace, lambda: None)
        snapshot = read_snapshot()
        if snapshot[0] != 0x1324:
            raise RuntimeError('unexpected camera ID')
        backup = {reg: snapshot[reg] for reg in base.RESTORE_ORDER}
        state['initial_registers'] = {hex(k):v for k,v in snapshot.items()}
        save(output/'SENSOR_BACKUP.json', state['initial_registers'])
        plan = deps.manual_plan(snapshot, coarse_rows=args.exposure_lines, gain_code=16)
        base._camera_idle(runtime.controller)
        try:
            for reg, value in plan:
                modified = True; access.write_verified(reg, value)
            readback = {hex(reg):int(access.read(reg)) for reg in base.RESTORE_ORDER}
            if readback != {'0x35':16, '0xb':args.exposure_lines, '0xaf':0}:
                raise RuntimeError('manual exposure/gain readback mismatch')
        except BaseException:
            sccb_fault = True
            raise
        if (output/'START.flag').exists():
            raise RuntimeError('START.flag existed before READY')
        check_motor()
        state['phase'] = 'waiting_for_start'
        save(output/'READY.json', dict(schema='r9.camera_intrinsics_ready.v1',
            ready_utc=utc_now(), capture_batch_id=state['capture_batch_id'],
            operator_state_sha256=state['operator_state_sha256'], exposure_lines=args.exposure_lines,
            register_readback=readback, max_observations=args.max_observations,
            period_s=args.period_s, duration_s=args.duration_s,
            ready_timeout_s=args.ready_timeout_s, captures_before_start=0))
        status()
        wait_deadline = clock()+args.ready_timeout_s
        while True:
            check_motor()
            if clock() >= wait_deadline:
                raise TimeoutError('START.flag was not received before READY deadline')
            if (output/'START.flag').is_file():
                break
            sleep(min(.1, wait_deadline-clock()))
        check_motor()
        trigger = json.loads((output/'START.flag').read_text(encoding='utf-8'))
        if (not isinstance(trigger, dict) or trigger.get('ready_batch_id') != state['capture_batch_id']
                or trigger.get('host_triggered') is not True):
            raise ValueError('START.flag does not bind this READY batch')
        state['start_flag_sha256'] = digest(output/'START.flag')
        state['start_utc'] = utc_now()
        state['phase'] = 'capturing'; status()
        started = clock(); deadline = started+args.duration_s; next_capture = started
        index = 0
        while len(state['frames']) < args.max_observations:
            check_motor()
            now = clock()
            # Never submit a DMA request with an artificially shortened timeout.
            if deadline-now < 2.:
                break
            if now < next_capture:
                sleep(min(.1, next_capture-now, deadline-now))
                continue
            frame = deps.capture(runtime.controller, timeout_s=2., require_motion_metadata=True)
            if (type(frame.request_id) is not int or frame.request_id != ((last_id+1) & 0xffffffff)
                    or frame.epoch != expected_epoch):
                raise RuntimeError('nonconsecutive capture ID')
            last_id = frame.request_id
            previous = base._check_motion_metadata(frame.motion_metadata, frame.request_id, frame.epoch,
                state['motor_before'], previous)
            check_motor()
            if len(frame.raw) != 752*480:
                raise RuntimeError('native raw length mismatch')
            record = dict(request_id=frame.request_id, epoch=frame.epoch,
                capture_batch_id=state['capture_batch_id'], saved_utc=utc_now(),
                exposure_lines=args.exposure_lines, gain_code=16, exposure_revision=0,
                register_readback=readback, actual_effect_known=False,
                three_frames_discarded_before_observation=True,
                raw_sha256=hashlib.sha256(frame.raw).hexdigest(),
                profile_sha256=hashlib.sha256(frame.profile_payload).hexdigest(),
                motion=frame.motion_metadata, discarded_transition=index<3,
                saved_elapsed_s=clock()-started, time_basis='host save time, not exposure trigger')
            folder = output/f'frame_{frame.request_id:010d}'; folder.mkdir()
            for name, payload in [('raw.gray8.bin', frame.raw), ('profile.bin', frame.profile_payload)]:
                with (folder/name).open('xb') as stream:
                    stream.write(payload); stream.flush(); os.fsync(stream.fileno())
            save(folder/'frame.json', dict(record, diagnostics=dataclasses.asdict(frame.diagnostics)))
            (state['discarded'] if index<3 else state['frames']).append(record)
            index += 1
            # Transition frames are finite immediate settling captures. The
            # observation cadence starts after them and never catches up in a burst.
            next_capture = clock()+(args.period_s if index>3 else 0.)
            status()
        state.update(capture_complete=bool(len(state['discarded']) == 3 and state['frames']),
            stop_reason='observation_limit' if len(state['frames']) == args.max_observations else 'duration_limit',
            observation_count=len(state['frames']), capture_elapsed_s=clock()-started)
        if not state['capture_complete']:
            raise RuntimeError('capture window yielded no complete post-transition observation')
    except BaseException as exc:
        state['failure'] = type(exc).__name__+': '+str(exc)
    finally:
        state['phase'] = 'closing'
        if runtime is not None:
            try:
                base._camera_idle(runtime.controller)
                state['guard_after'], state['motor_after'] = check_motor()
                if access is not None and modified:
                    if not sccb_fault:
                        try:
                            final = read_snapshot()
                            state['final_registers_before_restore'] = {hex(k):v for k,v in final.items()}
                            if any(final[k] != snapshot[k] for k in WINDOW_MODE_REGISTERS):
                                raise RuntimeError('camera identity/window/mode changed before restore')
                            if (final[0xAF], final[0x35], final[0x0B]) != (0, 16, args.exposure_lines):
                                raise RuntimeError('manual exposure/gain changed before restore')
                        except BaseException as exc:
                            sccb_fault = True
                            state['final_readback_error'] = type(exc).__name__+': '+str(exc)
                    if sccb_fault:
                        state['restore'] = dict(verified=False, status='sccb_fault_no_blind_rollback',
                                                external_recovery_required=True)
                    else:
                        state['restore'] = base._restore_sensor(access, backup, backup_complete=True)
            except BaseException as exc:
                state['final_readback_error'] = type(exc).__name__+': '+str(exc)
        deps.close(state, scope=scope, display=None, runtime=runtime, profiler=None, output=output)
        state['sccb_trace'] = trace
        state['passed'] = bool(state.get('capture_complete') and not state['failure']
            and not state.get('final_readback_error') and state.get('restore', {}).get('verified') is True
            and state.get('cleanup', {}).get('closed') is True
            and not state.get('generation_close_error') and not state.get('cleanup_error'))
        state['phase'] = 'complete' if state['passed'] else 'failed'
        status(); save(output/'RESULT.json', state)
        save(output/'DONE.json', dict(passed=state['passed'], exit_code=0 if state['passed'] else 1))
    return state


def main():
    p = parser(); args = p.parse_args()
    try:
        validate_limits(args.exposure_lines, args.max_observations, args.period_s, args.duration_s, args.ready_timeout_s)
    except ValueError as exc:
        p.error(str(exc))
    result = run_capture(args)
    print(json.dumps({k:v for k,v in result.items() if k not in ('frames','discarded','sccb_trace')}, default=str))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
