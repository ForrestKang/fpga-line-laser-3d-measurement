"""Three bounded SCCB READ snapshots; no sensor write/reset/restore or capture."""
from pathlib import Path
import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
from types import SimpleNamespace

REGISTERS = (0, 1, 2, 3, 4, 5, 6, 7, 0x0D, 0x0F, 0x35, 0x0B, 0xAF, 0xD5)
WINDOW_MISMATCH = 'RuntimeError: camera identity/window/mode changed before restore'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_prior(value):
    """Only the explicitly diagnosed, cleanly closed failed camera batch."""
    if not isinstance(value, dict) or value.get('passed') is not False:
        raise ValueError('prior HOST_RECEIPT must preserve failed status')
    result = value.get('result', {})
    if not isinstance(result, dict):
        raise ValueError('prior result must be an object')
    cleanup = result.get('cleanup', {})
    restore = result.get('restore', {})
    if not isinstance(cleanup, dict) or not isinstance(restore, dict):
        raise ValueError('prior cleanup/restore must be objects')
    if (value.get('schema') != 'r9.camera_intrinsics_host.v1'
            or result.get('schema') != 'r9.camera_intrinsics_capture.v1'
            or result.get('passed') is not False or result.get('failure') is not None
            or result.get('final_readback_error') != WINDOW_MISMATCH
            or cleanup.get('closed') is not True
            or not isinstance(cleanup.get('capture_handoff'), str)
            or not cleanup['capture_handoff'].strip()
            or restore.get('verified') is not False
            or restore.get('status') != 'sccb_fault_no_blind_rollback'
            or restore.get('external_recovery_required') is not True
            or any(result.get(key) for key in ('generation_close_error', 'cleanup_error'))
            or not all(type(result.get(key)) is int and result[key] == 0 for key in
                       ('motor_start_count', 'guard_arm_count', 'overlay_download_count'))):
        raise ValueError('prior failure is not the admitted clean window mismatch')
    return cleanup['capture_handoff']


def dependencies(source):
    sys.path.insert(0, str(source/'board/scripts'))
    import continuous_no_laser as base
    from continuous_board import close_static_resources
    from structured_light.scan3d.stream_capture import exclusive_capture_generation
    from structured_light.scan3d.stream_backend import GenerationCheckedMMIO
    from structured_light.p2.camera_exposure import SCCBAccess
    return SimpleNamespace(base=base, close=close_static_resources,
        scope=exclusive_capture_generation, gated_mmio=GenerationCheckedMMIO, access=SCCBAccess)


def run_readback(args, *, deps=None, sleep=time.sleep):
    prior_bytes = args.prior_host_receipt.read_bytes()
    prior = json.loads(prior_bytes.decode('utf-8'))
    handoff = validate_prior(prior)
    if args.receipt != Path(handoff):
        raise ValueError('attach receipt differs from bound prior clean handoff')
    deps = deps or dependencies(args.source); base = deps.base
    output = args.output; output.mkdir(parents=True, exist_ok=False)
    state = dict(schema='r9.sensor_readback_only.v1', passed=False, rounds=[], failure=None,
        diagnostic_only=True, measurement_eligible=False, sensor_restored=False,
        external_recovery_required=True, prior_fault_cleared=False,
        prior_host_receipt_sha256=hashlib.sha256(prior_bytes).hexdigest(), prior_handoff=handoff,
        motor_start_count=0, guard_arm_count=0, overlay_download_count=0,
        capture_count=0, sensor_write_count=0, display_requested=False,
        cleanup_policy='existing protective guard.revoke/session.close; not zero MMIO writes',
        requested_rounds=3, interval_s=.1, registers=[hex(reg) for reg in REGISTERS])
    runtime = scope = None; trace=[]

    def write(path, value, exclusive=True):
        with path.open('x' if exclusive else 'w', encoding='utf-8') as stream:
            json.dump(base._json_safe(value), stream, indent=2, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())

    def save_trace():
        temporary=output/'SCCB_TRACE.json.tmp'
        write(temporary, trace, exclusive=False); temporary.replace(output/'SCCB_TRACE.json')

    def check():
        base._camera_idle(runtime.controller)
        guard,motor=base._safe_board_snapshot(runtime)
        if not base._same_motor_identity(state['motor_before'],motor):
            raise RuntimeError('motor identity changed during read-only diagnostic')
        if any(guard.get(key) != state['guard_before'].get(key)
               for key in ('version','request_id','accepted_id','carrier_mode')):
            raise RuntimeError('guard command identity changed during read-only diagnostic')
        identity=dict(request_id=int(runtime.transport.mmio.request_id),epoch=int(runtime.transport.mmio.epoch))
        if identity != state['capture_identity_before']:
            raise RuntimeError('capture identity changed without a diagnostic capture request')
        return guard,motor,identity

    try:
        base._validate_policy_manifest(args.manifest)
        runtime=base._default_runtime_factory(args.manifest,load=False,power_off_proof=None,
            receipt=args.receipt,output=output/'runtime',quality_roi=(260,350,470,465))
        scope=deps.scope(runtime);scope.__enter__()
        base._camera_idle(runtime.controller)
        state['guard_before'],state['motor_before']=base._safe_board_snapshot(runtime)
        state['capture_identity_before']=dict(request_id=int(runtime.transport.mmio.request_id),
                                              epoch=int(runtime.transport.mmio.epoch))
        access=deps.access(deps.gated_mmio(runtime.transport._diag_mmio,runtime.session._gate),trace,save_trace)
        save_trace()
        for index in range(3):
            if index:
                sleep(.1)
            check()
            current=dict(index=index,started_utc=dt.datetime.now(dt.timezone.utc).isoformat(),registers={})
            state['rounds'].append(current)
            for reg in REGISTERS:
                check()
                value=access.read(reg)
                if type(value) is not int or not 0 <= value <= 65535:
                    raise RuntimeError('invalid 16-bit SCCB read value')
                current['registers'][hex(reg)]=value
                if reg==0 and value != 0x1324:
                    raise RuntimeError('unexpected camera ID')
                if index and value != state['rounds'][0]['registers'][hex(reg)]:
                    raise RuntimeError('register changed between snapshots: '+hex(reg))
            current['completed_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
            write(output/f'ROUND_{index}.json',current)
        state['diagnostic_completed']=True
        state['readings_stable']=True
        first=state['rounds'][0]['registers']
        state['differences_from_prior']={name:{reg:dict(prior=value,current=first[reg])
            for reg,value in prior['result'].get(name,{}).items() if reg in first and value != first[reg]}
            for name in ('initial_registers','final_registers_before_restore')}
    except BaseException as exc:
        state['failure']=type(exc).__name__+': '+str(exc)
    finally:
        if runtime is not None:
            try:
                state['guard_after'],state['motor_after'],state['capture_identity_after']=check()
            except BaseException as exc:
                state['final_identity_error']=type(exc).__name__+': '+str(exc)
        deps.close(state,scope=scope,display=None,runtime=runtime,profiler=None,output=output)
        state['sccb_trace']=trace
        state['passed']=bool(state.get('diagnostic_completed') and not state['failure']
            and not state.get('final_identity_error') and not state.get('generation_close_error')
            and not state.get('cleanup_error') and state.get('cleanup',{}).get('closed') is True
            and isinstance(state.get('cleanup',{}).get('capture_handoff'),str)
            and state['cleanup']['capture_handoff'].strip())
        write(output/'RESULT.json',state)
    return state


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','manifest','receipt','prior-host-receipt','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    result=run_readback(parser.parse_args(argv))
    print(json.dumps(dict(passed=result['passed'],failure=result['failure'],cleanup=result.get('cleanup'))))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
