"""Continuous HDMI alignment on the active B Overlay; no load or motor START."""
from pathlib import Path
import argparse
import json
import math
import os
import signal
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src/python'))
from structured_light.p2.preview_transport import PreviewPynqTransport
from structured_light.motion.capture_handoff import HandoffPynqTransport


class AlignmentTransport(PreviewPynqTransport,HandoffPynqTransport):
    """Reuse the fast bounded buffers while retaining strict attach IDs."""

    def poll(self):
        # A rejected frame may intentionally never generate DMA TLAST. Do
        # not turn the original camera error into an unexplained host timeout.
        from structured_light.p2.contract import DIAG_OFFSETS,REG_ERROR_FLAGS
        from structured_light.p2.errors import ProtocolError
        if self._accepted_request_id is not None:
            legacy=int(self.mmio.read(REG_ERROR_FLAGS))
            error=int(self._diag_mmio.read(DIAG_OFFSETS['error']))
            if legacy or error:
                snapshot={'before_reset':True,'request_id':self._accepted_request_id,
                    'legacy_error':legacy,'camera_eof_errors':legacy&0xffff,'diag_error':error,
                    'monotonic_s':time.monotonic(),'monitor_is_context_not_request_bound':True}
                for name,offset in DIAG_OFFSETS.items():
                    if name!='row_table':snapshot[name]=int(self._diag_mmio.read(offset))
                snapshot['monitor']={hex(offset):int(self._diag_mmio.read(offset)) for offset in range(0x84,0xdc,4)}
                snapshot['dma']={}
                for name,dma in [('raw',self.raw_dma),('profile',self.profile_dma)]:
                    try:snapshot['dma'][name]={k:int(dma._rd(getattr(dma,k+'_offset'))) for k in ('control','status','length')}
                    except Exception as exc:snapshot['dma'][name]={'read_error':str(exc)}
                self.failure_snapshot=snapshot
                raise ProtocolError(f'PL rejected preview frame: DIAG_ERROR=0x{error:08X}, ERROR_FLAGS=0x{legacy:08X}')
        return super().poll()


def require_disabled(state):
    if (state['active'] or state['step_pin'] or not state['enable_pin']
            or state.get('fault',False) or state.get('motor_busy',False)):
        raise RuntimeError('alignment requires motor disabled, STEP low, EN high')


def publish(path,value):
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,indent=2,default=str)+'\n',encoding='utf-8')
    temp.replace(path)


def prepare_display_artifacts(artifacts,output):
    """Name-only compatibility for the old display owner, never another bit.

    The owner still checks active PL, both artifact hashes and HWH addresses.
    Copies live only under this preview's new output; signed sources stay put.
    """
    import hashlib
    folder=output/'display_artifacts';folder.mkdir(parents=True)
    copies={}
    for kind in ('bit','hwh','manifest'):
        payload=Path(artifacts[kind]).read_bytes()
        digest=hashlib.sha256(payload).hexdigest()
        if digest!=artifacts['files'][kind]['sha256']:
            raise RuntimeError('display compatibility source hash mismatch: '+kind)
        name='manifest.json' if kind=='manifest' else 'structured_light_profile_hdmi.'+kind
        target=folder/name
        with target.open('xb') as stream:stream.write(payload)
        if hashlib.sha256(target.read_bytes()).hexdigest()!=digest:
            raise RuntimeError('display compatibility copy hash mismatch')
        copies[kind]={'source':artifacts[kind],'copy':str(target),'sha256':digest}
    publish(folder/'compatibility_receipt.json',{'name_alias_only':True,'overlay_loaded':False,'files':copies})
    return folder/'manifest.json'


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--receipt',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--duration-s',type=float,default=1800)
    a=p.parse_args(argv)
    if not math.isfinite(a.duration_s) or not 10<=a.duration_s<=1800:p.error('duration must be 10..1800 seconds')
    if a.output.exists():p.error('output already exists')
    a.output.mkdir(parents=True)
    state={'phase':'initializing','pid':os.getpid(),'frames':0,'preview_only':True,
           'reference_comparison':False,'overlay_loaded':False,'motor_started':False,
           'duration_s':a.duration_s,'raw_transformed':False}
    publish(a.output/'status.json',state)
    stopping=False
    def stop_handler(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGINT,stop_handler);signal.signal(signal.SIGTERM,stop_handler)
    from structured_light.motion.lifecycle import HardwareProcessLock
    from structured_light.motion.pynq_runtime import (
        verify_stage_b_artifacts,verify_loaded_overlay_receipt,
        _current_pl_identity,_overlay_mmio,MMIO32,MOTION_ADDRESSES,
        P2_OVERLAY_ADDRESSES,OVERLAY_MODE)
    from structured_light.motion.guard import GuardController
    from structured_light.p2.contract import load_board_contract
    from structured_light.p2.controller import P2Controller
    from structured_light.p2.live_preview import LivePreview
    from structured_light.p2.hdmi_output import create_output
    from pynq import Overlay
    lock=HardwareProcessLock(label='stage_b_alignment_preview')
    viewer=controller=transport=display=None
    result=0
    try:
        lock.acquire()
        artifacts=verify_stage_b_artifacts(a.manifest)
        overlay=Overlay(artifacts['bit'],download=False)
        identity=verify_loaded_overlay_receipt(a.receipt,artifacts=artifacts,overlay=overlay)
        contract=load_board_contract(artifacts['contract'])
        guard=GuardController(MMIO32(_overlay_mmio(overlay,'bsg1_guard',MOTION_ADDRESSES['bsg1_guard'],contract.window),
                              base_address=MOTION_ADDRESSES['bsg1_guard'],name='BSG1'),
                              carrier_mode='direct_wire',manifest=json.loads(a.manifest.read_text()))
        guard.validate_identity();state['guard_before']=guard.status();require_disabled(state['guard_before'])
        transport=AlignmentTransport(artifacts['bit'],artifacts['hwh'],artifacts['manifest'],
            frame_width=752,frame_height=480,expected_addresses=dict(P2_OVERLAY_ADDRESSES),
            overlay=overlay,contract=contract,overlay_mode=OVERLAY_MODE,
            contract_path=artifacts['contract'],strict_manifest=True)
        receipt=json.loads(a.receipt.read_text())
        transport.restore_capture_checkpoint(receipt.get('capture_checkpoint'))
        controller=P2Controller(transport,default_timeout_s=2.)
        display_manifest=prepare_display_artifacts(artifacts,a.output)
        display=create_output(overlay,display_manifest,background_owner=True,
                              owner_log=a.output/'hdmi_owner.log',preview_only=True)
        display.configure()
        viewer=LivePreview(controller,display)
        state.update(boot_id=identity['boot_id'],artifacts=artifacts['files'])
        deadline=time.monotonic()+a.duration_s;last_status=last_image=0.
        while not stopping and not (a.output/'stop.request').exists() and time.monotonic()<deadline:
            metrics=viewer.step();now=time.monotonic()
            state.update(metrics,phase='running',elapsed_s=now-viewer.started)
            if now-last_status>=1:
                require_disabled(guard.status());publish(a.output/'status.json',state);last_status=now
            if now-last_image>=10:
                from PIL import Image
                (a.output/'latest_gray.bin').write_bytes(viewer.latest.pixels)
                temp=a.output/'latest_gray.tmp'
                Image.frombytes('L',(752,480),viewer.latest.pixels).save(temp,format='PNG',compress_level=0)
                temp.replace(a.output/'latest_gray.png');last_image=now
        state.update(phase='stopped',stop_reason='requested' if stopping or (a.output/'stop.request').exists() else 'duration_limit')
    except BaseException as exc:
        import traceback
        state.update(phase='failed',error=type(exc).__name__+': '+str(exc),traceback=traceback.format_exc());result=1
        if transport is not None:
            state['failure_before_reset']=getattr(transport,'failure_snapshot',None)
            state['transport_recovery']=getattr(transport,'last_recovery',{})
    finally:
        try:
            if viewer is not None:
                try:viewer.close()
                except BaseException:
                    # Camera and VDMA are separate masters. Attempt the camera
                    # barrier even if the display owner retains its buffers.
                    if controller is not None:controller.close()
                    raise
            else:
                if display is not None:display.close()
                if controller is not None:controller.close()
                elif transport is not None:transport.close()
            state['cleanup_confirmed']=True
            if transport is not None and viewer is not None and viewer.count:
                try:checkpoint=transport.export_capture_checkpoint()
                except Exception as exc:
                    # A cancelled generation is deliberately not resumable by
                    # another process. This is different from unfreed buffers.
                    state.update(handoff_ready=False,handoff_reason=str(exc))
                else:
                    ident=_current_pl_identity(overlay,expected_bit=Path(artifacts['bit']))
                    payload={'loaded':True,'overlay_mode':OVERLAY_MODE,
                        'active_bitfile':ident['active_bitfile'],'boot_identity':{'boot_id':ident['boot_id']},
                        **{k+'_sha256':artifacts['files'][k]['sha256'] for k in ('bit','hwh','manifest')},
                        'capture_checkpoint':checkpoint,'handoff_scope':'clean capture generation only; not motion authorization'}
                    handoff=a.output/'capture_handoff.json'
                    with handoff.open('x',encoding='utf-8') as stream:
                        stream.write(json.dumps(payload,indent=2)+'\n');stream.flush();os.fsync(stream.fileno())
                    state.update(capture_handoff=str(handoff),handoff_ready=True)
                state['guard_after']=guard.status();require_disabled(state['guard_after'])
        except BaseException as exc:
            state.update(cleanup_confirmed=False,cleanup_error=type(exc).__name__+': '+str(exc));result=1
        publish(a.output/'status.json',state);publish(a.output/'summary.json',state)
        if lock.acquired:lock.release()
    return result


if __name__=='__main__':raise SystemExit(main())
