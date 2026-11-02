"""Explicit three-register restore admitted by a newer stable read-only probe."""
from pathlib import Path
import argparse
import datetime as dt
import hashlib
import json
import os
import time
import sensor_readback_only_r9 as readback

TARGET_ORDER=(0x35,0x0B,0xAF)


def validate_inputs(probe_bytes,failed_bytes,bridge_bytes):
    probe=json.loads(probe_bytes.decode('utf-8'));failed=json.loads(failed_bytes.decode('utf-8'))
    bridge=json.loads(bridge_bytes.decode('utf-8'))
    old_handoff=readback.validate_prior(failed)
    if bridge!=failed:
        raise ValueError('probe prior copy differs from original failed receipt JSON')
    result=probe.get('result',{})
    if (probe.get('schema')!='r9.sensor_readback_host.v1' or probe.get('passed') is not True
            or type(probe.get('exit_code')) is not int or probe['exit_code']!=0
            or result.get('schema')!='r9.sensor_readback_only.v1' or result.get('passed') is not True
            or result.get('failure') is not None or result.get('diagnostic_completed') is not True
            or result.get('readings_stable') is not True or result.get('cleanup',{}).get('closed') is not True
            or result.get('prior_handoff')!=old_handoff
            or probe.get('prior_host_receipt_sha256')!=hashlib.sha256(bridge_bytes).hexdigest()
            or result.get('prior_host_receipt_sha256')!=hashlib.sha256(bridge_bytes).hexdigest()
            or any(result.get(k) for k in ('final_identity_error','generation_close_error','cleanup_error'))
            or not all(type(result.get(k)) is int and result[k]==0 for k in
                ('motor_start_count','guard_arm_count','overlay_download_count','capture_count','sensor_write_count'))):
        raise ValueError('probe is not a bound successful stable read-only diagnostic')
    rounds=result.get('rounds')
    if not isinstance(rounds,list) or len(rounds)!=3:
        raise ValueError('probe must have exactly three complete snapshots')
    expected=rounds[0].get('registers',{})
    keys={hex(reg) for reg in readback.REGISTERS}
    if (set(expected)!=keys or any(type(v) is not int or not 0<=v<=65535 for v in expected.values())
            or any(r.get('registers')!=expected for r in rounds)
            or expected['0x0']!=0x1324 or (expected['0x4'],expected['0x3'])!=(752,480)
            or expected['0x7']&0x8000 or expected['0xf']&1 or expected['0xd5']!=0):
        raise ValueError('probe native geometry/mode or snapshot consistency rejected')
    target={reg:failed['result'].get('initial_registers',{}).get(hex(reg)) for reg in TARGET_ORDER}
    if any(type(target[reg]) is not int for reg in TARGET_ORDER) or target!={0x35:16,0x0B:480,0xAF:3}:
        raise ValueError('original backup is not the authorized gain16/shutter480/AF3 restore')
    handoff=result['cleanup'].get('capture_handoff')
    if not isinstance(handoff,str) or not handoff.strip():
        raise ValueError('probe lacks a clean diagnostic handoff')
    return probe,failed,expected,target,handoff


def run_restore(args,*,deps=None,sleep=time.sleep):
    inputs=[args.probe_host_receipt.read_bytes(),args.failed_host_receipt.read_bytes(),args.probe_prior_receipt.read_bytes()]
    probe,failed,expected,target,handoff=validate_inputs(*inputs)
    if args.receipt!=Path(handoff):raise ValueError('attach must use the admitted newer diagnostic handoff')
    deps=deps or readback.dependencies(args.source);base=deps.base
    output=args.output;output.mkdir(parents=True,exist_ok=False)
    state=dict(schema='r9.sensor_restore_checked.v1',passed=False,failure=None,initial_rounds=[],final_rounds=[],
        frames=[],discarded=[],is_capture=False,measurement_eligible=False,sensor_restored=False,
        historical_anomaly_resolved=False,quarantined_batch_released=False,external_recovery_required=True,
        restore_strategy='independent explicit restore after new stable probe and fresh matching snapshots; not blind rollback',
        probe_host_receipt_sha256=hashlib.sha256(inputs[0]).hexdigest(),
        failed_host_receipt_sha256=hashlib.sha256(inputs[1]).hexdigest(),
        probe_prior_receipt_sha256=hashlib.sha256(inputs[2]).hexdigest(),prior_handoff=handoff,
        motor_start_count=0,guard_arm_count=0,overlay_download_count=0,capture_count=0,display_requested=False,
        sensor_write_attempts=[],sensor_write_count=0,
        cleanup_policy='existing protective guard.revoke/session.close; not zero MMIO writes')
    runtime=scope=None;trace=[]

    def save(path,value,exclusive=True):
        with path.open('x' if exclusive else 'w',encoding='utf-8') as stream:
            json.dump(base._json_safe(value),stream,indent=2,allow_nan=False);stream.flush();os.fsync(stream.fileno())

    def save_trace():
        temporary=output/'SCCB_TRACE.json.tmp';save(temporary,trace,False);temporary.replace(output/'SCCB_TRACE.json')

    def check():
        base._camera_idle(runtime.controller)
        guard,motor=base._safe_board_snapshot(runtime)
        if not base._same_motor_identity(state['motor_before'],motor):raise RuntimeError('motor identity changed')
        if any(guard.get(k)!=state['guard_before'].get(k) for k in ('version','request_id','accepted_id','carrier_mode')):
            raise RuntimeError('guard command identity changed')
        identity=dict(request_id=int(runtime.transport.mmio.request_id),epoch=int(runtime.transport.mmio.epoch))
        if identity!=state['capture_identity_before']:raise RuntimeError('capture identity changed')
        return guard,motor,identity

    def snapshots(name,desired):
        for index in range(3):
            if index:sleep(.1)
            current=dict(index=index,started_utc=dt.datetime.now(dt.timezone.utc).isoformat(),registers={})
            state[name].append(current)
            for reg in readback.REGISTERS:
                check();value=access.read(reg);current['registers'][hex(reg)]=value
                if type(value) is not int or value!=desired[hex(reg)]:
                    raise RuntimeError(name+' mismatch at '+hex(reg))
            current['completed_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
            save(output/f'{name}_{index}.json',current)

    try:
        base._validate_policy_manifest(args.manifest)
        runtime=base._default_runtime_factory(args.manifest,load=False,power_off_proof=None,
            receipt=args.receipt,output=output/'runtime',quality_roi=(260,350,470,465))
        scope=deps.scope(runtime);scope.__enter__();base._camera_idle(runtime.controller)
        state['guard_before'],state['motor_before']=base._safe_board_snapshot(runtime)
        state['capture_identity_before']=dict(request_id=int(runtime.transport.mmio.request_id),epoch=int(runtime.transport.mmio.epoch))
        if (state['capture_identity_before']!=probe['result']['capture_identity_after']
                or not base._same_motor_identity(probe['result']['motor_after'],state['motor_before'])):
            raise RuntimeError('current capture/motor identity differs from successful probe')
        access=deps.access(deps.gated_mmio(runtime.transport._diag_mmio,runtime.session._gate),trace,save_trace)
        save_trace();snapshots('initial_rounds',expected)
        state['initial_verified']=True
        for reg in TARGET_ORDER:
            check()
            state['sensor_write_attempts'].append(dict(register=hex(reg),value=target[reg]))
            save(output/f'WRITE_INTENT_{reg:02x}.json',state['sensor_write_attempts'][-1])
            access.write_verified(reg,target[reg])
            state['sensor_write_count']+=1
        final_expected=dict(expected);final_expected.update({hex(reg):value for reg,value in target.items()})
        snapshots('final_rounds',final_expected)
        state['restore_verified']=True
    except BaseException as exc:
        state['failure']=type(exc).__name__+': '+str(exc)
    finally:
        if runtime is not None:
            try:state['guard_after'],state['motor_after'],state['capture_identity_after']=check()
            except BaseException as exc:state['final_identity_error']=type(exc).__name__+': '+str(exc)
        deps.close(state,scope=scope,display=None,runtime=runtime,profiler=None,output=output)
        state['sccb_trace']=trace
        state['passed']=bool(state.get('restore_verified') and not state['failure'] and not state.get('final_identity_error')
            and not state.get('generation_close_error') and not state.get('cleanup_error')
            and state.get('cleanup',{}).get('closed') is True and state['cleanup'].get('capture_handoff'))
        state['sensor_restored']=state['passed'];state['external_recovery_required']=not state['passed']
        state['restore']=dict(verified=state['passed'],strategy=state['restore_strategy'],
            target={hex(k):v for k,v in target.items()},external_recovery_required=not state['passed'])
        save(output/'RESULT.json',state)
    return state


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','manifest','receipt','output','probe-host-receipt','failed-host-receipt','probe-prior-receipt'):
        parser.add_argument('--'+name,type=Path,required=True)
    result=run_restore(parser.parse_args(argv))
    print(json.dumps(dict(passed=result['passed'],failure=result['failure'],cleanup=result.get('cleanup'))))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
