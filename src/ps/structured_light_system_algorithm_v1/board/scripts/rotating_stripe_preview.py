"""Finite rotating raw/SLP2 pixel preview on the already loaded B Overlay.

This preview does not claim per-frame Python reference checks, optical
correctness, calibrated angles or XYZ. Measurement APIs remain unchanged.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import os
import signal
import sys
import threading
import time


def validate_preview_pair(raw, payload, handle, profile, diagnostics):
    if len(raw) != 752*480 or len(payload) != 5792:
        raise ValueError('incomplete preview raw/SLP2 length')
    if (profile.format_version != 2 or len(profile.points) != 480
            or diagnostics.row_count != 480
            or (diagnostics.width, diagnostics.height) != (752,480)):
        raise ValueError('preview geometry/profile version mismatch')
    if (profile.profile_id != handle.request_id
            or diagnostics.request_id != handle.request_id
            or diagnostics.epoch != handle.epoch):
        raise ValueError('preview raw/Profile identity mismatch')
    return {'request_id':handle.request_id,'epoch':handle.epoch,
            'reference_compared':False,'row_diagnostics_read':False,
            'angle_calibrated':False,'xyz_available':False}


def fill_native_store(store, raw, profile):
    """Fill ONLY an inactive HDMI store; source bytes never change."""
    import numpy as np
    if len(raw) != 752*480 or store.shape != (720,1280,4):
        raise ValueError('native preview geometry mismatch')
    gray=np.frombuffer(raw,np.uint8).reshape(480,752)
    words=np.asarray(store).view(np.uint32).reshape(720,1280)
    np.multiply(gray,0x010101,out=words[120:600,264:1016],dtype=np.uint32)
    count=0
    for point in profile.points:
        if not point.valid:
            continue
        x=(point.xc_fixed+2048)//4096;y=point.row
        if not 0<=x<752 or not 0<=y<480:
            raise ValueError('valid Profile point lies outside gray frame')
        words[120+y,264+max(0,x-1):264+min(752,x+2)]=0x0000ff  # G,B,R,X lanes
        count+=1
    return count


def validate_motion_receipt(receipt):
    motion=receipt.get('motion',{})
    if (receipt.get('status')!='complete' or receipt.get('passed') is not True
            or motion.get('start_count')!=1 or motion.get('requested_pulses')!=3200
            or motion.get('emitted_pulses')!=3200):
        raise ValueError('finite rotating-preview task failed or incomplete: '+str(receipt.get('error','invalid motion receipt')))
    return motion


def build_display(base):
    from structured_light.p2.preview_output import PreviewHDMIOutput
    import numpy as np
    from PIL import Image,ImageDraw

    class StripeDisplay(PreviewHDMIOutput):
        def show_preview(self,raw,width,height,*,profile_b64,request_id,epoch,fps,stage):
            import base64
            from structured_light.profile import decode_profile
            if (width,height)!=(752,480):raise ValueError('invalid stripe display geometry')
            payload=base64.b64decode(profile_b64,validate=True)
            profile=decode_profile(payload,frame_width=752,frame_height=480)
            if profile.format_version!=2 or profile.profile_id!=request_id or len(profile.points)!=480:
                raise ValueError('display Profile does not belong to gray request')
            return self.show_stripe(raw,profile,request_id,epoch,fps,stage)

        def show_stripe(self,raw,profile,request_id,epoch,fps,stage):
            if self.quarantined:
                raise RuntimeError('preview buffers quarantined')
            if self.geometry is None:
                self._initialize(752,480,0,False)
            self._status()
            active=(int(self.dma.read(0x28))>>16)&31
            desired=int(self.dma.read(0x28))&31
            if active!=self.current or desired!=self.current:
                raise RuntimeError('unexpected HDMI active-store ownership')
            target=(self.current+1)%3
            frame=self.buffers[target]
            count=fill_native_store(frame,raw,profile)
            header=Image.new('RGB',(1280,100),'#101822');draw=ImageDraw.Draw(header)
            draw.text((24,14),f'LIVE ROTATING STRIPE | REQ {request_id} | {fps:.2f} fps | {stage}',fill='white')
            draw.text((24,38),f'PL SLP2 | VALID ROWS {count}/480 | SAME FRAME | UNCALIBRATED PIXELS',fill='#60ff90')
            draw.text((24,62),'ONE FINITE TURN | NO XYZ / MILLIMETER RESULT | NO PER-FRAME PYTHON REFERENCE',fill='#ffd060')
            rgb=np.asarray(header)
            frame[:100,:,0]=rgb[:,:,1];frame[:100,:,1]=rgb[:,:,2];frame[:100,:,2]=rgb[:,:,0];frame[:100,:,3]=0
            frame[:100].flush();frame[120:600].flush()
            self.dma.write(4,0x1000)
            self.dma.write(0x28,(int(self.dma.read(0x28))&~31)|target)
            self._wait_store(target);self.current=target;self.updates+=1
            return {'update_count':self.updates,'active_store':target,'valid_rows':count,
                    'request_id':request_id,'camera_epoch':epoch,'same_frame_overlay':True,
                    'reference_compared':False,'rendered':True,'vdma_status':self._status()}
    return StripeDisplay(base.clock,base.vtc,base.dma,base.allocator,timeout_s=base.timeout_s)


def remote_stripe_display(manifest,source_root,owner_log):
    """Use the proven owner protocol and quarantine; only its renderer changes.

    The owner entry script is redirected before any workers start. Existing
    parent IPC, deadlines, allocation ownership and quarantine remain intact.
    The frozen owner's source on disk is never modified.
    """
    from structured_light.p2 import hdmi_owner
    import base64
    class RemoteStripe(hdmi_owner.RemoteHDMIOutput):
        def show_stripe(self,raw,profile,request_id,epoch,fps,stage,payload=None):
            if payload is None:raise ValueError('actual SLP2 bytes required')
            self.submit_preview(raw,752,480,profile_b64=base64.b64encode(payload).decode('ascii'),request_id=request_id,epoch=epoch,fps=fps,stage=stage)
            return self.finish_preview()
    original=hdmi_owner.__file__;prior=os.environ.get('SL_ROTATING_SOURCE_ROOT')
    try:
        hdmi_owner.__file__=str(Path(__file__).resolve())
        os.environ['SL_ROTATING_SOURCE_ROOT']=str(source_root)
        return RemoteStripe(manifest,owner_log=owner_log,timeout_s=2.,preview_only=True)
    finally:
        hdmi_owner.__file__=original
        if prior is None:os.environ.pop('SL_ROTATING_SOURCE_ROOT',None)
        else:os.environ['SL_ROTATING_SOURCE_ROOT']=prior


def serve_display(socket_path,manifest,journal):
    source=Path(os.environ['SL_ROTATING_SOURCE_ROOT']).resolve(strict=True)
    sys.path.insert(0,str(source/'src/python'))
    from structured_light.p2 import hdmi_output,hdmi_owner
    factory=hdmi_output.create_output
    def stripe_factory(*args,**kwargs):return build_display(factory(*args,**kwargs))
    hdmi_output.create_output=stripe_factory
    hdmi_owner.serve(socket_path,manifest,journal)


def capture_preview_pair(controller):
    from structured_light.profile import decode_profile
    handle=controller._arm_and_start(raw=True,profile=True)
    try:
        value=controller._wait_for_transfer(2.)
        raw,payload=value.raw,value.profile
        if raw is None or payload is None or value.raw_length!=len(raw) or value.profile_length!=len(payload):
            raise ValueError('preview actual DMA byte count mismatch')
        profile=decode_profile(payload,frame_width=752,frame_height=480)
        meta=validate_preview_pair(raw,payload,handle,profile,value.diagnostics)
        return raw,payload,profile,meta
    finally:
        controller._finish_inflight()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('source-root','manifest','receipt','output','motion-confirmation','driver-proof','coordinator'):
        parser.add_argument('--'+flag,type=Path,required=True)
    parser.add_argument('--coordinator-sha256',required=True)
    args=parser.parse_args(argv)
    if args.output.exists():raise RuntimeError('refusing to overwrite preview evidence')
    args.output.mkdir(parents=True)
    sys.path.insert(0,str(args.source_root/'src/python'));sys.path.insert(0,str(args.source_root/'board/scripts'))
    from b_alignment_preview import AlignmentTransport,prepare_display_artifacts
    from structured_light.p2.pynq_transport import PynqTransport
    from structured_light.motion.pynq_runtime import (verify_stage_b_artifacts,verify_loaded_overlay_receipt,
        _current_pl_identity,_overlay_mmio,MMIO32,MOTION_ADDRESSES,P2_OVERLAY_ADDRESSES,OVERLAY_MODE,
        _motion_confirmation,_driver_readonly_proof,prepare_camera_control)
    from structured_light.motion.guard import GuardController
    from structured_light.motion.motor_control import MotorController
    from structured_light.motion.orchestrator import MotionSession
    from structured_light.motion.types import MotionConfig
    from structured_light.motion.lifecycle import HardwareProcessLock
    from structured_light.p2.contract import load_board_contract
    from structured_light.p2.controller import P2Controller
    from pynq import Overlay
    if hashlib.sha256(args.coordinator.read_bytes()).hexdigest()!=args.coordinator_sha256:
        raise ValueError('coordinator source hash mismatch')
    spec=importlib.util.spec_from_file_location('finite_rotating_coordinator',args.coordinator)
    coordinator=importlib.util.module_from_spec(spec);spec.loader.exec_module(coordinator)
    _motion_confirmation(args.motion_confirmation);_driver_readonly_proof(args.driver_proof)
    class FastStripeTransport(AlignmentTransport):
        def stream_requirements(self,*,raw,profile):
            return PynqTransport.stream_requirements(self,raw=raw,profile=profile)
    lock=HardwareProcessLock(label='finite_rotating_stripe_preview')
    state={'state':'initializing','passed':False,'motion_start_count':0,'frames':0,'new_overlay_downloads':0,
           'reference_compared':False,'xyz_available':False,'angle_calibrated':False}
    session=controller=transport=display=None;stop=threading.Event()
    def publish():
        tmp=args.output/'status.tmp';tmp.write_text(json.dumps(state,indent=2,default=str));tmp.replace(args.output/'status.json')
    def stop_handler(*_):stop.set()
    signal.signal(signal.SIGINT,stop_handler);signal.signal(signal.SIGTERM,stop_handler)
    publish();started=time.monotonic();frame_records=[];stage='PREVIEW - MOTOR DISABLED'
    try:
        lock.acquire();artifacts=verify_stage_b_artifacts(args.manifest)
        overlay=Overlay(artifacts['bit'],download=False)
        identity=verify_loaded_overlay_receipt(args.receipt,artifacts=artifacts,overlay=overlay)
        contract=load_board_contract(artifacts['contract'])
        guard=GuardController(MMIO32(_overlay_mmio(overlay,'bsg1_guard',MOTION_ADDRESSES['bsg1_guard'],contract.window),base_address=MOTION_ADDRESSES['bsg1_guard'],name='BSG1'),carrier_mode='direct_wire',manifest=json.loads(args.manifest.read_text()))
        motor=MotorController(MMIO32(_overlay_mmio(overlay,'motor_controller',MOTION_ADDRESSES['motor_controller'],contract.window),base_address=MOTION_ADDRESSES['motor_controller'],name='MFP1'))
        g=guard.validate_identity();m=motor.validate_identity()
        if g['active'] or g['fault'] or g['error'] or g['step_pin'] or not g['enable_pin'] or m['busy'] or m['fault'] or m['error'] or not m['enable_n']:
            raise RuntimeError('motor/guard is not clean idle before preview')
        transport=FastStripeTransport(artifacts['bit'],artifacts['hwh'],artifacts['manifest'],frame_width=752,frame_height=480,expected_addresses=dict(P2_OVERLAY_ADDRESSES),overlay=overlay,contract=contract,overlay_mode=OVERLAY_MODE,contract_path=artifacts['contract'],strict_manifest=True)
        transport.restore_capture_checkpoint(json.loads(args.receipt.read_text()).get('capture_checkpoint'))
        state['camera_ready']=prepare_camera_control(transport._diag_mmio,initialize=False)
        from capture_laser_contrast import load_frozen_sccb_access,read_sensor_registers,validate_sensor_settings
        access_class,_=load_frozen_sccb_access(Path(__file__).with_name('camera_exposure.py'),'3ac098ba8d248934d35fbe9d6f79b2f403af210007979be15057b1fe5e244bdb')
        sensor_trace=[]
        access=access_class(transport._diag_mmio,sensor_trace,lambda:None)
        sensor=read_sensor_registers(access);validate_sensor_settings(sensor)
        state['sensor_before']={hex(k):v for k,v in sensor.items()}
        state['pclk_hz']=int(transport._diag_mmio.read(0x0c))
        if not 23760000<=state['pclk_hz']<=24240000:raise RuntimeError('camera PCLK changed')
        controller=P2Controller(transport,default_timeout_s=2.)
        controller.set_threshold(60);controller.configure_roi(470,210,630,405)
        alias=prepare_display_artifacts(artifacts,args.output)
        display=remote_stripe_display(alias,args.source_root,args.output/'hdmi_owner.log');display.configure()
        config=MotionConfig(positions=1,pulses_per_position=3200,rpm=5.,host_poll_max_s=.010)
        session=MotionSession(guard,motor,controller,config=config,output=args.output/'motion',hardware_lock=lock,mode='pynq',settle_roi=(480,220,615,400),installation=args.output.name)
        def frame_step():
            index=len(frame_records)
            if index>=512:raise RuntimeError('finite preview frame cap reached')
            begin=time.monotonic();raw,payload,profile,meta=capture_preview_pair(controller)
            before_show=time.monotonic();fps=len(frame_records)/max(.001,before_show-started)
            shown=display.show_stripe(raw,profile,meta['request_id'],meta['epoch'],fps,stage,payload=payload)
            folder=args.output/'frames';folder.mkdir(exist_ok=True)
            name=f'frame_{index:04d}'
            (folder/(name+'.gray')).write_bytes(raw);(folder/(name+'.slp2')).write_bytes(payload)
            record={**meta,'index':index,'raw_sha256':hashlib.sha256(raw).hexdigest(),'profile_sha256':hashlib.sha256(payload).hexdigest(),'capture_started_s':begin,'capture_completed_s':before_show,'display_completed_s':time.monotonic(),'valid_rows':shown['valid_rows'],'render_receipt':shown,'stage':stage}
            frame_records.append(record)
            with (args.output/'frames.jsonl').open('a') as f:f.write(json.dumps(record)+'\n')
            state.update(frames=len(frame_records),last_request=meta['request_id'],last_frame=record)
            # Status is published by this sole frame worker, never by guard IO.
            publish();return record
        # Prewarm camera/display and prove live output before arming a motor.
        session._owner.acquire();session._gate.begin()
        try:frame_step();frame_step()
        finally:session._gate.revoke(timeout_s=2.);session._owner.release()
        state['state']='countdown';publish()
        for i in range(5,0,-1):
            if stop.is_set():raise RuntimeError('stopped before motion')
            print(f'FINITE ROTATING PREVIEW STARTS IN {i}',flush=True);time.sleep(1.)
        stage='ROTATING - ONE FINITE TURN';state['state']='rotating_preview';publish()
        state['motion_receipt']=coordinator.run_rotating_preview(session,frame_step,stop_event=stop)
        state['motion_start_count']=state['motion_receipt'].get('motion',{}).get('start_count',0)
        validate_motion_receipt(state['motion_receipt'])
        stage='TURN FINISHED - MOTOR DISABLED';state['state']='post_motion_preview';publish()
        # Five seconds of new live frames after the motor permit is revoked.
        session._owner.acquire();session._gate.begin()
        try:
            until=time.monotonic()+5
            while time.monotonic()<until and not stop.is_set():frame_step()
        finally:session._gate.revoke(timeout_s=2.);session._owner.release()
        state['passed']=True;state['state']='complete'
    except BaseException as exc:
        import traceback
        state.update(state='failed',error=str(exc),error_type=type(exc).__name__,traceback=traceback.format_exc())
        if transport is not None:state['failure_before_reset']=getattr(transport,'failure_snapshot',None)
    finally:
        errors=[]
        if session is not None:
            try:
                closed=session.close();state['session_close']=closed
                if closed.get('closed') is not True:errors.append('session cleanup unconfirmed')
            except BaseException as exc:errors.append('session.close: '+str(exc))
        if display is not None:
            try:display.close()
            except BaseException as exc:errors.append('display.close: '+str(exc))
        if session is None and controller is not None:
            try:controller.close()
            except BaseException as exc:errors.append('controller.close: '+str(exc))
        state['cleanup_errors']=errors;state['cleanup_confirmed']=not errors
        if transport is not None and not errors:
            try:
                checkpoint=transport.export_capture_checkpoint();ident=_current_pl_identity(overlay,expected_bit=Path(artifacts['bit']))
                handoff={'loaded':True,'overlay_mode':OVERLAY_MODE,'active_bitfile':ident['active_bitfile'],'boot_identity':{'boot_id':ident['boot_id']},**{k+'_sha256':artifacts['files'][k]['sha256'] for k in ('bit','hwh','manifest')},'capture_checkpoint':checkpoint,'handoff_scope':'clean capture generation only; not motion authorization'}
                with (args.output/'capture_handoff.json').open('x') as f:json.dump(handoff,f);f.flush();os.fsync(f.fileno())
                state['handoff_ready']=True
            except BaseException as exc:state.update(handoff_ready=False,handoff_error=str(exc))
        if errors:state.update(state='failed',passed=False)
        if errors:
            # Keep CMA buffers and the exclusive hardware lock alive when a
            # cleanup barrier is unconfirmed. Never exit and free live DMA.
            state['state']='quarantined';publish()
            print('Unconfirmed cleanup: retaining ownership; no automatic restart.',flush=True)
            while True:time.sleep(1.)
        if lock.acquired:lock.release()
        state['elapsed_s']=time.monotonic()-started;publish()
    return 0 if state['passed'] else 1


if __name__=='__main__':
    if len(sys.argv)==5 and sys.argv[1]=='--serve':serve_display(*sys.argv[2:])
    else:raise SystemExit(main())
