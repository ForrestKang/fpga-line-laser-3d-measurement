"""Open one evidence-bound PYNQ session for notebook or HDMI acceptance."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src/python'))


def open_profile_dashboard(config_path, output, *, hdmi=False, background_owner=False, preview_only=False):
    """Explicitly load a validated Overlay, verify the camera, then return an owner.

    This function performs real hardware operations. It never substitutes Mock
    for failed hardware and never reloads the Overlay during individual captures.
    """
    import yaml
    from verify_p2_board import (_check_gray_target_identity, _check_gray_gate_a,
                                validate_overlay_contract, release_camera)
    from gray_capture_board import verify_camera_control
    from structured_light.p2.camera_evidence import assess_signals, run_monitor
    from structured_light.p2.capture_dashboard import CaptureDashboard
    from structured_light.p2.controller import P2Controller
    from structured_light.p2.pynq_transport import PynqTransport,validate_runtime_contract
    from structured_light.p2.session import SessionRecorder
    from structured_light.p2.hdmi_output import create_output
    from pynq import MMIO, Overlay
    config_path=Path(config_path).resolve(strict=True)
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=True)
    record={'started_board_utc':datetime.now(timezone.utc).isoformat(),
            'status':'initializing','hardware':'real_pynq_z2','hdmi_requested':hdmi}
    controller=transport=recorder=display=None
    def journal():
        (output/'session_initialization.json').write_text(json.dumps(record,indent=2,default=str)+'\n')
    journal()
    try:
        config=yaml.safe_load(config_path.read_text(encoding='utf-8'))
        if not isinstance(config,dict): raise ValueError('config must be a mapping')
        record['identity']=_check_gray_target_identity(config)
        record['gate_a']=_check_gray_gate_a(config,config_path)
        mode=config.get('overlay_mode')
        if mode not in {'structured_light_profile','structured_light_profile_hdmi'}:
            raise ValueError('paired Profile Overlay required')
        if hdmi and mode!='structured_light_profile_hdmi':
            raise ValueError('HDMI requires the combined Profile/HDMI Overlay')
        validation=validate_overlay_contract(config,mode)
        record['artifacts']=validation; journal()
        overlay=Overlay(validation['bitfile'],download=True)
        diag=MMIO(0x43c10000,0x10000); control=MMIO(0x43c00000,0x10000)
        record['runtime']=validate_runtime_contract(control,diag,overlay_mode=mode)
        release_camera(diag)
        camera=verify_camera_control(diag,output)
        width,height=int(config['width']),int(config['height'])
        signal=assess_signals(run_monitor(diag,timeout_s=6),width=width,height=height,
                              pclk_hz=min(camera['pclk_hz']),frames=16)
        record['signals']=signal; journal()
        if not signal['camera_signal_verified']: raise RuntimeError('16-frame camera signal gate failed')
        transport_type = PynqTransport
        if preview_only:
            from structured_light.p2.preview_transport import PreviewPynqTransport
            transport_type = PreviewPynqTransport
        record['preview_only'] = preview_only
        transport=transport_type(validation['bitfile'],validation['hwh'],validation['manifest'],
                                frame_width=width,frame_height=height,overlay_mode=mode,overlay=overlay)
        recorder=SessionRecorder(output/'sessions',metadata={'source':'real_pynq_z2','same_frame':True,'motion_enabled':False})
        controller=P2Controller(transport,recorder=recorder,session_id=recorder.session_id,
                                default_timeout_s=max(2.,3*signal['frame_period_s']+1))
        parameters={key:int(config.get(key,default)) for key,default in
                    [('threshold',30),('min_width',1),('max_width',32),('min_sum',30),('min_peak',15)]}
        parameters['roi']=tuple(config.get('roi',(0,0,width,height)))
        controller.set_threshold(parameters['threshold']); controller.configure_roi(*parameters['roi'])
        controller.configure_centroid(**{key:parameters[key] for key in ('min_width','max_width','min_sum','min_peak')})
        if hdmi:
            display=create_output(overlay,validation['manifest'],background_owner=background_owner,
                                  owner_log=output/'hdmi_owner.json',preview_only=preview_only)
            display.configure()
        dashboard=CaptureDashboard(controller,output/'captures',display=display,parameters=parameters)
        dashboard.board_context=record
        record.update(status='camera_verified_session_open',config=config,parameters=parameters)
        journal()
        return dashboard
    except Exception as error:
        record.update(status='failed',error_type=type(error).__name__,error=str(error))
        # Closing may fail if hardware has not halted; retain that fact and the
        # display owner rather than killing a process that still owns live CMA.
        for name,owner in [('display',display),('controller',controller),('transport',transport if controller is None else None),('recorder',recorder)]:
            if owner is not None:
                try: owner.close()
                except Exception as cleanup: record[name+'_cleanup_error']=str(cleanup)
        journal()
        raise
