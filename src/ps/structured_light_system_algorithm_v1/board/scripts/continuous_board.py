"""Finite board checks for the continuous candidate; powered motion is separate.

static-bench: real fresh ambient-light frames, optional HDMI dashboard.
No guard ARM, motor START, laser operation or metric acceptance is performed.
"""
from __future__ import annotations
from pathlib import Path
import argparse,hashlib,json,sys,time,statistics,dataclasses

ROOT=Path(__file__).resolve().parents[2]
for p in (ROOT/'src/python',ROOT.parent/'structured_light_system_geometry_v1/src/python',ROOT/'board/scripts'):
    if str(p) not in sys.path:sys.path.insert(0,str(p))

def write_new(path,value):
    with Path(path).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)

def close_static_resources(state,*,scope,display,runtime,profiler,output,worker_unconfirmed=False):
    """Hardware cleanup precedes optional profiling I/O, even on disk errors."""
    if profiler is not None:
        try:profiler.disable()
        except BaseException as exc:state.update(passed=False,profile_disable_error=str(exc))
    if worker_unconfirmed and runtime is not None:
        runtime.session._poisoned=True;runtime.session._external_worker_unconfirmed=True
        state.update(passed=False,external_recovery_required=True)
    if scope is not None:
        try:scope.__exit__(None,None,None)
        except BaseException as exc:state.update(passed=False,generation_close_error=str(exc))
    if display is not None and not worker_unconfirmed:
        try:
            display.close();state['display_closed']=not display.started and display.buffer is None and not display.quarantined
            if not state['display_closed']:state['passed']=False
        except BaseException as exc:state.update(passed=False,display_close_error=str(exc))
    if runtime is not None and not worker_unconfirmed:
        try:
            state['cleanup']=runtime.close()
            if state['cleanup'].get('closed') is not True:state['passed']=False
        except BaseException as exc:state.update(passed=False,cleanup_error=str(exc))
    if profiler is not None:
        try:
            profiler.dump_stats(str(output/'loop_profile.pstats'))
            import pstats,io
            stream=io.StringIO();pstats.Stats(profiler,stream=stream).sort_stats('cumulative').print_stats(55)
            (output/'loop_profile.txt').write_text(stream.getvalue())
        except BaseException as exc:state.update(passed=False,profile_export_error=str(exc))

def static_bench(args):
    import resource
    from structured_light.motion import capture_handoff
    from structured_light.scan3d.stream_capture import FeaturePynqTransport,capture_features,exclusive_capture_generation
    from structured_light.motion.pynq_runtime import create_stage_b_runtime
    from structured_light.p2.reference import compare_same_frame
    from structured_light.frame_io import GrayFrame
    from structured_light.p2.camera_exposure import SCCBAccess
    out=Path(args.output);out.mkdir(parents=True,exist_ok=False)
    manifest=json.loads(Path(args.manifest).read_text())
    has_meta=manifest.get('frame_motion_metadata',{}).get('magic')==0x43534d31
    if args.require_metadata and not has_meta:raise ValueError('manifest does not admit CSM1 metadata')
    if args.load and args.power_off_proof is None:raise ValueError('load requires supplied user12V-off proof')
    capture_handoff.HandoffPynqTransport=FeaturePynqTransport
    state={'schema':'continuous_static_board_bench_v1','passed':False,'laser_available':False,
           'motor_start_count':0,'guard_arm_count':0,'measurement_acceptance':False,
           'overlay_downloads':int(args.load),'frames':[],'mode':'ambient_light_device_path_only',
           'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    rt=display=scope=profiler=display_pool=display_future=None
    display_worker_unconfirmed=False
    try:
        rt=create_stage_b_runtime(args.manifest,stage='static',load=args.load,attach=not args.load,
            power_off_confirmation=args.power_off_proof,loaded_overlay_receipt=args.receipt,
            output=out/'runtime',no_display=True,threshold=60,stripe_roi=(620,210,710,430))
        rt.transport.frame_motion_metadata_supported=has_meta
        scope=exclusive_capture_generation(rt);scope.__enter__()
        state['guard_before']=rt.guard.status();state['motor_before']=rt.motor.status()
        for key in ('active','fault','motor_busy','step_pin'):assert state['guard_before'][key] is False
        assert state['guard_before']['enable_pin'] is True
        trace=[]
        access=SCCBAccess(rt.transport._diag_mmio,trace,lambda:None)
        sensor={str(reg):access.read(reg) for reg in (0,7,0x0f,0x0b,0x35,0xaf,0xd5)}
        assert sensor['0']==0x1324
        state['sensor']=sensor
        renderer=None
        if args.display:
            from structured_light.p2.hdmi_output import create_output
            from structured_light.scan3d.dashboard import DashboardRenderer,DashboardStatus
            from b_alignment_preview import prepare_display_artifacts
            alias=prepare_display_artifacts(rt.artifacts,out)
            display=create_output(rt.overlay,alias,background_owner=False);display.configure()
            renderer=DashboardRenderer(font_path=args.font)
        started=time.monotonic();cpu_start=time.process_time();previous=None;next_draw=0
        qa_count=0;capture_times=[];draw_times=[];render_times=[];show_times=[];metas=[];canvas=None
        display_records=[]
        if args.parallel_display:
            from concurrent.futures import ThreadPoolExecutor
            display_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='hdmi_dashboard')
        def render_one(capture,index):
            before=time.monotonic()
            status=DashboardStatus(mode='设备通路检查',frame_number=capture.request_id,laser_available=False,source='live',
                camera_fps=(index+1)/max(.001,time.monotonic()-started),
                display_fps=len(display_records)/max(.001,time.monotonic()-started),model_fps=None)
            pixels=renderer.render(capture.raw,None,status=status,cloud_revision=0)
            rendered=time.monotonic()
            receipt=display.show(pixels,evidence=index in {0,args.frames//2,args.frames-1})
            return pixels,{'request_id':capture.request_id,'render_s':rendered-before,
                'show_s':time.monotonic()-rendered,'display_s':time.monotonic()-before,
                'canvas_sha256':receipt['canvas_sha256'],'completed_s':time.monotonic()-started}
        def collect_display(future):
            nonlocal canvas
            pixels,record=future.result(timeout=4.)
            canvas=pixels;display_records.append(record)
            draw_times.append(record['display_s']);render_times.append(record['render_s']);show_times.append(record['show_s'])
        if args.profile:
            import cProfile
            profiler=cProfile.Profile();profiler.enable()
        for index in range(args.frames):
            frame_start=time.monotonic()
            result=capture_features(rt.controller,require_motion_metadata=args.require_metadata)
            elapsed=time.monotonic()-frame_start;capture_times.append(elapsed)
            identity=(result.epoch,result.request_id)
            if previous is not None:assert identity==(previous[0],previous[1]+1),'frame identity discontinuity'
            previous=identity
            item={'index':index,'request_id':result.request_id,'epoch':result.epoch,
                  'capture_s':elapsed,'raw_sha256':result.raw_sha256,'motion':result.motion_metadata}
            if result.motion_metadata is not None:
                assert result.motion_metadata['emitted_command_count']==0,'motor command count changed during no-motion test'
                if metas:
                    assert result.motion_metadata['fv_ticks']>metas[-1]['fv_ticks'],'CSM frame time did not advance'
                    assert result.motion_metadata['admitted_event_count']==metas[-1]['admitted_event_count']+1,'CSM event sequence gap'
                    assert result.motion_metadata['motor_accepted_id']==metas[-1]['motor_accepted_id'],'CSM motor task changed without motion'
                    assert result.motion_metadata['motor_epoch']==metas[-1]['motor_epoch'],'CSM motor epoch changed without motion'
                metas.append(result.motion_metadata)
            if index in {0,args.frames//2,args.frames-1}:
                frame=GrayFrame.from_pixels(752,480,result.raw)
                params=rt.controller._centroid_parameters()
                comparison=compare_same_frame(frame,result.profile,diagnostics=result.diagnostics,
                                              roi=(620,210,710,430),**params)
                item['same_frame_comparison']=dataclasses.asdict(comparison)
                assert comparison.passed,'raw/Profile parity failed'
                qa_count+=1
            if index in {0,args.frames-1}:
                name=f'frame_{index:04d}.gray8.bin';(out/name).write_bytes(result.raw);item['raw_file']=name
            if display_future is not None and display_future.done():
                collect_display(display_future);display_future=None
            if renderer is not None and time.monotonic()>=next_draw and display_future is None:
                next_draw=max(next_draw+.2,time.monotonic())
                if display_pool is not None:display_future=display_pool.submit(render_one,result,index)
                else:
                    from concurrent.futures import Future
                    completed=Future();completed.set_result(render_one(result,index));collect_display(completed)
            state['frames'].append(item)
            if args.target_hz:
                delay=1/args.target_hz-(time.monotonic()-frame_start)
                if delay>0:time.sleep(delay)
        if display_future is not None:collect_display(display_future);display_future=None
        total=time.monotonic()-started
        import os
        cpu_seconds=time.process_time()-cpu_start;cpu_cores=os.cpu_count()
        if canvas is not None:
            import cv2
            assert cv2.imwrite(str(out/'hdmi_latest.png'),canvas)
        state.update(passed=True,total_s=total,received_fps=args.frames/total,
            capture_only_median_fps=1/statistics.median(capture_times),
            capture_s_median=statistics.median(capture_times),capture_s_max=max(capture_times),
            display_updates=len(draw_times),display_update_s_median=statistics.median(draw_times) if draw_times else None,
            display_update_hz=len(draw_times)/total,parallel_display=bool(display_pool),display_records=display_records,
            render_s_median=statistics.median(render_times) if render_times else None,
            hdmi_show_s_median=statistics.median(show_times) if show_times else None,
            cpu_seconds=cpu_seconds,cpu_cores=cpu_cores,cpu_one_core_percent=cpu_seconds/total*100,
            cpu_total_capacity_percent=cpu_seconds/total/cpu_cores*100 if cpu_cores else None,
            process_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            same_frame_qa_frames=qa_count,strict_valid_laser_profile_rate=None,physical_accuracy_verified=False,
            csm_metadata_verified=bool(metas),source='fresh_real_camera_ambient_light')
        state['guard_after']=rt.guard.status();state['motor_after']=rt.motor.status()
        assert state['motor_before']['epoch']==state['motor_after']['epoch']
        assert state['motor_before']['accepted_id']==state['motor_after']['accepted_id']
        assert state['motor_after']['emitted']==state['motor_before']['emitted']
        write_new(out/'sensor_trace.json',trace)
    except BaseException as exc:
        state.update(passed=False,error_type=type(exc).__name__,error=str(exc))
    finally:
        if display_future is not None:
            try:collect_display(display_future);display_future=None
            except BaseException as exc:
                display_worker_unconfirmed=not display_future.done()
                state.update(passed=False,display_worker_error=str(exc),display_worker_unconfirmed=display_worker_unconfirmed)
        if display_pool is not None:display_pool.shutdown(wait=not display_worker_unconfirmed,cancel_futures=True)
        close_static_resources(state,scope=scope,display=display,runtime=rt,profiler=profiler,
                               output=out,worker_unconfirmed=display_worker_unconfirmed)
        write_new(out/'RESULT.json',state)
    return state

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('static-bench');b.add_argument('--manifest',type=Path,required=True)
    b.add_argument('--output',type=Path,required=True);b.add_argument('--load',action='store_true')
    b.add_argument('--power-off-proof',type=Path);b.add_argument('--receipt',type=Path)
    b.add_argument('--frames',type=int,default=120);b.add_argument('--target-hz',type=float,default=0)
    b.add_argument('--require-metadata',action='store_true');b.add_argument('--display',action='store_true')
    b.add_argument('--font',type=Path)
    b.add_argument('--profile',action='store_true')
    b.add_argument('--parallel-display',action='store_true')
    args=p.parse_args()
    if not 1<=args.frames<=1000 or not 0<=args.target_hz<=30:p.error('finite frames1..1000 and targetHz0..30 required')
    if args.parallel_display and not args.display:p.error('--parallel-display requires --display')
    result=static_bench(args)
    print(json.dumps({k:v for k,v in result.items() if k not in {'frames','sensor_trace'}},ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
