"""Finite actual-PL DDR replay, simulated geometry, complete source persistence.

Attach an already loaded R25 overlay only. This module never downloads FPGA,
starts a camera, writes a motor or runs a CPU image frontend. The frozen-input
SHA-bound capacity table admits1..240frames/target and1..3repeats subject to
unchanged storage limits. MOCK_ONLY injection is an internal unit-test option.
"""
from pathlib import Path
import argparse
from contextlib import ExitStack, nullcontext
from dataclasses import asdict,dataclass
from functools import wraps
import hashlib
import json
import os
import platform
import shutil
import sys
import threading
import time
from types import MappingProxyType
from unittest.mock import patch

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
for directory in (ROOT,ROOT/'structured_light_system_algorithm_v1/src/python',
                  ROOT/'structured_light_system_geometry_v1/src/python'):
    sys.path.insert(0,str(directory))
from structured_light.scan3d.payload_digest import payload_sha256
from candidate.io_policy_r25 import DEFAULT_IO_POLICY

FRAME_BOUND=32*1024*1024
MAX_SHORT_LIMIT=16
CAPACITY_SHA256='d06ca3451fdb9b07854b0be36f1265390383fe8ea63f006a0ce9d90939592b8e'


class NormalImageGuard:
    """Count and fail known CPU frontend entrypoints during the complete run.

    These process-wide patches require serialized replay; they do not intercept
    image copying, hashes, display resizing or numeric geometry.
    """
    TARGETS={
        'extraction':('candidate.multibranch_fast.extract_profiles',
            'candidate.multibranch_fast._reference','reference_candidate.multi_stripe.extract_profiles',
            'structured_light.stripe_v1.candidates.extract_candidates',
            'candidate.pl_features_r22.extract_candidates',
            'structured_light.scan3d.multibranch_processor_r21._default_extractor',
            'candidate.v3_reference_r25.extract_v3'),
        'branch_link':('candidate.pl_features_r22.link_candidates',),
        'Raw_quality':('candidate.quality_fast.assess_multibranch_quality',
            'structured_light.scan3d.multibranch_processor_r21.MultiBranchProcessor._quality',
            'structured_light.scan3d.multibranch_processor_r21.assess_observed_quality')}
    def __init__(self):
        self.calls={key:0 for key in self.TARGETS};self.entrypoints={};self._stack=ExitStack()
    def _forbidden(self,category,name):
        def fail(*args,**kwargs):
            self.calls[category]+=1;self.entrypoints[name]=self.entrypoints.get(name,0)+1
            raise RuntimeError('normal PL replay attempted ARM image algorithm: '+name)
        return fail
    def __enter__(self):
        try:
            for category,names in self.TARGETS.items():
                for name in names:self._stack.enter_context(patch(name,self._forbidden(category,name)))
        except BaseException:self._stack.close();raise
        return self
    def __exit__(self,*args):return self._stack.__exit__(*args)
    def report(self):
        return dict(ARM_pixel_extraction_calls=self.calls['extraction'],
            ARM_branch_link_calls=self.calls['branch_link'],ARM_Raw_quality_calls=self.calls['Raw_quality'],
            normal_image_guard=dict(active=True,entrypoints=list(sum(self.TARGETS.values(),())),
                intercepted_calls=dict(self.entrypoints),scope='KNOWN_ENTRYPOINTS_PROCESS_WIDE_SERIAL_RUN'))


def _guarded(call):
    @wraps(call)
    def run(*args,**kwargs):
        with NormalImageGuard() as guard:return call(*args,**kwargs,_image_guard=guard)
    return run


@dataclass(frozen=True)
class FrozenInputCapacity:
    target:str
    input_manifest_sha256:str
    entries:tuple
    table_sha256:str=CAPACITY_SHA256


def load_capacity_inputs(input_root,targets,limit,*,path=ROOT/'candidate/capacity_inputs_r25.json'):
    """Read-only storage planning. None of these counts enters image processing."""
    if type(limit) is not int or not 1<=limit<=240:raise ValueError('capacity limit must be1..240')
    data=Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest()!=CAPACITY_SHA256:raise ValueError('frozen capacity table SHA mismatch')
    table=json.loads(data)
    from candidate.pfo3_transport_r25 import DEFAULT_FINGERPRINT
    if table['schema']!='R25_FROZEN_INPUT_CAPACITY_V1' or table['parameter_fingerprint']!=DEFAULT_FINGERPRINT:
        raise ValueError('capacity table schema/fingerprint mismatch')
    selected={}
    for target in targets:
        source=Path(input_root)/target;entry=table['targets'][target]
        manifest_sha=hashlib.sha256((source/'INPUT_MANIFEST.json').read_bytes()).hexdigest()
        if manifest_sha!=entry['input_manifest_sha256']:raise ValueError('capacity/input manifest SHA mismatch')
        manifest=json.loads((source/'INPUT_MANIFEST.json').read_bytes())
        frame_data=(source/'frames.jsonl').read_bytes()
        if hashlib.sha256(frame_data).hexdigest()!=manifest['frames_sha256']:raise ValueError('capacity source metadata SHA mismatch')
        frames=[json.loads(line) for line in frame_data.splitlines()];rows=entry['frames']
        if len(rows)!=240 or len(frames)!=240:raise ValueError('complete frozen capacity source required')
        for index,(row,saved) in enumerate(zip(rows,frames)):
            if row['frame_id']!=index or saved['frame_id']!=index or row['raw_sha256']!=saved['raw_sha256']:
                raise ValueError('capacity Raw identity mismatch')
            for key in ('diagnostic_bound','qualified_bound','primary_bound','pfo3_bound','frame_bound_bytes'):
                if type(row[key]) is not int or row[key]<0:raise ValueError('invalid capacity integer')
            if row['frame_bound_bytes']<752*480+row['primary_bound']+row['pfo3_bound']:
                raise ValueError('capacity frame bound omits source bytes')
        selected[target]=FrozenInputCapacity(target,manifest_sha,tuple(MappingProxyType(dict(row)) for row in rows[:limit]))
    return selected


def timed(call,*args,**kwargs):
    wall=time.perf_counter();cpu=time.thread_time();value=call(*args,**kwargs)
    return value,dict(wall_s=time.perf_counter()-wall,thread_cpu_s=time.thread_time()-cpu)


class WriterDrainDeadlineR30:
    """One monotonic drain budget shared by normal exit and failure cleanup.

    Expiry never cancels accepted work or releases its owner. A repeated close
    can observe the current receipt, but cannot start another full wait budget.
    The caller still requires joined, durable, exact written count and readback.
    """
    def __init__(self,timeout_s=120.):
        if isinstance(timeout_s,bool) or not isinstance(timeout_s,(int,float)) or not np.isfinite(timeout_s) or timeout_s<0:
            raise ValueError('drain timeout must be finite and non-negative')
        self.timeout_s=float(timeout_s);self.started=None;self.deadline=None;self.calls=0

    def close(self,writer):
        now=time.monotonic()
        if self.deadline is None:
            self.started=now;self.deadline=now+self.timeout_s
        self.calls+=1
        return writer.close(timeout_s=max(0.,self.deadline-now))

    def snapshot(self):
        elapsed=None if self.started is None else max(0.,time.monotonic()-self.started)
        return dict(timeout_s=self.timeout_s,clock='monotonic',started_monotonic_s=self.started,
            deadline_monotonic_s=self.deadline,elapsed_s=elapsed,close_calls=self.calls,
            deadline_expired=bool(elapsed is not None and elapsed>=self.timeout_s))


def display_capacity_plan(capacities):
    """Allocate only SHA-admitted display sources; counts never enter geometry.

    Targets reuse the same cloud after a confirmed display drain. Qualified
    candidates bound the number of preview XYZ points; actual excess remains
    a hard failure, never an invitation to truncate measurement data.
    """
    if not capacities:raise ValueError('frozen display capacity required')
    archives=[];clouds=[]
    for target,capacity in capacities.items():
        if (type(capacity) is not FrozenInputCapacity or capacity.target!=target
                or capacity.table_sha256!=CAPACITY_SHA256 or not capacity.entries):
            raise ValueError('unbound display capacity')
        archives.extend(row['pfo3_bound'] for row in capacity.entries)
        clouds.append(sum(row['qualified_bound'] for row in capacity.entries)*24)
    rounded=lambda n:((n+4095)//4096)*4096
    archive=rounded(max(256+480*64,max(archives))+4096)
    cloud=rounded(max(24,max(clouds))+65536)
    if archive>12*1024**2 or cloud>64*1024**2:raise BufferError('display shared allocation exceeds budget')
    return dict(archive_slot_bytes=archive,cloud_bytes=cloud)


def save_json(path,value,budget=None):
    data=(json.dumps(value,indent=2,allow_nan=False)+'\n').encode('utf-8')
    token=budget.reserve((len(data)*5+3)//4) if budget is not None else None
    with Path(path).open('xb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    if token is not None:budget.commit(token,len(data))


def replay_identity(index,epoch,*,test_only=False):
    # Replay identity contains explicit software tags, not observed camera or
    # motor counters. No real capture timing/rotation is inferred from zeros.
    return dict(frame_id=index,epoch=epoch,cfg_version=1,exposure_version=0,
        motor_id=0,motor_epoch=0,motor_steps=0,FV_ticks=0,exposure_start=0,exposure_end=0,
        source=3 if test_only else 2)


def replay_metadata(frame,identity):
    if type(frame.get('frame_id')) is not int or frame['frame_id']!=identity['frame_id']:
        raise ValueError('saved source frame identity mismatch')
    if 'timestamp_s' not in frame or 'angle_rad' not in frame:
        raise ValueError('explicit simulated timestamp and angle required')
    metadata=dict(frame,request_id=identity['frame_id'],capture_epoch=identity['epoch'],
        feature_config_revision=identity['cfg_version'],exposure_lines=60,
        exposure_revision=identity['exposure_version'],motor_accepted_id=identity['motor_id'],
        motor_epoch=identity['motor_epoch'],emitted_command_count=identity['motor_steps'],
        fv_ticks=identity['FV_ticks'],exposure_start_ticks=identity['exposure_start'],
        exposure_end_ticks=identity['exposure_end'],replay_identity_tags=True,
        physical_capture_timing_known=False,physical_motor_registration=False,
        simulated_exposure_lines_assumed=60,geometry_evidence_type='SIMULATION_ONLY')
    return metadata


class _PreviewNoHistory:
    """The display owns its bounded preview cloud; do not retain a second list."""
    def append(self,points):
        if points.ndim!=2 or points.shape[1]!=3 or not np.isfinite(points).all():
            raise ValueError('finite preview XYZ required')


def make_processors(calibration,spool,limit,*,processor_class=None):
    from candidate.pfo3_processor_r25 import PLFinalProcessorR25
    cls=PLFinalProcessorR25 if processor_class is None else processor_class
    measurement=cls.from_virtual_config(calibration,max_cloud_points=limit*7680)
    preview=cls.from_virtual_config(calibration,max_cloud_points=limit*7680)
    measurement._cloud=spool;preview._cloud=_PreviewNoHistory()
    return measurement,preview


class _DurableReplayTrace:
    def __init__(self):self.events=[];self._lock=threading.Lock()
    def durable_group(self,group,at):
        with self._lock:
            for item in group:
                if item.kind=='frame' and item.payload['summary'].get('r25_nonempty_measurement_XYZ') is True:
                    self.events.append((item.payload['request_id'],at))


def _check_transport(frame,primary,receipt,admission,raw_sha,*,test_only):
    from candidate.pfo3_r25 import PFO3Error
    admission.validate()
    if type(primary) is not bytes or type(frame.payload) is not bytes:
        raise PFO3Error('immutable full primary and PFO3 payloads required')
    if test_only:
        from candidate.provenance_reuse_r25 import PacketDigestsR25
        digests=PacketDigestsR25(hashlib.sha256(primary).hexdigest(),hashlib.sha256(frame.payload).hexdigest())
        if receipt.get('primary_stream_sha256')!=digests.primary_sha256 or receipt.get('assembled_sha256')!=digests.final_sha256:
            raise PFO3Error('transport/archive receipt hash mismatch')
        if not admission.fixture_only or receipt.get('actual_PL_execution') is True:
            raise PFO3Error('MOCK_ONLY run cannot admit actual hardware claims')
    else:
        if (admission.fixture_only or admission.hardware_completed is not True
                or receipt.get('actual_PL_execution') is not True
                or frame.header['source']!=2 or admission.input_raw_sha256!=raw_sha):
            raise PFO3Error('actual same-Raw DDR PL completion required')
        from candidate.provenance_reuse_r25 import CompletedPairProofR25
        proof=CompletedPairProofR25.from_completion(admission,frame,primary,input_raw_sha256=raw_sha,
            expected_identity=admission.identity)
        digests=proof.check_receipt(receipt,admission,frame,primary,input_raw_sha256=raw_sha,
            expected_identity=admission.identity)
    return digests


def _save_rejected_input(output,index,raw,frame,primary,receipt,budget):
    directory=output/f'rejected_{index:04d}';directory.mkdir()
    for name,data in (('raw.gray8.bin',raw),('source.pfo3',frame.payload),('primary.bin',primary)):
        token=budget.reserve((len(data)*5+3)//4)
        with (directory/name).open('xb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        budget.commit(token,len(data))
    save_json(directory/'receipt.json',dict(receipt,implicit_CPU_fallback=False),budget)


@_guarded
def replay_target_pl(target,output,display,budget,transport,*,input_root,limit=1,epoch=1,
                     raw_codec='zlib1',writer_group_size=DEFAULT_IO_POLICY.group_size,
                     writer_max_delay_s=DEFAULT_IO_POLICY.max_delay_s,
                     stop_event=None,source_factory=None,writer_factory=None,processor_factory=None,
                     test_only=False,capacity=None,parallel_pl=False,geometry_backend='separate',_image_guard=None,
                     persist_preview=False,post_features=False,feature_workers=1,feature_policy='v4',
                     writer_validation_phase='submit',payload_preallocate_bytes=0,payload_run_bound_bytes=0,
                     storage_backend='chunk_v3',journal_buffer_bytes=512*1024):
    """One bounded target. Real calls require transport-issued hardware capability.

    Test injection never certifies actual PL, HDMI, durable device storage or
    performance. No software extractor is available as a normal-path fallback.
    """
    if type(limit) is not int or not 1<=limit<=240:raise ValueError('replay limit must be1..240')
    if feature_policy not in ('v4','v5','v6'):raise ValueError('explicit feature policy version required')
    if type(feature_workers) is not int or feature_workers not in (1,2):
        raise ValueError('feature workers must be 1 or 2')
    if writer_validation_phase not in ('submit','worker'):
        raise ValueError('writer validation phase must be submit or worker')
    if geometry_backend not in ('separate','paired','prepared_serial'):
        raise ValueError('explicit geometry backend required')
    if geometry_backend in ('paired','prepared_serial') and (test_only or processor_factory is not None):
        raise ValueError('shared-geometry modes require actual retained PL admission and concrete processors')
    if capacity is None and limit>MAX_SHORT_LIMIT:
        raise ValueError('long replay requires SHA-bound capacity table')
    if capacity is not None:
        if (not isinstance(capacity,FrozenInputCapacity) or capacity.table_sha256!=CAPACITY_SHA256
                or capacity.target!=target or len(capacity.entries)!=limit):
            raise ValueError('capacity table selection differs from replay')
        if hashlib.sha256((Path(input_root)/target/'INPUT_MANIFEST.json').read_bytes()).hexdigest()!=capacity.input_manifest_sha256:
            raise ValueError('capacity input manifest changed')
    if type(epoch) is not int or not 1<=epoch<65536:raise ValueError('explicit uint16 replay epoch required')
    from candidate.ps_pipeline_r22 import CloudSpool
    from candidate.storage_r21 import MultiBranchChunkWriter,encode_profile
    from candidate.replay_input_r21 import NativeImageReplay
    from tools.board_replay_r22 import valid_vdma_ack
    from candidate.pfo3_r25 import PFO3Error
    from candidate.display_pfo3_r25 import annotations_from_frame,DeferredPLAnnotations
    pipeline_r30=os.environ.get('SCAN3D_R30_PIPELINE')=='unified'
    if pipeline_r30 and (test_only or writer_factory is not None):raise ValueError('R30 requires actual PL and strict writer')
    if pipeline_r30:
        from candidate.frame_persistence_r30 import UnifiedFrameWriterR30,DeferredCloudR30
        CloudSpool=DeferredCloudR30
        MultiBranchChunkWriter=UnifiedFrameWriterR30
    source_factory=source_factory or NativeImageReplay
    writer_factory=writer_factory or MultiBranchChunkWriter
    if geometry_backend in ('paired','prepared_serial'):
        from candidate.pfo3_geometry_pair_r25 import (PLGeometryPairProcessorR25,
            PreparedGeometryPairR25,PairedModeExecutor)
        processor_factory=lambda calibration,spool,limit:make_processors(calibration,spool,limit,
            processor_class=PLGeometryPairProcessorR25)
    else:processor_factory=processor_factory or make_processors
    from candidate.scan_session_r26 import ScanSession
    from candidate.replay_finalize_r26 import close_preview_evidence,verify_replay_saved
    session=ScanSession(source_kind='offline' if test_only else 'actual_pl')
    output=Path(output);output.mkdir()
    invoked=time.perf_counter();source=spool=writer=acquisition=mode_executor=None;started=None
    acquisition_receipt=None;publisher=None;publisher_receipt=None;coordinator=None;coordinator_receipt=None
    completed=False;writer_receipt=None;frame_count=nonempty=0;unpersisted=None
    writer_drain=WriterDrainDeadlineR30() if pipeline_r30 else None
    accepted_measurement_points=accepted_measurement_frames=0
    preview_stream=None;preview_index=[];preview_offset=preview_points=0
    preview_close_receipt=None;readback=None
    phases={};stages=[];producer=[];admissions=[];trace=_DurableReplayTrace()
    boundaries={};tag=target+'/'+output.name
    def add(name,timing):
        total=phases.setdefault(name,dict(wall_s=0.,thread_cpu_s=0.))
        for key in total:total[key]+=timing[key]
    def call_stage(name,call,*args,**kwargs):
        value,timing=timed(call,*args,**kwargs);add(name,timing);return value,timing
    try:
        session.transition('ready')
        session.transition('processing')
        source=source_factory(Path(input_root)/target)
        if len(source.frames)<limit:raise ValueError('insufficient frozen source frames')
        spool=CloudSpool(output/'cloud.float64.bin',storage_budget=budget)
        preview_stream=None if pipeline_r30 or not persist_preview else (output/'preview.float64.bin').open('xb')
        measure_processor,preview_processor=processor_factory(Path(input_root)/target/'calibration.json',spool,limit)
        mode_executor=PairedModeExecutor() if geometry_backend=='paired' else None
        from candidate.observation_core_r26 import ObservationProcessingCore
        shared_core=ObservationProcessingCore(measure_processor,preview_processor) if geometry_backend=='prepared_serial' else None
        writer_options={} if writer_validation_phase=='submit' else dict(validation_phase=writer_validation_phase)
        if storage_backend!='chunk_v3':writer_options['storage_backend']=storage_backend
        if journal_buffer_bytes!=512*1024:writer_options['journal_buffer_bytes']=journal_buffer_bytes
        if payload_preallocate_bytes:
            writer_options.update(preallocate_payload_bytes=payload_preallocate_bytes,
                preallocate_run_bound_bytes=payload_run_bound_bytes)
        writer=writer_factory(output/'frames',capacity=128 if pipeline_r30 else DEFAULT_IO_POLICY.capacity,group_size=writer_group_size,
            max_delay_s=writer_max_delay_s,raw_codec=raw_codec,storage_budget=budget,
            byte_capacity=DEFAULT_IO_POLICY.byte_capacity,**writer_options)
        writer.performance_trace=trace
        if pipeline_r30:spool.writer=writer
        if payload_preallocate_bytes:writer.wait_payload_preallocation(timeout_s=10.)
        display.begin_target(tag)
        if pipeline_r30:
            from candidate.display_publisher_r30 import DisplayPublisherR30
            publisher=DisplayPublisherR30(display)
            from candidate.pipeline_status_r30 import PipelineStatusR30
            coordinator=PipelineStatusR30(display,writer)
        # Snapshot only after begin_target has drained the prior target; an ACK
        # from the prior target must not be attributed to this run.
        display_counts_before=dict(
            submitted=int(getattr(display,'submitted',0)),
            acknowledged=int(getattr(display,'acknowledged',0)),
            dropped=int(getattr(display,'dropped',0)))
        started=time.perf_counter();cpu_started=time.process_time()
        if parallel_pl:
            from candidate.pl_producer_r25 import PLFrameProducerR25
            def preserve(packet):
                _save_rejected_input(output,packet.index,packet.raw,packet.final,packet.primary,packet.receipt,budget)
            def preserve_failure(index,raw,identity,error):
                directory=output/f'failed_acquisition_{index}';directory.mkdir()
                if type(raw) is bytes:
                    ticket=budget.reserve((len(raw)*5+3)//4)
                    with (directory/'input.gray8.bin').open('xb') as stream:
                        stream.write(raw);stream.flush();os.fsync(stream.fileno())
                    budget.commit(ticket,len(raw))
                failed_primary=getattr(transport,'failed_primary',None)
                if type(failed_primary) is bytes:
                    ticket=budget.reserve((len(failed_primary)*5+3)//4)
                    with (directory/'UNVALIDATED_PRIMARY.bin').open('xb') as stream:
                        stream.write(failed_primary);stream.flush();os.fsync(stream.fileno())
                    budget.commit(ticket,len(failed_primary))
                save_json(directory/'ERROR.json',dict(error=error,identity=identity,
                    input_only=failed_primary is None,completed_PL_result_available=False,
                    transport_failure=getattr(transport,'failure_metadata',{}),automatic_retry=False),budget)
            acquisition=PLFrameProducerR25(source.frames[:limit],source,transport,
                identity_factory=lambda index:replay_identity(index,epoch,test_only=test_only),
                preserve=preserve,preserve_failure=preserve_failure,stop_event=stop_event,
                **(dict(prepare_capacity_frames=1,prepare_byte_capacity=4*752*480) if pipeline_r30 else {}))
        with (nullcontext(None) if pipeline_r30 else (output/'frames_trace.jsonl').open('x',encoding='utf-8')) as log:
            for saved in source.frames[:limit]:
                if stop_event is not None and stop_event.is_set():raise RuntimeError('resource stop requested')
                index=saved['frame_id'];entry=dict(frame_id=index,stages={})
                if pipeline_r30:
                    cap=capacity.entries[index] if capacity is not None else None
                    reserve=(752*480+cap['primary_bound']+cap['pfo3_bound']+cap['qualified_bound']*768+192*1024
                             if cap is not None else 2*1024**2)
                    _,reserve_timing=call_stage('persistence_capacity_reservation',writer.reserve_frame,reserve,timeout_s=30.,
                        cancelled=stop_event.is_set if stop_event is not None else None)
                    entry['stages']['persistence_capacity_reservation']=reserve_timing
                if acquisition is not None:
                    wait_started=time.perf_counter();produced=acquisition.take()
                    unpersisted=(produced.index,produced.raw,produced.final,produced.primary,produced.receipt)
                    if produced.index!=index:raise ValueError('acquisition/consumer frame order mismatch')
                    raw=produced.raw;timing=produced.timings['decode_hash'];add('decode_hash',timing)
                    entry['acquisition_wait_s']=time.perf_counter()-wait_started
                else:raw,timing=call_stage('decode_hash',source.read_frame,index)
                entry['stages']['decode_hash']=timing
                if type(raw) is not bytes or len(raw)!=752*480:raise ValueError('full immutable native source required')
                raw_sha=payload_sha256(raw)
                if raw_sha!=saved['raw_sha256']:raise ValueError('source Raw hash changed')
                bound=capacity.entries[index] if capacity is not None else None
                if bound is not None and raw_sha!=bound['raw_sha256']:raise ValueError('capacity Raw SHA mismatch')
                gray=np.frombuffer(raw,np.uint8).reshape(480,752)
                if acquisition is not None:
                    identity=produced.identity
                    frame,primary,receipt,admission=produced.final,produced.primary,produced.receipt,produced.admission
                    timing=produced.timings['actual_PL_transport'];add('actual_PL_transport',timing)
                else:
                    identity=replay_identity(index,epoch,test_only=test_only)
                    (frame,primary,receipt,admission),timing=call_stage('actual_PL_transport',transport.run,gray,identity=identity)
                entry['stages']['actual_PL_transport']=timing
                unpersisted=(index,raw,frame,primary,receipt)
                packet_digests,timing=call_stage('packet_provenance',_check_transport,frame,primary,receipt,admission,raw_sha,test_only=test_only)
                entry['stages']['packet_provenance']=timing
                unpersisted=(index,raw,frame,primary,receipt)
                if bound is not None:
                    actual=dict(diagnostic_bound=frame.header['diagnostic_count'],qualified_bound=frame.header['qualified_count'],
                        primary_bound=len(primary),pfo3_bound=len(frame.payload))
                    if any(actual[key]>bound[key] for key in actual):
                        raise BufferError('actual PL output exceeds frozen capacity table: '+str(actual))
                if frame.raw_required:
                    _save_rejected_input(output,index,raw,frame,primary,receipt,budget)
                    unpersisted=None
                    raise PFO3Error('explicit exceptional fallback required; preserved failed input, stop normal replay')
                source_ready_ns=(getattr(produced,'source_ready_monotonic_ns',0) if acquisition is not None else time.monotonic_ns())
                metadata=replay_metadata(saved,identity)
                if geometry_backend in ('paired','prepared_serial'):
                    pair,timing=call_stage('paired_camera_geometry',PreparedGeometryPairR25,
                        frame,measure_processor.calibration)
                    entry['stages']['paired_camera_geometry']=timing
                    pair.assert_binding(frame,preview_processor.calibration)
                    if geometry_backend=='paired':
                        if mode_executor is None:raise RuntimeError('paired mode executor was not created')
                        measured,preview,paired_timings=mode_executor.process_timed(
                            measure_processor,preview_processor,gray,metadata,pair=pair,
                            payload=frame.payload,admission=admission)
                        add('measurement_geometry',paired_timings['measurement'])
                        add('preview_geometry',paired_timings['preview'])
                        overlap=dict(wall_s=paired_timings['overlap_wall_s'],
                                     thread_cpu_s=float(paired_timings.get('overlap_thread_cpu_s',0.)),
                                     overlapping=True,additive=False)
                        entry['stages']['measurement_geometry']=paired_timings['measurement']
                        entry['stages']['preview_geometry']=paired_timings['preview']
                        entry['stages']['paired_modes_overlap']=overlap
                    else:
                        # One camera-geometry mapping, then both independent
                        # mode state machines.  Deliberately serial: the prior
                        # two-thread paired A/B was slower on the board, so this
                        # path optimises duplicated computation, not scheduling.
                        if mode_executor is not None:
                            raise RuntimeError('serial shared geometry must not own a mode pool')
                        _both=shared_core.process(gray,metadata,frame=frame,admission=admission,pair=pair)
                        measured,preview=_both.measurement,_both.preview
                        for label,key in (('measurement_geometry','measurement'),('preview_geometry','preview')):
                            timing=_both.timings[key];add(label,timing)
                            entry['stages'][label]=timing
                        entry['stages']['shared_geometry_serial']=dict(
                            wall_s=0.,thread_cpu_s=0.,overlapping=False,additive=False,
                            note='serial by design; one prepared camera mapping per frame')
                else:
                    if mode_executor is not None:
                        raise RuntimeError('separate geometry must not own a mode pool')
                    measured,timing=call_stage('measurement_geometry',measure_processor.process_final,
                        gray,metadata,payload=frame.payload,admission=admission,mode='measurement')
                    entry['stages']['measurement_geometry']=timing
                    preview,timing=call_stage('preview_geometry',preview_processor.process_final,
                        gray,metadata,payload=frame.payload,admission=admission,mode='preview')
                    entry['stages']['preview_geometry']=timing
                eligible=bool(frame.frame_usable('measurement') and measured.simulation_geometry_valid
                              and int(measured.valid_points.sum())>0)
                if eligible:producer.append(time.perf_counter()-started)
                measured.frame_metadata.update(replay_run_tag=tag,physical_accuracy_verified=False,
                    PL_transport_receipt=receipt,runner_evidence='MOCK_ONLY' if test_only else 'ACTUAL_PL_SAVED_SIMULATED_INPUT')
                summary=measured.summary();summary.update(r25_nonempty_measurement_XYZ=eligible,
                    measurement_frame_usable=frame.frame_usable('measurement'),
                    preview_frame_usable=frame.frame_usable('preview'),physical_accuracy_verified=False)
                if pipeline_r30:
                    packet=b'';timing=dict(wall_s=0.,thread_cpu_s=0.,deferred_to_persistence_worker=True)
                else:packet,timing=call_stage('profile_serialization',encode_profile,measured,summary)
                entry['stages']['profile_serialization']=timing
                camera_delta=measured.delta_camera_mm;axis_delta=measured.delta_axis_mm
                record=dict(request_id=index,capture_epoch=epoch,raw=raw,raw_sha256=raw_sha,
                    profile_payload=packet,source_pfo3_payload=frame.payload,source_pfo3_primary=primary,
                    points_camera_mm=camera_delta,points_axis_mm=axis_delta,summary=summary,
                    motion=dict(source='SIMULATION_ONLY_angle',angle_rad=float(saved['angle_rad']),motor_command=False),
                    exposure=dict(source='SIMULATED_IMAGE_ASSUMPTION',lines=60,revision=0))
                if not test_only:record['_pfo3_completion']=admission
                wire_bytes=len(raw)+len(packet)+len(frame.payload)+len(primary)+camera_delta.nbytes+axis_delta.nbytes
                # Include both spool and final export plus an entire trace/index
                # allowance, beyond the immutable record submitted to the writer.
                accounted_bytes=wire_bytes+2*axis_delta.nbytes+1024*1024
                if accounted_bytes>(bound['frame_bound_bytes'] if bound is not None else FRAME_BOUND):
                    raise BufferError('actual record exceeds pre-admitted frame capacity bound')
                if not pipeline_r30:
                    _,timing=call_stage('writer_admission',writer.submit_wait,record,timeout_s=5.)
                    accepted_measurement_points+=len(axis_delta);accepted_measurement_frames+=1
                    unpersisted=None
                    entry['stages']['writer_admission']=timing;admissions.append(time.perf_counter()-started)
                preview_delta=preview.delta_axis_mm if frame.frame_usable('preview') else np.empty((0,3),np.float64)
                if preview_stream is not None:
                    _blob=np.ascontiguousarray(preview_delta,dtype='<f8').tobytes()
                    preview_stream.write(_blob)
                    preview_index.append(dict(frame=index,offset=preview_offset,bytes=len(_blob),
                        count=int(len(preview_delta)),sha256=hashlib.sha256(_blob).hexdigest()))
                    preview_offset+=len(_blob);preview_points+=int(len(preview_delta))
                if test_only:
                    annotations,timing=call_stage('display_annotations',annotations_from_frame,frame,
                        actual_pl_execution=False,geometry_evidence_type='SIMULATION_ONLY')
                else:
                    annotations,timing=call_stage('display_annotations',DeferredPLAnnotations,frame,
                        actual_PL_execution=True,geometry_evidence_type='SIMULATION_ONLY')
                entry['stages']['display_annotations']=timing
                _,timing=call_stage('display_submit',publisher.submit if publisher is not None else display.submit,gray,preview_delta,index,float(saved['angle_rad']),
                    annotations=annotations,**(dict(source_capture_monotonic_ns=source_ready_ns) if not test_only and source_ready_ns>0 else {}))
                entry['source_ready_monotonic_ns']=source_ready_ns
                entry['source_age_basis']='REPLAY_RAW_READY_NOT_PHYSICAL_EXPOSURE'
                entry['stages']['display_submit']=timing
                if coordinator is not None:coordinator.note(index)
                entry.update(raw_sha256=raw_sha,primary_sha256=packet_digests.primary_sha256,
                    pfo3_sha256=packet_digests.final_sha256,record_bytes=wire_bytes,
                    valid_XYZ_points=int(measured.valid_points.sum()),preview_XYZ_points=len(preview_delta),
                    measurement_frame_usable=frame.frame_usable('measurement'),preview_frame_usable=frame.frame_usable('preview'),
                    actual_PL_execution=not test_only,capacity_accounted_bytes=accounted_bytes,
                    PL_processing_cycles=frame.header['processing_cycles_lo']+(frame.header['processing_cycles_hi']<<32),
                    **_image_guard.report(),
                    transport_receipt=receipt)
                if pipeline_r30:
                    record.update(_r30_profile=measured,_r30_preview=preview_delta,_r30_trace=entry,
                        _r30_frame_bound=bound['frame_bound_bytes'] if bound is not None else FRAME_BOUND)
                    _,timing=call_stage('writer_admission',writer.submit_wait,record,timeout_s=5.)
                    accepted_measurement_points+=len(axis_delta);accepted_measurement_frames+=1
                    unpersisted=None
                    entry['stages']['writer_admission']=timing;admissions.append(time.perf_counter()-started)
                    preview_points+=len(preview_delta)
                else:log.write(json.dumps(entry,allow_nan=False)+'\n')
                stages.append(entry)
                frame_count+=1;nonempty+=eligible
            boundaries['production_end_s']=time.perf_counter()-started
            if log is not None:log.flush();os.fsync(log.fileno())
        if publisher is not None:publisher_receipt=publisher.close(timeout_s=10.)
        if acquisition is not None:
            acquisition_receipt=acquisition.close()
            if acquisition_receipt['error']:raise RuntimeError('acquisition worker reported failure')
        # No producer or DMA master owns these buffers after confirmed close.
        # Retire CMA before readback/feature allocations, not after their peak.
        if not test_only and hasattr(transport,'release_idle_buffers'):
            retired,timing=call_stage('transport_retire_idle_buffers',transport.release_idle_buffers)
            boundaries['transport_buffer_retirement']=retired
        session.transition('draining')
        if coordinator is not None:coordinator.set_phase(3)
        if writer_drain is not None:
            writer_receipt,timing=call_stage('writer_drain',writer_drain.close,writer)
        else:writer_receipt,timing=call_stage('writer_drain',writer.close,timeout_s=10.)
        if not writer_receipt.worker_joined or not writer_receipt.durable or writer_receipt.written!=frame_count:
            raise RuntimeError('complete writer durability unconfirmed')
        if preview_stream is not None:
            preview_close_receipt=close_preview_evidence(preview_stream,preview_index,output,budget=budget)
            if not preview_close_receipt['complete']:
                raise RuntimeError('preview durability unconfirmed: '+str(preview_close_receipt['errors']))
        boundaries['writer_drain_end_s']=time.perf_counter()-started
        _,timing=call_stage('cloud_export',spool.export_npy,output/'cloud.npy')
        if pipeline_r30:
            preview_close_receipt=dict(writer.materialization,enabled=True,errors=[],binary_synced=True,index_synced=True,
                binary_closed=True,measurement_durability_confirmed=True)
        _,timing=call_stage('cloud_close',spool.close)
        if test_only:
            # Mock writers retain records in RAM and have no persisted payloads.
            # Their functional receipt must never satisfy real readback gates.
            readback=dict(verified=False,scope='MOCK_FUNCTIONAL_ONLY_NO_DISK_READBACK',
                          strict_iterator_exhausted=False)
        else:
            readback,timing=call_stage('strict_saved_readback',verify_replay_saved,output,
                expected_frames=frame_count,expect_preview=bool(persist_preview),
                expected_cloud_points=spool.count,expected_preview_points=preview_points)
        if coordinator is not None:
            coordinator.set_phase(6)
            coordinator_receipt=coordinator.close();coordinator=None
        _,timing=call_stage('pre_feature_display_drain',display.begin_target,tag+'/analysis',timeout_s=10.)
        # All measurement buffers have drained. Return retired NumPy/Python
        # allocations before forking the bounded final-analysis supervisor.
        import gc
        gc.collect()
        if platform.system()=='Linux':
            import ctypes
            try:ctypes.CDLL(None).malloc_trim(0)
            except (AttributeError,OSError):pass
        session.transition('features')
        feature_report=None;feature_seconds=None;association_audit=None;raw_feature_count=None
        feature_worker_mode=None;feature_execution=None
        if post_features:
            _t=time.perf_counter()
            import inspect
            from virtual_scan.features_v4 import FeaturePolicy,FeaturePolicyV5
            from virtual_scan.feature_task_r26 import discover_features_bounded as discover_features_with_audit
            _angles=list(measure_processor.scan_angles_rad)
            _cal=getattr(measure_processor,'calibration_id','simulation')
            _signature=inspect.signature(discover_features_with_audit)
            _feature_kwargs={}
            if 'workers' in _signature.parameters:
                _feature_kwargs['workers']=feature_workers
                feature_worker_mode=('serial' if feature_workers==1 else 'bounded_parallel')
            elif feature_workers!=1:
                raise ValueError('requested feature parallelism is not implemented by this build')
            if 'legacy' in _signature.parameters:
                # Production never runs the frozen legacy path because of an
                # inherited environment variable; legacy is an explicit A/B tool.
                _feature_kwargs['legacy']=False
            # Reserve the bounded transient result too. Only the child opens
            # the read-only point-cloud mapping; its object graph is gone
            # before the supervisor deserializes the final feature result.
            _feature_temporary_bound=65*1024**2
            _feature_ticket=budget.reserve(_feature_temporary_bound)
            import tempfile
            _feature_dir=Path(tempfile.mkdtemp(prefix='feature_result_',dir=output))
            try:
                feature_report,association_audit=discover_features_with_audit(
                    output/'cloud.npy',calibration_id=_cal,scan_angles_rad=_angles,
                    policy=None,version=feature_policy, result_directory=_feature_dir,
                    deadline_s=120., cancelled=(stop_event.is_set if stop_event is not None else None),
                    **_feature_kwargs)
            finally:
                # A timed-out deserializer may still own its spool. Never
                # recursively delete this tree; the supervisor reports that
                # retained path and the failed run keeps its byte reservation.
                if not any(_feature_dir.iterdir()):
                    _feature_dir.rmdir()
                    budget.commit(_feature_ticket,0)
            feature_seconds=time.perf_counter()-_t
            try:
                from virtual_scan.feature_parallel_r26 import last_execution
                feature_execution=last_execution()
            except BaseException:
                feature_execution=None
            raw_candidates=association_audit.get('original_features',[])
            raw_feature_count=len(raw_candidates)
            audit_payload={key:value for key,value in association_audit.items()
                           if key!='original_features'}
            save_json(output/'features.json',dict(features=feature_report,
                raw_features=raw_candidates,association_audit=audit_payload,
                calibration_id=_cal,evidence='SIMULATION_ONLY',
                physical_accuracy_verified=False,
                provenance='raw candidates retained; same-entity association uses support/position/axis/range'),budget)
            _dim=[f for f in feature_report if f.get('type') in
                  ('diameter','segment_height','plane_offset','parallel_plane_spacing')]
            _lines=['# feature / dimension report','',
                    'source: actual PL replay, SIMULATION_ONLY geometry; not physical metrology','',
                    'measurement points: %d'%spool.count, '']
            for f in _dim:
                v=f.get('value_mm')
                _lines.append('- %s = %s mm | region=%s | support=%d | residual=%s'%(
                    f.get('type'),'unmeasurable' if v is None else round(v,3),f.get('region_id'),
                    len(f.get('support',{}).get('point_indices',[])),f.get('residual_mm')))
            _unres=[f for f in feature_report if f.get('type')=='unresolved_component']
            _lines+=['', 'unresolved components: %d (no dimensions emitted)'%len(_unres)]
            (output/'FEATURES.md').write_text('\n'.join(_lines)+'\n',encoding='utf8')
        _,timing=call_stage('display_drain',display.begin_target,tag+'/completed',timeout_s=10.)
        elapsed=time.perf_counter()-started
        boundaries['full_end_s']=elapsed
        displayed=[row for row in list(display.records) if row['target']==tag]
        ack_rows=[row for row in displayed if valid_vdma_ack(row.get('receipt')) and not test_only]
        display_times=[row['ack_perf_s']-started for row in ack_rows]
        durable_times=sorted(at-started for _,at in trace.events)
        durable_complete=len(durable_times)==nonempty
        display_submitted_delta=int(display.submitted)-display_counts_before['submitted']
        display_acknowledged_delta=int(display.acknowledged)-display_counts_before['acknowledged']
        display_dropped_delta=int(display.dropped)-display_counts_before['dropped']
        result=dict(schema='R25_PL_REPLAY_V1',target=target,frames=frame_count,requested_frames=limit,
            display_submitted=display_submitted_delta,
            display_acknowledged=display_acknowledged_delta,
            display_dropped=display_dropped_delta,
            display_ack_rows=len(ack_rows),
            display_counts_before=display_counts_before,
            display_counts_after=dict(submitted=int(display.submitted),
                acknowledged=int(display.acknowledged),dropped=int(display.dropped)),
            valid_XYZ_profiles=nonempty,cloud_points=spool.count,actual_PL_frames=0 if test_only else frame_count,
            evidence_type='MOCK_ONLY_NO_HARDWARE' if test_only else 'ACTUAL_PL_REPLAY_SIMULATED_INPUT_GEOMETRY',
            geometry_evidence_type='SIMULATION_ONLY',physical_accuracy_verified=False,
            actual_PL_execution=not test_only,display_hardware_ACK=bool(display_times) and not test_only,
            **_image_guard.report(),
            all_raw_diagnostics_and_primary_retained=True,two_geometry_calls_per_frame=geometry_backend=='separate',
            geometry_backend=geometry_backend,
            camera_geometry_calls_per_frame=1 if geometry_backend in ('paired','prepared_serial') else 2,
            mode_execution=('serial_shared_geometry' if geometry_backend=='prepared_serial'
                            else 'two_thread_overlap' if geometry_backend=='paired' else 'two_serial_calls'),
            data_correct=writer_receipt.durable and writer_receipt.written==frame_count and durable_complete
                and (test_only or bool(readback and readback['verified'])),
            bounded_queues=(writer.peak_pending_items<=(128 if pipeline_r30 else DEFAULT_IO_POLICY.capacity) and writer.peak_pending_bytes<=DEFAULT_IO_POLICY.byte_capacity
                and (acquisition_receipt is None or acquisition_receipt['peak_pending']<=1
                     and acquisition_receipt['peak_bytes']<=32*1024**2)),
            acquisition=acquisition_receipt,parallel_PL_acquisition=parallel_pl,
            pipeline_r30_unified=bool(pipeline_r30),display_publisher=publisher_receipt,
            pipeline_status_coordinator=coordinator_receipt,
            unified_persistence=writer.pipeline_summary() if pipeline_r30 else None,
            writer_drain=writer_drain.snapshot() if writer_drain is not None else None,
            storage_evidence_type=('MOCK_ONLY' if test_only else
                'WRITER_SAME_FILE_PAYLOAD_AND_COMMIT_FSYNC' if storage_backend=='journal15c'
                else 'WRITER_PAYLOAD_AND_INDEX_FSYNC'),
            storage_backend=storage_backend,journal_buffer_bytes=journal_buffer_bytes,
            observation_columns_backend=(__import__('candidate.observation_columns_r28',fromlist=['identity']).identity()
                or {'backend':'NUMPY_REFERENCE'}),
            independent_readback=readback,preview_close_receipt=preview_close_receipt,
            mock_component_complete=bool(test_only),
            # Explicit functional scope of THIS entrypoint: geometry + persistence +
            # display only. Features, preview persistence, exposure and rescan are
            # not part of the DDR replay path and must not be implied by it.
            full_pipeline_entrypoint='pl_replay_r25.py',
            feature_discovery_included=bool(post_features),
            preview_XYZ_persisted=bool(persist_preview),
            preview_XYZ_points=preview_points,feature_count=(len(feature_report) if feature_report else 0),
            raw_feature_count=raw_feature_count,
            association_dropped_count=(association_audit.get('dropped_count')
                                       if association_audit else None),
            feature_discovery_s=feature_seconds,
            feature_workers=feature_workers,feature_policy=feature_policy,
            writer_validation_phase=writer_validation_phase,
            payload_preallocation=getattr(writer,'payload_preallocation',None),
            feature_worker_mode=feature_worker_mode,
            feature_parallel_execution=feature_execution,
            exposure_loop_exercised=False,
            rescan_exercised=False,
            validation_backend=(getattr(transport,'body_validator',None).__class__.__name__
                                if getattr(transport,'body_validator',None) is not None else 'STRICT_PYTHON'),
            writer=asdict(writer_receipt),
            persistence_timeline=getattr(writer,'persistence_timeline',None),
            persistence_timeline_summary=getattr(writer,'persistence_timeline_summary',None),
            persistence_io_accounting=getattr(writer,'io_accounting',None),
            phases=phases,stage_frames=stages,boundaries=boundaries,
            writer_policy=dict(capacity=128 if pipeline_r30 else DEFAULT_IO_POLICY.capacity,byte_capacity=DEFAULT_IO_POLICY.byte_capacity,
                group_size=writer_group_size,max_delay_s=writer_max_delay_s),
            peak_pending_items=writer.peak_pending_items,peak_pending_bytes=writer.peak_pending_bytes,
            # Per-batch sink timings (payload fsync / index fsync / validation).
            # Additive measurement only: it lets a drain stall be attributed
            # instead of guessed, and does not change what is written.
            writer_accounting=getattr(writer,'io_accounting',None),
            producer_times_s=producer,durable_times_s=durable_times,admission_times_s=admissions,display_times_s=display_times,
            activity_start_perf_s=started,activity_end_perf_s=started+boundaries['production_end_s'],
            active_duration_s=boundaries['production_end_s'],duration_s=elapsed,
            cold_start_s=started-invoked,total_invocation_duration_s=time.perf_counter()-invoked,
            process_cpu_s=time.process_time()-cpu_started,CPU_total_capacity_pct=None,mem_available_min_bytes=None,
            timing_scope='source decode/hash + actual SG/PL (overlapped worker if enabled) + measurement/preview geometry + source/MBR encoding + writer admission + display; overlapping stage wall times are not additive; full tail includes worker/writer drain/export/spool/display close',
            capacity_table_sha256=capacity.table_sha256 if capacity is not None else None,
            capacity_gate='FROZEN_INPUT_SHA_BOUND_TABLE' if capacity is not None else '32MiB_PER_FRAME_SHORT_ONLY',
            capture_timing_basis='SOFTWARE_REPLAY_TAGS_NOT_PHYSICAL_FV_OR_MOTOR_COUNTERS')
        # A paired executor close failure must invalidate the run before
        # RESULT.json is published, not merely be mentioned in CLEANUP.
        mode_executor_close_error=None
        if mode_executor is not None:
            try:
                mode_executor.close()
            except BaseException as exc:
                mode_executor_close_error=type(exc).__name__+': '+str(exc)
                raise RuntimeError('paired executor close failed: '+mode_executor_close_error) from exc
            finally:
                mode_executor=None
            result['paired_executor_closed']=True
        else:
            result['paired_executor_closed']=None
        result['mode_executor_close_error']=mode_executor_close_error
        session.confirm_completion(persistence_durable=bool(result['data_correct']),
            workers_joined=bool(writer_receipt.worker_joined and
                (acquisition_receipt is None or acquisition_receipt.get('worker_joined',False))),
            features_complete=bool(not post_features or feature_report is not None),
            readback_verified=bool(readback and readback['verified']))
        result['session']=session.snapshot()
        result['session_completion_receipt']='CLEANUP.json'
        save_json(output/'RESULT.json',result,budget)
        if not test_only:session.finish()
        completed=True
        return result
    finally:
        original_run_error=sys.exc_info()[1]
        coordinator_error=None
        if coordinator is not None:
            try:
                coordinator.set_phase(5)
                coordinator_receipt=coordinator.close();coordinator=None
            except BaseException as exc:
                # A status-only worker failure must not skip measurement,
                # display and journal cleanup or replace the original error.
                coordinator_error=type(exc).__name__+': '+str(exc)
        if not completed and session.state not in ('failed','cancelled','completed'):
            error=sys.exc_info()[1]
            if isinstance(error,KeyboardInterrupt):session.cancel()
            else:session.fail(type(error).__name__+': '+str(error))
        acquisition_error=None;acquisition_joined=acquisition is None
        if acquisition is not None:
            try:acquisition_receipt=acquisition.close();acquisition_joined=acquisition_receipt['worker_joined']
            except BaseException as exc:acquisition_error=type(exc).__name__+': '+str(exc)
        publisher_error=None
        if publisher is not None:
            try:publisher_receipt=publisher.close(timeout_s=10.)
            except BaseException as exc:publisher_error=type(exc).__name__+': '+str(exc)
        if pipeline_r30 and writer is not None:writer.cancel_reservation()
        mode_executor_error=None
        if mode_executor is not None:
            try:mode_executor.close()
            except BaseException as exc:mode_executor_error=type(exc).__name__+': '+str(exc)
        clean=None;cleanup_error=None
        if writer is not None:
            try:
                clean=writer_drain.close(writer) if writer_drain is not None else writer.close(timeout_s=10.)
            except BaseException as exc:clean=getattr(exc,'receipt',None);cleanup_error=type(exc).__name__+': '+str(exc)
        spool_error=None
        if spool is not None:
            try:spool.close()
            except BaseException as exc:spool_error=type(exc).__name__+': '+str(exc)
        source_save_error=None
        if unpersisted is not None:
            try:_save_rejected_input(output,*unpersisted,budget)
            except BaseException as exc:source_save_error=type(exc).__name__+': '+str(exc)
        preview_partial_receipt=None
        if not completed and preview_stream is not None:
            preview_partial_receipt=close_preview_evidence(preview_stream,preview_index,output,
                partial=True,budget=budget)
        cleanup_errors={key:value for key,value in dict(acquisition=acquisition_error,status_coordinator=coordinator_error,
            mode_executor=mode_executor_error,display_publisher=publisher_error,writer=cleanup_error,
            writer_receipt=getattr(clean,'error',None),spool=spool_error,
            unadmitted_source_save=source_save_error).items() if value is not None}
        for key,receipt in (('preview_close',preview_close_receipt),('preview_partial',preview_partial_receipt)):
            if receipt and receipt.get('errors'):cleanup_errors[key]=receipt['errors']
        from candidate.storage_failure15 import cleanup_writer_telemetry
        writer_telemetry=cleanup_writer_telemetry(writer,clean)
        from candidate.spool_authority15c import spool_authority
        spool_state=spool_authority(spool_points=int(getattr(spool,'count',0)),
            accepted_points=accepted_measurement_points,accepted_frames=accepted_measurement_frames,
            receipt=clean,completed=completed)
        cleanup=dict(session=session.snapshot(),completed=bool(completed and not test_only),writer_worker_joined=writer is None or bool(getattr(clean,'worker_joined',False)),
            completion_scope='MOCK_FUNCTIONAL_ONLY' if test_only else 'STRICT_SAVED_READBACK',
            mock_component_complete=bool(completed and test_only),
            independent_readback=readback,preview_close_receipt=preview_close_receipt,
            preview_partial_receipt=preview_partial_receipt,
            cleanup_errors=cleanup_errors,cleanup_ok=not bool(cleanup_errors),
            acquisition_worker_joined=acquisition_joined,acquisition_error=acquisition_error,
            mode_executor_error=mode_executor_error,
            writer_worker_unconfirmed=writer is not None and bool(getattr(clean,'worker_unconfirmed',True)),
            writer_durable=writer is None or bool(getattr(clean,'durable',False)),
            writer_accepted=getattr(clean,'accepted',0),writer_written=getattr(clean,'written',0),
            writer_drain=writer_drain.snapshot() if writer_drain is not None else None,
            writer_telemetry=writer_telemetry,
            storage_backend=storage_backend,measurement_spool_authority=spool_state,
            error=cleanup_error,spool_error=spool_error,unadmitted_source_save_error=source_save_error,
            **_image_guard.report())
        from candidate.storage_failure15 import save_cleanup_preserving_error
        save_cleanup_preserving_error(lambda:save_json(output/'CLEANUP.json',cleanup,budget),
            cleanup,original_run_error)


def capacity_admission(output,frame_count,*,data_root=None,capacities=None,repeats=1,temporary_bytes=0):
    from candidate.formal_resources_r22 import managed_data_bytes
    from resource_policy import StorageBudget,capacity_plan,BudgetExceeded
    output=Path(output).resolve();data_root=Path(data_root or output.parent).resolve()
    if data_root not in output.parents:raise ValueError('output must be beneath managed data root')
    existing=data_root
    while not existing.exists():existing=existing.parent
    used=managed_data_bytes(data_root);free=lambda:int(shutil.disk_usage(existing).free)
    if capacities is None:
        bound=frame_count*FRAME_BOUND;basis='32MiB_PER_FRAME_SHORT_ONLY'
    else:
        if type(repeats) is not int or not 1<=repeats<=3:raise ValueError('bounded repeat count required')
        if any(not isinstance(row,FrozenInputCapacity) or row.table_sha256!=CAPACITY_SHA256 for row in capacities.values()):
            raise ValueError('frozen capacity table admission required')
        if sum(len(row.entries) for row in capacities.values())*repeats!=frame_count:
            raise ValueError('capacity frame count mismatch')
        bound=sum(sum(item['frame_bound_bytes'] for item in row.entries) for row in capacities.values())*repeats
        basis='FROZEN_INPUT_SHA_BOUND_TABLE'
    if type(temporary_bytes) is not int or temporary_bytes<0:
        raise ValueError('nonnegative temporary byte bound required')
    plan=capacity_plan(1,bound+temporary_bytes,existing_data_bytes=used,filesystem_free_bytes=free())
    if not plan['admitted']:raise BudgetExceeded('capacity preflight failed: '+str(plan['reasons']))
    return StorageBudget(used,free),dict(plan,frame_count=frame_count,aggregate_frame_bound_bytes=bound,
        frame_bound_bytes=FRAME_BOUND if capacities is None else None,basis=basis,
        capacity_table_sha256=CAPACITY_SHA256 if capacities is not None else None,data_root=str(data_root),
        temporary_feature_bound_bytes=temporary_bytes)


def parser():
    value=argparse.ArgumentParser(description=__doc__)
    value.add_argument('--manifest',type=Path,required=True);value.add_argument('--output',type=Path,required=True)
    value.add_argument('--input-root',type=Path,default=ROOT.parent/'inputs')
    value.add_argument('--data-root',type=Path);value.add_argument('--raster-manifest',type=Path)
    value.add_argument('--pfo3-validator-manifest',type=Path)
    value.add_argument('--display-backend',choices=('thread','process'),default='thread')
    value.add_argument('--display-min-interval-ms',type=float,default=0.0,
        help='0=render every delivered image; >0=bounded latest-image cadence (display-only)')
    value.add_argument('--display-native-finite-validation-only',action='store_true')
    value.add_argument('--display-release-storage-on-close',action='store_true')
    value.add_argument('--display-sample-cap',type=int,default=None,
        help='bounded model-panel sample cap; full XYZ remains in the shared cloud and disk')
    value.add_argument('--geometry-backend',choices=('separate','paired','prepared_serial'),default='separate',
        help='separate=two camera calls; paired=old two-thread overlap; '
             'prepared_serial=one shared camera mapping with serial mode state machines')
    value.add_argument('--limit',type=int,choices=range(1,241),default=1)
    value.add_argument('--repeats',type=int,choices=range(1,4),default=1)
    value.add_argument('--targets',nargs='+',choices=('T05','T06'),default=['T05','T06'])
    value.add_argument('--raw-codec',choices=('none','zlib1'),default='zlib1')
    # R26: the writer has always accepted 1..32 batches; this only exposes the
    # range so the durability-granularity trade-off can be measured. The default
    # and the production policy stay at group 8 / 50 ms.
    value.add_argument('--payload-preallocate-mib',type=int,choices=range(0,65),default=0,
        help='optional bounded Linux KEEP_SIZE allocation before processing; default off')
    value.add_argument('--storage-backend',choices=('chunk_v3','journal15c'),default='chunk_v3')
    value.add_argument('--journal-buffer-kib',type=int,choices=(0,512,2048),default=512,
        help='bounded journal RAM staging; layout and fsync policy unchanged')
    value.add_argument('--writer-group-size',type=int,choices=range(1,33),default=DEFAULT_IO_POLICY.group_size)
    value.add_argument('--writer-max-delay-ms',type=float,default=DEFAULT_IO_POLICY.max_delay_s*1000.,
                       help='bounded collection window; every group and final drain still fsync')
    value.add_argument('--writer-validation-phase',choices=('submit','worker'),default='submit',
        help='worker: strict snapshot validation before any batch write; queue acceptance is not durability')
    value.add_argument('--serial-pl',action='store_true',help='explicit diagnostic control; default overlaps PL acquisition and PS geometry')
    value.add_argument('--full-chain',action='store_true',
        help='persist preview-derived XYZ separately and run post-scan feature/dimension discovery')
    value.add_argument('--feature-workers',type=int,choices=(1,2),default=1,
        help='bounded post-scan slice workers; 1 preserves the frozen serial path')
    value.add_argument('--feature-policy',choices=('v4','v5','v6'),default='v6')
    value.add_argument('--release-config-json', default=None)
    return value


def main(argv=None):
    args=parser().parse_args(argv)
    if args.payload_preallocate_mib and (len(args.targets)!=1 or args.repeats!=1):
        raise ValueError('KEEP_SIZE experiment requires one target and one repeat per run')
    if len(set(args.targets))!=len(args.targets):raise ValueError('duplicate targets are not admitted')
    if not 10.<=args.writer_max_delay_ms<=500.:raise ValueError('writer delay must be 10..500 ms')
    if not 0.<=args.display_min_interval_ms<=1000.:raise ValueError('display cadence must be 0..1000 ms')
    if args.display_sample_cap is not None and not 1000<=args.display_sample_cap<=24000:
        raise ValueError('display sample cap must be 1000..24000 points')
    if platform.system()!='Linux' or platform.machine() not in ('armv7l','armv7'):
        raise RuntimeError('actual ARM Linux execution required; no connection made')
    if args.output.exists():raise FileExistsError(args.output)
    # Diagnostic only: opt-in sub-stage timing, off unless SCAN3D_PROFILE_DIR is set.
    _profiler=None
    if os.environ.get('SCAN3D_PROFILE_DIR'):
        from tools.stage_profile_r26 import install
        _profiler,_restore_profile=install()
    capacities=load_capacity_inputs(args.input_root,args.targets,args.limit)
    shared_display_capacity=display_capacity_plan(capacities)
    # Worst case one transaction per frame, including 25% budget margin.
    journal_overhead_bound=(260*len(args.targets)*args.limit*args.repeats
                            if args.storage_backend=='journal15c' else 0)
    budget,plan=capacity_admission(args.output,len(args.targets)*args.limit*args.repeats,data_root=args.data_root,
        capacities=capacities,repeats=args.repeats,
        temporary_bytes=(65*1024**2 if args.full_chain else 0)+journal_overhead_bound)
    plan['storage_backend']=args.storage_backend
    plan['journal_overhead_bound_bytes']=journal_overhead_bound
    from pynq import Overlay
    from candidate.pfo3_transport_r25 import ActualPLTransportR25,resolve_manifest
    from candidate.runtime_monitor_r22 import RuntimeMonitor,linux_sample
    from candidate.display_pfo3_r25 import renderer_factory
    from virtual_scan.display_r22 import SessionDisplay,SessionDisplayStartupError
    from structured_light.motion.lifecycle import HardwareProcessLock
    from tools.board_replay_r22 import ReplayOwner,writer_cleanup_confirmed,quarantine_owner,valid_vdma_ack
    from acceptance_r26 import evaluate_trace_r26,activity_cpu_metrics
    import cv2
    manifest=resolve_manifest(args.manifest);args.output.mkdir(parents=True)
    lock=HardwareProcessLock(label='r25_actual_pl_finite_replay_no_motion').acquire()
    stop=threading.Event();transport=display=monitor=None;started_runs=0;clean=False;results=[]
    started=time.perf_counter()
    try:
        monitor=RuntimeMonitor(args.output/'resources.jsonl',storage_budget=budget,scheduler_stop=stop)
        monitor.start()
        overlay=Overlay(manifest['bit_absolute'],download=False)
        body_validator=None
        if args.pfo3_validator_manifest:
            from candidate.pfo3_decode_native_r25 import NativeBodyValidatorR25
            body_validator=NativeBodyValidatorR25.from_manifest(args.pfo3_validator_manifest)
        transport=ActualPLTransportR25(overlay,manifest=manifest,hardware_lock=lock,body_validator=body_validator)
        save_json(args.output/'ADMISSION.json',dict(release_config=(json.loads(args.release_config_json) if args.release_config_json else None),capacity=plan,geometry_evidence_type='SIMULATION_ONLY',
            loaded_bit_sha256=manifest['files']['bit']['sha256'],hardware_attach_only=True,
            FPGA_loads=0,motor_commands=0,camera_commands=0,board_access=True,
            actual_PL_execution=False,scope='ADMISSION_ONLY_NOT_EXECUTION_PROOF'),budget)
        config=dict(frame_total=args.limit,manifest=str(args.manifest),owner_log=str(args.output/'HDMI_OWNER.log'),
            hardware_lock_fd=lock._handle.fileno(),warmup=True,
            display_source='ACTUAL PL / SAVED SIMULATED INPUT + GEOMETRY / NOT METROLOGY')
        config['display_native_finite_validation_only']=args.display_native_finite_validation_only
        config['display_release_storage_on_close']=args.display_release_storage_on_close
        if args.display_min_interval_ms>0.:
            config['display_min_interval_s']=args.display_min_interval_ms/1000.
        if args.display_sample_cap is not None:
            config['display_sample_cap']=args.display_sample_cap
        if args.raster_manifest:config['raster_manifest']=str(args.raster_manifest)
        cv2.setNumThreads(1)
        display_class=SessionDisplay;display_options={}
        if args.display_backend=='process':
            from candidate.display_process_r25 import ProcessSessionDisplayR25
            display_class=ProcessSessionDisplayR25;display_options=shared_display_capacity
        try:display=display_class(owner_factory=ReplayOwner,renderer_factory=renderer_factory,config=config,
            startup_timeout_s=65.,**display_options)
        except SessionDisplayStartupError as exc:display=exc.display;raise
        for repeat in range(args.repeats):
            for target_index,target in enumerate(args.targets):
                before=linux_sample();started_runs+=1
                run=replay_target_pl(target,args.output/f'{target}_{repeat+1:02d}',display,budget,transport,
                    input_root=args.input_root,limit=args.limit,epoch=repeat*len(args.targets)+target_index+1,
                    persist_preview=bool(getattr(args,'full_chain',False)),
                    post_features=bool(getattr(args,'full_chain',False)),
                    raw_codec=args.raw_codec,writer_group_size=args.writer_group_size,
                    writer_max_delay_s=args.writer_max_delay_ms/1000.,stop_event=stop,
                    capacity=capacities[target],parallel_pl=not args.serial_pl,geometry_backend=args.geometry_backend,
                    feature_workers=args.feature_workers,feature_policy=args.feature_policy,writer_validation_phase=args.writer_validation_phase,
                    payload_preallocate_bytes=args.payload_preallocate_mib*1024**2,payload_run_bound_bytes=plan['budget_bytes'],
                    storage_backend=args.storage_backend,journal_buffer_bytes=args.journal_buffer_kib*1024)
                after=linux_sample();ticks=after['cpu_total_ticks']-before['cpu_total_ticks']
                run['CPU_total_capacity_pct']=None if ticks<=0 else 100*(1-(after['cpu_idle_ticks']-before['cpu_idle_ticks'])/ticks)
                run['activity_resource_metrics']=activity_cpu_metrics(
                    monitor.interval_samples(run['activity_start_perf_s'],run['activity_end_perf_s']),
                    run['activity_start_perf_s'],run['activity_end_perf_s'])
                run['activity_CPU_total_capacity_pct']=run['activity_resource_metrics']['CPU_total_capacity_pct']
                run['activity_IOwait_pct']=run['activity_resource_metrics']['IOwait_pct']
                run['mem_available_min_bytes']=monitor.minimum_available
                run['display_backend']=args.display_backend
                run['display_min_interval_s']=args.display_min_interval_ms/1000.
                run['display_sample_cap']=args.display_sample_cap
                run['display_shared_capacity']=shared_display_capacity if args.display_backend=='process' else None
                display_tag=f'{target}/{target}_{repeat+1:02d}'
                display_rows=[row for row in list(display.records)
                              if row.get('target')==display_tag
                              and valid_vdma_ack(row.get('receipt'))]
                run['performance_acceptance']=evaluate_trace_r26(run,display_records=display_rows)
                run['performance_verified']=bool(run['actual_PL_execution'] and run['display_hardware_ACK'] and
                    run['performance_acceptance']['observed_goals_passed'])
                save_json(args.output/f'{target}_{repeat+1:02d}'/'BOARD_RESULT.json',run,budget);results.append(run)
        display_receipt=display.close(timeout_s=30.);resource=monitor.close();transport.close()
        clean=display_receipt['owner_closed'] and display_receipt['worker_joined'] and resource['worker_joined']
        operational=bool(clean and not resource['error'] and not resource['stop_reason'] and
            all(run['frames']==args.limit and run['valid_XYZ_profiles']>0 and run['data_correct'] and run['actual_PL_execution'] for run in results))
        profile_report=None
        if _profiler is not None:
            try:
                profile_report=_profiler.report()
                try:
                    from candidate.pfo3_transport_r25 import TRANSPORT_STAGES
                    profile_report['transport']={k:dict(seconds=v[0],n=v[1])
                                                 for k,v in TRANSPORT_STAGES.items()}
                    from candidate.pfo3_stream_r25 import ASSEMBLE_STAGES
                    profile_report['assemble']={k:dict(seconds=v[0],n=v[1])
                                                for k,v in ASSEMBLE_STAGES.items()}
                except BaseException:
                    pass
                save_json(args.output/'PROFILE_STAGES.json',profile_report,budget)
            except BaseException as exc:
                profile_report={'error':type(exc).__name__+': '+str(exc)}
        save_json(args.output/'DONE.json',dict(operational_passed=operational,profile_stages=profile_report,
            performance_verified=operational and all(run['performance_verified'] for run in results),
            acceptance_scope='FINITE_FROZEN_INPUT_PL_REPLAY_SIMULATED_GEOMETRY',runs=results,display=display_receipt,
            resource=resource,total_wall_s=time.perf_counter()-started,physical_accuracy_verified=False,
            geometry_evidence_type='SIMULATION_ONLY',FPGA_loads=0,motor_commands=0,camera_commands=0),budget)
        return 0 if operational else 1
    except BaseException as exc:
        try:save_json(args.output/'ERROR.json',dict(error=type(exc).__name__+': '+str(exc),
            diagnostics=getattr(exc,'diagnostics',{}),
            partial_evidence_preserved=True,physical_accuracy_verified=False,FPGA_loads=0,motor_commands=0,camera_commands=0),budget)
        except BaseException:pass
        raise
    finally:
        transport_clean=transport is None;display_clean=display is None;monitor_clean=monitor is None
        if transport is not None:
            try:transport.close();transport_clean=transport.closed and not transport.active
            except BaseException:transport_clean=False
        if display is not None:
            try:receipt=display.close(timeout_s=30.);display_clean=receipt['owner_closed'] and receipt['worker_joined']
            except BaseException as close_error:
                # A failed rendering run remains failed. A separate explicit
                # owner-exit/worker-join receipt may nevertheless prove that
                # retaining the physical lock is no longer necessary.
                from virtual_scan.display_r22 import SessionDisplayCloseError
                from candidate.display_process_r25 import ProcessDisplayCloseError
                receipt=getattr(close_error,'receipt',None)
                display_clean=bool(isinstance(close_error,(SessionDisplayCloseError,ProcessDisplayCloseError))
                    and isinstance(receipt,dict) and receipt.get('cleanup_confirmed') is True
                    and receipt.get('owner_closed') is True and receipt.get('worker_joined') is True)
                if receipt is not None:
                    try:save_json(args.output/'DISPLAY_FAILED_CLOSE.json',dict(receipt,run_failed=True),budget)
                    except BaseException:display_clean=False
        if monitor is not None:
            try:monitor_clean=monitor.close()['worker_joined']
            except BaseException:monitor_clean=False
        if transport_clean and display_clean and monitor_clean and writer_cleanup_confirmed(args.output,started_runs):
            lock.release()
        else:quarantine_owner(args.output)


if __name__=='__main__':raise SystemExit(main())
