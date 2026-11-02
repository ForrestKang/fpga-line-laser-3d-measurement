"""Camera-only B acceptance: 1000 raw, 1000 paired, 32 bounded recoveries.

One explicit load with power-off proof, or attach; no motor START. Reuses B static
runner and the frozen P2 fault-injection implementation under one owner.
"""
from pathlib import Path
import argparse,json,sys,time,traceback
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src/python'))
from structured_light.motion.pynq_runtime import create_stage_b_runtime

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',type=Path,required=True)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--receipt',type=Path)
    source.add_argument('--power-off-proof',type=Path)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    r=None;began=time.monotonic()
    result={'motion_commands':0,'overlay_downloads':0,'hidden_retry':False,'passed':False}
    def save():
        from p2_closeout_board import _write_json
        _write_json(a.output/'acceptance.json',result)
    try:
        from pynq import Overlay
        original_download=Overlay.download
        def bounded_download(*args,**kwargs):
            if a.power_off_proof is None or result['overlay_downloads']:
                raise RuntimeError('No additional Overlay download is allowed during acceptance')
            result['overlay_downloads']+=1
            return original_download(*args,**kwargs)
        Overlay.download=bounded_download
        admission=(dict(load=True,power_off_confirmation=a.power_off_proof) if a.power_off_proof is not None else dict(attach=True,loaded_overlay_receipt=a.receipt))
        r=create_stage_b_runtime(a.manifest,stage='static',output=a.output/'static',no_display=True,**admission)
        result['guard_before']=r.guard.status()
        assert not result['guard_before']['active'] and not result['guard_before']['step_pin'] and result['guard_before']['enable_pin']
        from gray_capture_board import verify_camera_control
        from structured_light.p2.camera_evidence import run_monitor,assess_signals
        control=verify_camera_control(r.transport._diag_mmio,a.output)
        monitor=run_monitor(r.transport._diag_mmio)
        signal=assess_signals(monitor,width=752,height=480,pclk_hz=min(control['pclk_hz']),frames=16)
        assert signal['camera_signal_verified'],signal
        result['camera_gate']=signal;save()
        config={'width':752,'height':480,'overlay_mode':'structured_light_profile_hdmi','closeout_threshold':30,'closeout_roi':[0,120,752,300],'min_width':1,'max_width':32,'min_sum':30,'min_peak':15,'normal_timeout_cycles':100000000,'watchdog_timeout_cycles':500000}
        import p2_closeout_board as recovery
        params=recovery.effective_parameters(config)
        recovery._configure_controller(r.controller,params)
        (a.output/'parameters.json').write_text(json.dumps(params,indent=2))
        for mode in ('raw','profile'):
            result['phase']=mode;save();print('START '+mode,flush=True)
            result[mode]=r.static(count=1000,mode=mode)
            result['guard_'+mode]=r.guard.status()
            assert not result['guard_'+mode]['active'] and not result['guard_'+mode]['step_pin'] and result['guard_'+mode]['enable_pin']
            save();print('PASS '+mode,flush=True)
        result['phase']='recovery';save();print('START recovery',flush=True)
        # The legacy entry would load P2. Replace only its preparation hook
        # with the already validated, exclusively owned B setup. The actual
        # STOP/abort/watchdog/host-timeout logic is reused without alteration.
        original=recovery._prepare_board
        def attached_setup(config,path,output,*,mode):
            assert mode=='structured_light_profile_hdmi'
            return {'transport':r.transport,'signal':signal,'overlay':r.overlay}
        recovery._prepare_board=attached_setup
        try:
            result['recovery']=recovery.run_recovery_stage(config,a.manifest,a.output/'recovery',32,include_host_timeout=True)
        finally:recovery._prepare_board=original
        result['guard_after']=r.guard.status()
        assert not result['guard_after']['active'] and not result['guard_after']['step_pin'] and result['guard_after']['enable_pin']
        result['passed']=True;result['phase']='complete';print('PASS recovery',flush=True)
    except BaseException as e:
        result.update(passed=False,error=type(e).__name__+': '+str(e),traceback=traceback.format_exc())
        print(result['traceback'],flush=True)
    finally:
        if r is not None:
            try:result['cleanup']=r.close()
            except BaseException as e:result.update(passed=False,cleanup_error=str(e))
        result['elapsed_s']=time.monotonic()-began;save()
        print(json.dumps(result),flush=True)
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
