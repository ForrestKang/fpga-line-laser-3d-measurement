"""Offline saved-input R22 pipeline; fake owner, no board imports or commands."""
from pathlib import Path
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import os
import shutil
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
for path in (ROOT,ROOT/'structured_light_system_algorithm_v1/src/python',ROOT/'structured_light_system_geometry_v1/src/python'):
    sys.path.insert(0,str(path))
sys.path.append('D:/Workspace/CodexWork/software_algorithm_package_20261017/verify-env/Lib/site-packages')
import cv2
import numpy as np
from resource_policy import StorageBudget,capacity_plan,BudgetExceeded
from candidate.multibranch_fast import extract_profiles,native_available
from candidate.replay_input_r21 import NativeImageReplay
from candidate.storage_r21 import MultiBranchChunkWriter,encode_profile,iter_records
from candidate.ps_pipeline_r22 import CloudSpool,ReplayProcessor
from candidate.display_r21 import renderer_factory
from virtual_scan.display_r22 import SessionDisplay


class FakeOwner:
    """Canvas consumer only: ACK explicitly denies real hardware evidence."""
    def __init__(self,config):self.configured=False;self.closed=False;self.shows=0
    def configure(self):self.configured=True
    def show_canvas(self,canvas):
        if not self.configured or self.closed:raise RuntimeError('fake owner lifecycle')
        if canvas.shape!=(720,1280,3):raise ValueError('native dashboard canvas expected')
        cv2.rectangle(canvas,(0,0),(1279,23),(0,50,80),-1)
        cv2.putText(canvas,'OFFLINE FAKE DISPLAY / SAVED SIMULATED INPUT / NO HARDWARE',
            (12,17),cv2.FONT_HERSHEY_SIMPLEX,.49,(255,235,160),1,cv2.LINE_AA)
        self.shows+=1
        return dict(fake_owner=True,real_HDMI_ACK=False,canvas_sha256=hashlib.sha256(memoryview(canvas)).hexdigest())
    def close(self,timeout_s=5.):self.closed=True;return dict(owner_closed=True,fake_owner=True)


def timed(call,*args,**kwargs):
    wall=time.perf_counter();cpu=time.thread_time();value=call(*args,**kwargs)
    return value,dict(wall_s=time.perf_counter()-wall,thread_cpu_s=time.thread_time()-cpu)


def audit(reference,actual):
    result=dict(frames=0,raw_byte_equal=True,profile_byte_equal=True,all_arrays_byte_equal=True,
        accepted_reasons_quality_equal=True,XYZ_numeric_equal=True,max_XYZ_abs_error_mm=0.)
    left=iter(iter_records(reference));right=iter(iter_records(actual))
    while True:
        a=next(left,None);b=next(right,None)
        if a is None or b is None:
            if a is not b:raise AssertionError('reference frame count differs')
            break
        if a['kind']!=b['kind']:raise AssertionError('reference order differs')
        if a['kind']=='event':continue
        result['frames']+=1
        result['raw_byte_equal'] &= a['raw']==b['raw']
        result['profile_byte_equal'] &= a['profile_payload']==b['profile_payload']
        for key,value in a['profile'].items():
            other=b['profile'][key]
            if isinstance(value,np.ndarray):
                exact=value.dtype==other.dtype and value.shape==other.shape and value.tobytes()==other.tobytes()
                result['all_arrays_byte_equal'] &= exact
                if key.startswith('points_'):
                    equal=np.allclose(value,other,rtol=0,atol=1e-10,equal_nan=True)
                    result['XYZ_numeric_equal'] &= bool(equal)
                    mask=np.isfinite(value)&np.isfinite(other)
                    if mask.any():result['max_XYZ_abs_error_mm']=max(result['max_XYZ_abs_error_mm'],float(np.max(np.abs(value[mask]-other[mask]))))
                elif not np.array_equal(value,other):result['accepted_reasons_quality_equal']=False
            elif key=='observation_quality':
                for name,array in value.items():
                    result['accepted_reasons_quality_equal'] &= bool(np.array_equal(array,other[name]))
            elif value!=other:result['accepted_reasons_quality_equal']=False
    if not all(result[key] for key in ('raw_byte_equal','profile_byte_equal','all_arrays_byte_equal',
                                      'accepted_reasons_quality_equal','XYZ_numeric_equal')):
        raise AssertionError(result)
    return result


def replay_target(target,codec,out,display,budget,reference_root,limit,stop_event=None,input_root=None,
                  *,writer_group_size=8,writer_max_delay_s=.05):
    invoked=time.perf_counter()
    input_root=ROOT/'inputs' if input_root is None else Path(input_root)
    out.mkdir();source=NativeImageReplay(input_root/target)
    spool=writer=None
    totals={};frame_count=0;valid_count=0;pending=None
    producer_times=[];admission_times=[];stage_frames=[];boundaries={}
    def add(name,timing):
        current=totals.setdefault(name,dict(wall_s=0.,thread_cpu_s=0.))
        for key in current:current[key]+=timing[key]
    def persist(pending,log):
        profile,raw,frame,future,summary,camera_delta,axis_delta,frame_stages=pending
        (packet,encoding),timing=timed(future.result);add('serialization_wait',timing);add('serialization_worker',encoding)
        frame_stages.update(serialization_wait=timing,serialization_worker=encoding)
        record=dict(request_id=frame['frame_id'],capture_epoch=1,raw=raw,raw_sha256=frame['raw_sha256'],
            profile_payload=packet,points_camera_mm=camera_delta,points_axis_mm=axis_delta,
            summary=summary,motion=dict(source='SIMULATION_ONLY_angle',angle_rad=frame['angle_rad'],motor_command=False),
            exposure=dict(source='SIMULATED_IMAGE',lines=60,revision=0))
        waited,timing=timed(writer.submit_wait,record,timeout_s=5.);add('writer_admission',timing)
        frame_stages['writer_admission']=timing
        frame_stages['writer_admission_detail']=dict(writer.last_admission_timings)
        for name,detail in writer.last_admission_timings.items():add('admission_'+name,detail)
        admission_times.append(time.perf_counter()-started)
        def write_log():
            log.write(json.dumps(dict(frame_id=frame['frame_id'],valid_points=int(profile.valid_points.sum()),
                profile_sha256=hashlib.sha256(packet).hexdigest(),backpressure=waited))+'\n')
        _,timing=timed(write_log);add('profiles_log_write',timing);frame_stages['profiles_log_write']=timing
    try:
        spool=CloudSpool(out/'cloud.float64.bin',storage_budget=budget)
        processor=ReplayProcessor.with_spool(input_root/target/'calibration.json',spool,extractor=extract_profiles,max_frames=limit)
        writer=MultiBranchChunkWriter(out/'frames',capacity=32,group_size=writer_group_size,
            max_delay_s=writer_max_delay_s,raw_codec=codec,storage_budget=budget)
        display.begin_target(target+'/'+codec)
        started=time.perf_counter();process_cpu=time.process_time()
        boundaries['production_start_s']=0.
        pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='r22-serialize')
        with (out/'profiles.jsonl').open('x',encoding='utf8') as log:
            try:
                for frame in source.frames[:limit]:
                    if stop_event is not None and stop_event.is_set():raise RuntimeError('resource monitor requested bounded replay stop')
                    raw,timing=timed(source.read_frame,frame['frame_id']);add('decode_hash',timing)
                    frame_stages=dict(frame_id=frame['frame_id'],input=dict(source.last_stage_timings))
                    for name,detail in source.last_stage_timings.items():add('input_'+name,detail)
                    gray=np.frombuffer(raw,np.uint8).reshape(480,752)
                    profile,timing=timed(processor.process,gray,dict(frame,request_id=frame['frame_id'],capture_epoch=1,exposure_lines=60,exposure_revision=0));add('XYZ_fusion',timing)
                    frame_stages['processor']=dict(processor.last_stage_timings)
                    for name,detail in processor.last_stage_timings.items():add('processor_'+name,detail)
                    eligible=bool(profile.simulation_geometry_valid and profile.valid_points.sum()>=3)
                    if eligible:producer_times.append(time.perf_counter()-started)
                    if pending:persist(pending,log)
                    def prepare():return profile.summary(),profile.delta_camera_mm,profile.delta_axis_mm
                    (summary,camera_delta,axis_delta),timing=timed(prepare);add('summary_deltas',timing)
                    frame_stages['summary_deltas']=timing
                    pending=(profile,raw,frame,pool.submit(timed,encode_profile,profile,summary),summary,camera_delta,axis_delta,frame_stages)
                    _,timing=timed(display.submit,gray,axis_delta,frame['frame_id'],frame['angle_rad']);add('display_submit',timing)
                    frame_count+=1;valid_count+=eligible
                    stage_frames.append(frame_stages)
                boundaries['production_end_s']=time.perf_counter()-started
                if pending:
                    _,timing=timed(persist,pending,log);add('final_pending_admission',timing)
                boundaries['admission_end_s']=time.perf_counter()-started
                def sync_log():log.flush();os.fsync(log.fileno())
                _,timing=timed(sync_log);add('profiles_log_sync',timing)
                boundaries['profiles_log_sync_end_s']=time.perf_counter()-started
            finally:
                _,timing=timed(pool.shutdown,wait=True);add('serialization_pool_drain',timing)
                boundaries['serialization_pool_drain_end_s']=time.perf_counter()-started
        receipt,timing=timed(writer.close,timeout_s=10.);add('writer_drain',timing)
        if not receipt.durable or not receipt.worker_joined:raise RuntimeError('writer drain unconfirmed')
        writer.pending_bytes
        boundaries['writer_drain_end_s']=time.perf_counter()-started
        _,timing=timed(spool.export_npy,out/'cloud.npy');add('cloud_export',timing)
        boundaries['export_end_s']=time.perf_counter()-started
        _,timing=timed(spool.close);add('cloud_close_sync',timing)
        boundaries['cloud_close_end_s']=time.perf_counter()-started
        _,timing=timed(display.begin_target,target+'/completed');add('display_target_drain',timing)
        elapsed=time.perf_counter()-started
        boundaries['display_drain_end_s']=elapsed
        durable_times=[batch['sync_done_perf_s']-started for batch in writer.batch_stats for _ in range(batch['frame_count'])]
        boundaries['durable_end_s']=max(durable_times,default=0.)
        result=dict(target=target,codec=codec,frames=frame_count,valid_XYZ_profiles=valid_count,cloud_points=spool.count,
            wall_s=elapsed,process_cpu_s=time.process_time()-process_cpu,phases=totals,writer=asdict(receipt),
            duration_s=elapsed,active_duration_s=boundaries['production_end_s'],producer_times_s=producer_times,
            durable_times_s=durable_times,admission_times_s=admission_times,boundaries=boundaries,
            total_invocation_duration_s=time.perf_counter()-invoked,cold_start_s=started-invoked,
            production_duration_s=boundaries['production_end_s'],
            activity_interval_definition='decode/process/submit scheduling start through final display submit; final admission, fsync, drain and export remain in full duration',
            full_duration_includes_persistence_tail=True,stage_frames=stage_frames,
            writer_policy=dict(group_size=writer_group_size,max_delay_s=writer_max_delay_s,capacity=32,byte_capacity=64*1024*1024),
            display_times_s=[row['ack_perf_s']-started for row in display.records if row['target']==target+'/'+codec],
            pipeline_start_perf_s=started,CPU_total_capacity_pct=None,mem_available_min_bytes=None,
            peak_pending_bytes=writer.peak_pending_bytes,peak_pending_items=writer.peak_pending_items,
            batch_stats=list(writer.batch_stats),evidence_type='PC_SAVED_SIMULATED_IMAGES_FAKE_DISPLAY_NO_HARDWARE',
            ARM_speed_verified=False,physical_accuracy_verified=False,FPGA_stream_execution=False,board_access=False,
            feature_discovery_included=False,display_owner='fake',display_hardware_ACK=False,
            timing_scope='decode/hash + XYZ/fusion/cloud spool + staged serialization + profiles fsync + pool drain + durable writer drain + full cloud export + cloud close fsync + display drain; display async; reference audits excluded',
            all_raw_candidates_reasons_quality_float64XYZ_preserved=True)
        if reference_root and limit==240:
            reference=reference_root/target
            result['R21_reference']=audit(reference/'frames',out/'frames')
            original=np.load(reference/'cloud.npy',mmap_mode='r');actual=np.load(out/'cloud.npy',mmap_mode='r')
            result['cloud_byte_equal']=original.dtype==actual.dtype and original.shape==actual.shape and all(
                original[i:i+4096].tobytes()==actual[i:i+4096].tobytes() for i in range(0,len(actual),4096))
            result['cloud_numeric_equal']=original.shape==actual.shape and all(np.allclose(original[i:i+4096],actual[i:i+4096],rtol=0,atol=1e-10) for i in range(0,len(actual),4096))
            if not result['cloud_numeric_equal']:raise AssertionError('R21 cloud numeric mismatch')
        (out/'RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(dict(target=target,codec=codec,frames=frame_count,wall_s=elapsed)),flush=True)
        return result
    finally:
        try:
            final_writer=writer.close(timeout_s=10.) if writer is not None else None
            with (out/'CLEANUP.json').open('x') as cleanup:
                json.dump(dict(writer_worker_joined=final_writer is None or final_writer.worker_joined,
                    writer_worker_unconfirmed=final_writer is not None and final_writer.worker_unconfirmed,
                    writer_durable=final_writer is None or final_writer.durable),cleanup)
        except BaseException as exc:
            receipt=getattr(exc,'receipt',None)
            with (out/'CLEANUP.json').open('x') as cleanup:
                json.dump(dict(writer_worker_joined=getattr(receipt,'worker_joined',False),
                    writer_worker_unconfirmed=getattr(receipt,'worker_unconfirmed',True),error=str(exc)),cleanup)
            raise
        finally:
            if spool is not None:spool.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--input-root',type=Path,default=ROOT/'inputs')
    parser.add_argument('--limit',type=int,default=240);parser.add_argument('--reference-root',type=Path,
        help='optional preserved R21 round03 directory; never required to run a relocated package')
    parser.add_argument('--writer-group-size',type=int,default=8)
    parser.add_argument('--writer-max-delay-s',type=float,default=.05)
    args=parser.parse_args()
    if not 1<=args.limit<=240:raise ValueError('finite 1..240 frame budget required')
    args.output.mkdir(parents=True,exist_ok=False);cv2.setNumThreads(1)
    # 4MiB/frame includes raw expansion, <=3840 candidates, duplicate XYZ,
    # spool and full export, index/diagnostics. 25% headroom is additional.
    existing=sum(path.stat().st_size for path in args.output.parent.rglob('*') if path.is_file())
    plan=capacity_plan(4*args.limit,4*1024*1024,existing_data_bytes=existing,filesystem_free_bytes=shutil.disk_usage(args.output).free)
    if not plan['admitted']:raise BudgetExceeded(str(plan))
    budget=StorageBudget(existing,lambda:shutil.disk_usage(args.output).free)
    display=SessionDisplay(owner_factory=FakeOwner,renderer_factory=renderer_factory,
        config=dict(frame_total=args.limit,target='OFFLINE FAKE DISPLAY'))
    runs=[];close=None
    try:
        for codec in ('none','zlib1'):
            for target in ('T05','T06'):
                runs.append(replay_target(target,codec,args.output/(target+'_'+codec),display,budget,args.reference_root,args.limit,
                    input_root=args.input_root,writer_group_size=args.writer_group_size,writer_max_delay_s=args.writer_max_delay_s))
        parity={target:audit(args.output/(target+'_none')/'frames',args.output/(target+'_zlib1')/'frames') for target in ('T05','T06')}
    finally:close=display.close(timeout_s=10.)
    result=dict(passed=True,evidence_type='PC_OFFLINE_ONLY_FAKE_DISPLAY',native_extractor=native_available(),
        owner_configure_count=1,targets_per_session=4,display=close,codec_parity=parity,capacity_plan=plan,
        storage_accounted_bytes=budget.data_used,storage_initial_inventory_bytes=existing,runs=runs,ARM_speed_verified=False,real_HDMI_verified=False,
        FPGA_stream_execution=False,board_access=False,feature_discovery_included=False)
    (args.output/'RESULT.json').write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__':main()
