"""Single-owner real PYNQ camera/HDMI preview with a bounded lifetime."""
import argparse
from datetime import datetime,timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src/python'))


def write_json(path,value):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,indent=2,default=str)+'\n',encoding='utf-8')
    tmp.replace(path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--duration-s',type=float,default=1800)
    parser.add_argument('--rotation',type=int,choices=[0,90,180,270],default=0)
    parser.add_argument('--mirror',action='store_true')
    args=parser.parse_args()
    if not 10<=args.duration_s<=1800:parser.error('duration must be 10..1800 seconds')
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    stop=False
    def stop_handler(*_):
        nonlocal stop
        stop=True
    signal.signal(signal.SIGTERM,stop_handler);signal.signal(signal.SIGINT,stop_handler)
    lock=open('/tmp/sl_camera_preview.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise RuntimeError('a live preview already owns the camera')
    state={'pid':os.getpid(),'mode':'continuous_complete_frame_preview','phase':'initializing',
           'frames':0,'duration_s':args.duration_s,'rotation':args.rotation,'mirror':args.mirror,
           'raw_transformed':False,'reference_comparison':False,'hardware':'real_pynq_z2',
           'snapshot_interval_s':30.,
           'started_board_utc':datetime.now(timezone.utc).isoformat()}
    write_json(output/'status.json',state)
    dashboard=viewer=None
    result=0
    try:
        from profile_dashboard_board import open_profile_dashboard
        from structured_light.p2.live_preview import LivePreview
        dashboard=open_profile_dashboard(args.config,output,hdmi=True,background_owner=True,preview_only=True)
        viewer=LivePreview(dashboard.controller,dashboard.display,rotation=args.rotation,mirror=args.mirror)
        deadline=time.monotonic()+args.duration_s
        published=saved=0.
        state['camera_precheck']=dashboard.board_context
        while not stop and not (output/'stop.request').exists() and time.monotonic()<deadline:
            metrics=viewer.step()
            now=time.monotonic()
            state.update(metrics,phase='running',elapsed_s=now-viewer.started)
            if now-published>=1:
                write_json(output/'status.json',state);published=now
            if now-saved>=30:
                # Overwrite only these explicitly named current preview files;
                # no growing per-frame archive and no accepted batch changes.
                (output/'latest_gray.bin').write_bytes(viewer.latest.pixels)
                from PIL import Image
                tmp=output/'latest_gray.tmp'
                Image.frombytes('L',(viewer.latest.width,viewer.latest.height),viewer.latest.pixels).save(tmp,format='PNG',compress_level=0)
                tmp.replace(output/'latest_gray.png')
                saved=now
        state['phase']='stopped';state['stop_reason']='requested' if stop or (output/'stop.request').exists() else 'duration_limit'
    except Exception as error:
        state.update(phase='failed',error_type=type(error).__name__,error=str(error));result=1
    finally:
        if viewer is not None:
            try:viewer.close();state['display_stop_confirmed']=True
            except Exception as error:state.update(cleanup_error=str(error),display_stop_confirmed=False);result=1
        elif dashboard is not None:
            try:dashboard.close()
            except Exception as error:state['cleanup_error']=str(error);result=1
        state['finished_board_utc']=datetime.now(timezone.utc).isoformat()
        write_json(output/'status.json',state);write_json(output/'summary.json',state)
        lock.close()
    return result


if __name__=='__main__':raise SystemExit(main())
