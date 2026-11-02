"""Bounded camera-only exposure bracket on the existing checked bit; no motion."""
from pathlib import Path
import argparse
import dataclasses
import hashlib
import json
import os
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--receipt',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--levels',type=int,nargs='+',default=[120,60,30])
    args=parser.parse_args()
    if not 1<=len(args.levels)<=4 or any(not 1<=x<=480 for x in args.levels):
        parser.error('one to four levels in 1..480 are required')
    sys.path.insert(0,str(args.source/'board/scripts'))
    import continuous_no_laser as base
    from continuous_board import close_static_resources
    from structured_light.scan3d.stream_capture import capture_features,exclusive_capture_generation
    from structured_light.scan3d.stream_backend import GenerationCheckedMMIO
    from structured_light.p2.camera_exposure import SCCBAccess,manual_plan
    output=args.output
    output.mkdir(parents=True,exist_ok=False)
    state=dict(schema='r9.stationary_exposure_bracket.v1',passed=False,frames=[],discarded=[],
               levels=args.levels,measurement_eligible=False,actual_exposure_effect_verified=False,
               motor_start_count=0,guard_arm_count=0,overlay_download_count=0,
               physical_motor_12v='not_reconfirmed',failure=None)
    runtime=scope=access=None
    backup={};trace=[];modified=False;sccb_fault=False;last_id=None;previous=None
    def save(path,value):
        with path.open('x',encoding='utf-8') as f:
            json.dump(base._json_safe(value),f,indent=2,allow_nan=False);f.flush();os.fsync(f.fileno())
    try:
        base._validate_policy_manifest(args.manifest)
        runtime=base._default_runtime_factory(args.manifest,load=False,power_off_proof=None,
            receipt=args.receipt,output=output/'runtime',quality_roi=(260,350,470,465))
        scope=exclusive_capture_generation(runtime);scope.__enter__()
        state['guard_before'],state['motor_before']=base._safe_board_snapshot(runtime)
        access=SCCBAccess(GenerationCheckedMMIO(runtime.transport._diag_mmio,runtime.session._gate),trace,lambda:None)
        snapshot={reg:int(access.read(reg)) for reg in (0,7,15,175,53,11,213,0xBA,0xBB,0x0D,0x01,0x02,0x03,0x04,0x05,0x06)}
        if snapshot[0]!=0x1324:raise RuntimeError('unexpected camera ID')
        backup={reg:snapshot[reg] for reg in base.RESTORE_ORDER}
        state['initial_registers']={hex(k):v for k,v in snapshot.items()}
        save(output/'SENSOR_BACKUP.json',state['initial_registers'])
        for revision,level in enumerate(args.levels):
            plan=manual_plan(snapshot,coarse_rows=level,gain_code=16)
            base._camera_idle(runtime.controller)
            try:
                for reg,value in plan:
                    modified=True;access.write_verified(reg,value)
                readback={hex(reg):int(access.read(reg)) for reg in base.RESTORE_ORDER}
            except BaseException:
                sccb_fault=True;raise
            for index in range(5):
                frame=capture_features(runtime.controller,timeout_s=2.,require_motion_metadata=True)
                if last_id is not None and frame.request_id!=last_id+1:
                    raise RuntimeError('nonconsecutive capture ID')
                last_id=frame.request_id
                previous=base._check_motion_metadata(frame.motion_metadata,frame.request_id,frame.epoch,
                    state['motor_before'],previous)
                _,motor=base._safe_board_snapshot(runtime)
                if not base._same_motor_identity(state['motor_before'],motor):
                    raise RuntimeError('motor identity changed')
                record=dict(request_id=frame.request_id,epoch=frame.epoch,exposure_lines=level,
                    gain_code=16,exposure_revision=revision,register_readback=readback,
                    actual_effect_known=False,three_frames_discarded_before_observation=True,
                    raw_sha256=hashlib.sha256(frame.raw).hexdigest(),
                    profile_sha256=hashlib.sha256(frame.profile_payload).hexdigest(),motion=frame.motion_metadata)
                folder=output/f'frame_{frame.request_id:010d}';folder.mkdir()
                for name,payload in [('raw.gray8.bin',frame.raw),('profile.bin',frame.profile_payload)]:
                    with (folder/name).open('xb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
                record['discarded_transition']=index<3
                record['diagnostics']=dataclasses.asdict(frame.diagnostics)
                save(folder/'frame.json',record)
                (state['discarded'] if index<3 else state['frames']).append({k:v for k,v in record.items() if k!='diagnostics'})
        state['capture_complete']=True
    except BaseException as exc:
        state['failure']=type(exc).__name__+': '+str(exc)
    finally:
        if runtime is not None:
            try:
                base._camera_idle(runtime.controller)
                state['guard_after'],state['motor_after']=base._safe_board_snapshot(runtime)
                if not base._same_motor_identity(state['motor_before'],state['motor_after']):
                    raise RuntimeError('motor count/identity changed')
                if access is not None and modified:
                    if sccb_fault:
                        state['restore']={'verified':False,'status':'sccb_fault_no_blind_rollback','external_recovery_required':True}
                    else:
                        if access.read(0)!=0x1324:raise RuntimeError('camera identity differs before restore')
                        state['restore']=base._restore_sensor(access,backup,backup_complete=True)
            except BaseException as exc:
                state['final_readback_error']=type(exc).__name__+': '+str(exc)
        close_static_resources(state,scope=scope,display=None,runtime=runtime,profiler=None,output=output)
        state['sccb_trace']=trace
        state['passed']=bool(state.get('capture_complete') and not state['failure'] and not state.get('final_readback_error')
            and state.get('restore',{}).get('verified') and state.get('cleanup',{}).get('closed')
            and not state.get('generation_close_error') and not state.get('cleanup_error'))
        save(output/'RESULT.json',state)
    print(json.dumps({k:v for k,v in state.items() if k not in ('frames','discarded','sccb_trace')},default=str))
    return 0 if state['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
