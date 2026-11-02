"""Finite LIVE HDMI preview with explicit one-command/one-frame manual snapshots."""
from pathlib import Path
import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import time
from types import SimpleNamespace

REGISTERS=(0,1,2,3,4,5,6,7,0x0D,0x0F,0x35,0x0B,0xAF,0xD5)
FIXED_REGISTERS=(0,1,2,3,4,5,6,7,0x0D,0x0F,0xD5)


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def utc():return dt.datetime.now(dt.timezone.utc).isoformat()


def validate_limits(duration_s,max_manual_shots):
    if type(duration_s) not in (int,float) or not math.isfinite(duration_s) or not 2<=duration_s<=900:
        raise ValueError('duration-s must be finite in 2..900')
    if type(max_manual_shots) is not int or not 1<=max_manual_shots<=30:
        raise ValueError('max-manual-shots must be an integer in 1..30')


def validate_operator(operator):
    if (not isinstance(operator,dict) or operator.get('laser_off') is not True
            or operator.get('motor_12v_off') is not True or not isinstance(operator.get('statement'),str)
            or not operator['statement'].strip()):
        raise ValueError('explicit laser OFF/motor12V OFF operator statement required')


def validate_prior(prior):
    if (not isinstance(prior,dict) or prior.get('passed') is not True
            or prior.get('result',{}).get('passed') is not True
            or prior['result'].get('cleanup',{}).get('closed') is not True):
        raise ValueError('latest receipt must be a genuinely successful closed handoff')
    handoff=prior['result']['cleanup'].get('capture_handoff')
    if not isinstance(handoff,str) or not handoff.strip():raise ValueError('latest receipt lacks clean handoff')
    return handoff


def render_canvas(raw,request_id,preview_count,manual_count,*,transition=False):
    import cv2
    import numpy as np
    if not isinstance(raw,bytes) or len(raw)!=752*480:raise ValueError('native 752x480 raw required')
    canvas=np.zeros((720,1280,3),np.uint8)
    gray=np.frombuffer(raw,np.uint8).reshape(480,752)
    large=cv2.resize(gray,(971,620),interpolation=cv2.INTER_NEAREST)
    canvas[50:670,154:1125]=large[:,:,None]
    cv2.rectangle(canvas,(185,73),(1094,646),(0,210,255),2)
    def text(value,x,y,scale=.64,color=(230,230,230)):
        cv2.putText(canvas,value,(x,y),cv2.FONT_HERSHEY_SIMPLEX,scale,color,1,cv2.LINE_AA)
    text('LIVE CAMERA / CALIBRATION PENDING',16,30,.73,(80,220,255))
    text('12V OFF / LASER OFF',966,28,.57,(100,255,120))
    text(f'FRAME {preview_count} / REQUEST {request_id}',18,694,.62)
    text(f'SAVED MANUAL {manual_count}',780,694,.62)
    text('KEEP THE WHOLE CHECKERBOARD INSIDE GUIDE - HOLD STILL, THEN SNAP',173,716,.51,(100,220,255))
    if transition:text('EXPOSURE SETTLING / TRANSITION',400,48,.48,(100,210,255))
    return np.ascontiguousarray(canvas)


def dependencies(source):
    sys.path.insert(0,str(source/'board/scripts'))
    import continuous_no_laser as base
    from continuous_board import close_static_resources
    from structured_light.scan3d.stream_capture import capture_features,exclusive_capture_generation
    from structured_light.scan3d.stream_backend import GenerationCheckedMMIO
    from structured_light.p2.camera_exposure import SCCBAccess,manual_plan
    def display_factory(runtime,manifest,output):
        from structured_light.p2.hdmi_owner import RemoteHDMIOutput
        # Original motion basename is supported by the current checked owner.
        # Do not use the older filename-alias copier.
        os.environ['SL_HDMI_PROCESSED_CANVAS']='1'
        return RemoteHDMIOutput(manifest,owner_log=output/'HDMI_OWNER.log',timeout_s=5.,
            startup_timeout_s=60.,preview_only=True,hardware_lock_fd=runtime.hardware_lock._handle.fileno())
    return SimpleNamespace(base=base,close=close_static_resources,capture=capture_features,
        scope=exclusive_capture_generation,gated_mmio=GenerationCheckedMMIO,access=SCCBAccess,
        manual_plan=manual_plan,display_factory=display_factory,render=render_canvas)


def run_live(args,*,deps=None,clock=time.monotonic):
    validate_limits(args.duration_s,args.max_manual_shots)
    operator=json.loads(args.operator_state.read_text(encoding='utf-8'));validate_operator(operator)
    prior=json.loads(args.prior_host_receipt.read_text(encoding='utf-8'));handoff=validate_prior(prior)
    if args.receipt!=Path(handoff):raise ValueError('receipt must match bound latest clean handoff')
    deps=deps or dependencies(args.source);base=deps.base
    output=args.output;output.mkdir(parents=True,exist_ok=False)
    (output/'commands').mkdir();(output/'acks').mkdir()
    state=dict(schema='r10.camera_calibration_live.v1',capture_kind='manual_camera_calibration_preview',
        passed=False,phase='initializing',failure=None,frames=[],discarded=[],preview_records=[],
        capture_batch_id=output.parent.name,start_utc=utc(),operator_state=operator,
        operator_state_sha256=sha(args.operator_state),prior_host_receipt_sha256=sha(args.prior_host_receipt),
        preview_frames_are_observations=False,fit_source_eligible=False,source_review_required=True,
        measurement_eligible=False,physical_accuracy_verified=False,exposure_lines=240,gain_code=16,
        motor_start_count=0,guard_arm_count=0,overlay_download_count=0,display_requested=True,
        duration_s=args.duration_s,max_manual_shots=args.max_manual_shots,transition_count=3,
        display_submissions=0,manual_command_mappings=[],unsaved_capture_records=[],stop_reason=None,
        cleanup_policy='protective revoke/cancel allowed; no ARM/START; retain child HDMI ownership if close unconfirmed')
    runtime=scope=access=display=None;trace=[];snapshot={};backup={}
    modified=False;bus_fault=False;display_attempted=False;hdmi_unconfirmed=False;processed=set()
    previous=None;last_id=None

    def save(path,value,exclusive=True):
        target=path.with_name(path.name+'.pending') if exclusive else path
        with target.open('x' if exclusive else 'w',encoding='utf-8') as stream:
            json.dump(base._json_safe(value),stream,indent=2,allow_nan=False)
            stream.flush();os.fsync(stream.fileno())
        if exclusive:
            # Hard-link publication is atomic and never replaces an existing
            # nonce/ACK/metadata file. Pending names are not command *.json.
            os.link(target,path);target.unlink()

    def status():
        value=dict(schema='r10.live_status.v1',phase=state['phase'],capture_batch_id=state['capture_batch_id'],
            previews=len(state['preview_records']),manual_shots=len(state['frames']),discarded=len(state['discarded']),
            last_request_id=last_id,display_submissions=state['display_submissions'],failure=state['failure'],
            last_snapshot=state['manual_command_mappings'][-1] if state['manual_command_mappings'] else None)
        tmp=output/'STATUS.json.tmp';save(tmp,value,False);tmp.replace(output/'STATUS.json')

    def trace_save():
        tmp=output/'SCCB_TRACE.json.tmp';save(tmp,trace,False);tmp.replace(output/'SCCB_TRACE.json')

    def check_motor():
        base._camera_idle(runtime.controller)
        guard,motor=base._safe_board_snapshot(runtime)
        if not base._same_motor_identity(state['motor_before'],motor):raise RuntimeError('motor command identity changed')
        return guard,motor

    def read_snapshot():
        nonlocal bus_fault
        try:return {reg:int(access.read(reg)) for reg in REGISTERS}
        except BaseException:
            bus_fault=True;raise

    def commands():
        pending=[]
        for path in sorted((output/'commands').glob('*.json')):
            if path.stem in processed:continue
            value=json.loads(path.read_text(encoding='utf-8'))
            if (not re.fullmatch('[0-9a-f]{32}',path.stem) or value.get('nonce')!=path.stem
                    or value.get('capture_batch_id')!=state['capture_batch_id']
                    or value.get('action') not in ('SNAP','STOP')):
                raise ValueError('invalid manual command nonce/action/batch binding')
            pending.append((path,value))
        return pending

    def preserve(frame,*,transition,command=None):
        record=dict(request_id=frame.request_id,epoch=frame.epoch,capture_batch_id=state['capture_batch_id'],
            saved_utc=utc(),raw_sha256=hashlib.sha256(frame.raw).hexdigest(),
            profile_sha256=hashlib.sha256(frame.profile_payload).hexdigest(),motion=frame.motion_metadata,
            exposure_lines=240,gain_code=16,register_readback=state['manual_readback'],
            discarded_transition=transition,manual_snapshot=not transition,actual_effect_known=False,
            measurement_eligible=False,source_review_required=True)
        if command:
            path,value=command
            record.update(command_nonce=value['nonce'],command=value,command_sha256=sha(path),
                requested_after_preview_id=last_id)
        folder=output/f'frame_{frame.request_id:010d}';folder.mkdir()
        for name,payload in [('raw.gray8.bin',frame.raw),('profile.bin',frame.profile_payload)]:
            with (folder/name).open('xb') as stream:stream.write(payload);stream.flush();os.fsync(stream.fileno())
        save(folder/'frame.json',dict(record,diagnostics=dataclasses.asdict(frame.diagnostics)))
        (state['discarded'] if transition else state['frames']).append(record)
        if command:
            mapping=dict(nonce=value['nonce'],request_id=frame.request_id,epoch=frame.epoch,
                raw_sha256=record['raw_sha256'],profile_sha256=record['profile_sha256'],saved=True)
            state['manual_command_mappings'].append(mapping);processed.add(value['nonce'])
            save(output/'acks'/f"{value['nonce']}.json",mapping)

    try:
        status();base._validate_policy_manifest(args.manifest)
        runtime=base._default_runtime_factory(args.manifest,load=False,power_off_proof=None,
            receipt=args.receipt,output=output/'runtime',quality_roi=(0,0,752,480))
        scope=deps.scope(runtime);scope.__enter__()
        state['guard_before'],state['motor_before']=base._safe_board_snapshot(runtime)
        last_id=int(runtime.transport.mmio.request_id);epoch=int(runtime.transport.mmio.epoch)
        state['initial_capture_identity']=dict(request_id=last_id,epoch=epoch)
        access=deps.access(deps.gated_mmio(runtime.transport._diag_mmio,runtime.session._gate),trace,trace_save)
        snapshot=read_snapshot();state['initial_registers']={hex(k):v for k,v in snapshot.items()}
        save(output/'SENSOR_BACKUP.json',state['initial_registers'])
        if snapshot[0]!=0x1324 or (snapshot[4],snapshot[3])!=(752,480):raise RuntimeError('native sensor identity/window mismatch')
        backup={reg:snapshot[reg] for reg in base.RESTORE_ORDER}
        try:
            for reg,value in deps.manual_plan(snapshot,coarse_rows=240,gain_code=16):
                check_motor();modified=True;access.write_verified(reg,value)
            readback={hex(reg):int(access.read(reg)) for reg in base.RESTORE_ORDER}
            if readback!={'0x35':16,'0xb':240,'0xaf':0}:raise RuntimeError('manual exposure/gain readback mismatch')
            state['manual_readback']=readback
        except BaseException:
            bus_fault=True;raise
        display_attempted=True;display=deps.display_factory(runtime,args.manifest,output);display.configure()
        state['phase']='live_settling';status()
        started=clock();deadline=started+args.duration_s
        while len(state['frames'])<args.max_manual_shots:
            check_motor();pending=commands()
            stops=[entry for entry in pending if entry[1]['action']=='STOP']
            if stops:
                path,value=stops[0];processed.add(value['nonce'])
                save(output/'acks'/f"{value['nonce']}.json",dict(stopped=True,nonce=value['nonce'],last_request_id=last_id))
                state['stop_reason']='operator_stop';break
            if deadline-clock()<2.:
                state['stop_reason']='duration_limit';break
            command=next((entry for entry in pending if entry[1]['action']=='SNAP'),None) if len(state['discarded'])==3 else None
            frame=deps.capture(runtime.controller,timeout_s=2.,require_motion_metadata=True)
            if type(frame.request_id) is not int or frame.request_id!=((last_id+1)&0xffffffff) or frame.epoch!=epoch:
                raise RuntimeError('live capture request ID/epoch did not advance exactly')
            previous=base._check_motion_metadata(frame.motion_metadata,frame.request_id,frame.epoch,state['motor_before'],previous)
            check_motor()
            if not isinstance(frame.raw,bytes) or len(frame.raw)!=752*480 or not isinstance(frame.profile_payload,bytes):
                raise RuntimeError('native immutable raw/profile payload required')
            # STOP arriving while DMA was in flight wins over an earlier SNAP.
            # This captured frame remains an explicitly unsaved event.
            stops=[entry for entry in commands() if entry[1]['action']=='STOP']
            if stops:
                _,value=stops[0];processed.add(value['nonce']);last_id=frame.request_id
                state['unsaved_capture_records'].append(dict(request_id=frame.request_id,epoch=frame.epoch,
                    reason='STOP received during capture; not persisted or displayed',capture_utc=utc()))
                save(output/'acks'/f"{value['nonce']}.json",dict(stopped=True,nonce=value['nonce'],last_request_id=last_id))
                state['stop_reason']='operator_stop';break
            transition=len(state['discarded'])<3
            if transition or command:preserve(frame,transition=transition,command=command)
            canvas=deps.render(frame.raw,frame.request_id,len(state['preview_records'])+1,len(state['frames']),transition=transition)
            receipt=display.show_canvas(canvas)
            if receipt.get('hdmi_acknowledged') is not True:raise RuntimeError('HDMI current-frame acknowledgement missing')
            state['display_submissions']+=1
            state['preview_records'].append(dict(request_id=frame.request_id,epoch=frame.epoch,preview_utc=utc(),
                persisted=bool(transition or command),transition=transition,hdmi_acknowledged=True))
            last_id=frame.request_id
            if len(state['discarded'])==3 and not (output/'READY.json').exists():
                state['phase']='live';save(output/'READY.json',dict(schema='r10.live_ready.v1',
                    capture_batch_id=state['capture_batch_id'],live=True,last_request_id=last_id,
                    hdmi_acknowledged=True,operator_state_sha256=state['operator_state_sha256'],
                    manual_shots=0,preview_frames_are_observations=False,ready_utc=utc()))
                import cv2
                ok,png=cv2.imencode('.png',canvas)
                if not ok:raise RuntimeError('live evidence PNG encoding failed')
                with (output/'HDMI_READY.png').open('xb') as stream:stream.write(png.tobytes())
            status()
        if state['stop_reason'] is None:state['stop_reason']='manual_shot_limit'
        state['live_completed']=bool(len(state['discarded'])==3 and state['display_submissions']>=3)
    except BaseException as exc:state['failure']=type(exc).__name__+': '+str(exc)
    finally:
        state['phase']='closing'
        if runtime is not None:
            try:
                state['guard_after'],state['motor_after']=check_motor()
                if access is not None and modified:
                    if not bus_fault:
                        try:
                            final=read_snapshot();state['final_registers_before_restore']={hex(k):v for k,v in final.items()}
                            if any(final[k]!=snapshot[k] for k in FIXED_REGISTERS) or (final[0x35],final[0x0B],final[0xAF])!=(16,240,0):
                                raise RuntimeError('camera identity/window/mode/manual settings changed before restore')
                        except BaseException as exc:bus_fault=True;state['final_readback_error']=type(exc).__name__+': '+str(exc)
                    if bus_fault:
                        state['restore']=dict(verified=False,status='sccb_fault_no_blind_rollback',external_recovery_required=True)
                    else:state['restore']=base._restore_sensor(access,backup,backup_complete=True)
            except BaseException as exc:state['final_identity_error']=type(exc).__name__+': '+str(exc)
        if display_attempted:
            try:
                if display is None:raise RuntimeError('HDMI child startup unconfirmed')
                closed=display.close()
                if closed.get('closed_confirmed') is not True or closed.get('owner_exited') is not True:
                    raise RuntimeError('HDMI child close/exit acknowledgement missing')
                state['display_close']=closed
            except BaseException as exc:
                hdmi_unconfirmed=True;state['display_close_error']=type(exc).__name__+': '+str(exc)
        deps.close(state,scope=scope,display=None,runtime=runtime,profiler=None,output=output,worker_unconfirmed=hdmi_unconfirmed)
        if hdmi_unconfirmed and runtime is not None:
            # Never LOCK_UN an open file description still inherited by the
            # child retaining HDMI CMA. Close only this parent's duplicate.
            lock=getattr(runtime,'hardware_lock',None)
            if lock is not None and getattr(lock,'_handle',None) is not None:
                lock._handle.close();lock._handle=None
            state['cleanup']=dict(closed=False,capture_handoff=None,hdmi_owner_unconfirmed=True)
        state['sccb_trace']=trace
        state['passed']=bool((state.get('live_completed') or state['stop_reason']=='operator_stop') and not state['failure'] and not hdmi_unconfirmed
            and not state.get('final_readback_error') and not state.get('final_identity_error')
            and state.get('restore',{}).get('verified') is True and state.get('cleanup',{}).get('closed') is True
            and state['cleanup'].get('capture_handoff') and not state.get('generation_close_error') and not state.get('cleanup_error'))
        state['phase']='complete' if state['passed'] else 'failed';status()
        save(output/'RESULT.json',state);save(output/'DONE.json',dict(passed=state['passed'],exit_code=0 if state['passed'] else 1))
    return state


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','manifest','receipt','prior-host-receipt','operator-state','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--duration-s',type=float,default=900.)
    parser.add_argument('--max-manual-shots',type=int,default=30)
    args=parser.parse_args(argv);result=run_live(args)
    print(json.dumps(dict(passed=result['passed'],failure=result['failure'],cleanup=result.get('cleanup'))))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
