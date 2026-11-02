"""Bounded, one-clock complete-chain observations; never a hardware simulator."""
from array import array
import json
import math
import os
from pathlib import Path
import threading
import time
import struct

from acceptance_r22 import evaluate_trace,minimum_window_rate

SOURCE_NAMES=('unknown','PL_FINAL_V3','PS_V3_EXPLICIT_FALLBACK','legacy_explicit','software_fixture','MOCK_ONLY_PS_V3_EXPLICIT_FALLBACK')
OBSERVED,ELIGIBLE,DURABLE,CPU_EVENT,CPU_CALLED,CPU_TIME_KNOWN,INVALID=1,2,4,8,16,32,64


class FormalTrace:
    """Up to 32768 unique frames with bounded timestamps and typed source codes.

    A bounded identity map deduplicates completion callbacks. A missing,
    truncated or failed trace cannot certify performance. Activity is the
    actual capture scheduling/motor task interval, never processing drain.
    """
    def __init__(self, *, evidence_type, run_started=None, clock=time.perf_counter,
                 max_frames=32768, scheduler_stop=None):
        if evidence_type not in ('REAL_BOARD_COMPLETE_CHAIN','BOARD_REPLAY','HOST_REPLAY','SIMULATION'):
            raise ValueError('explicit evidence type required')
        if type(max_frames) is not int or not 1 <= max_frames <= 32768:
            raise ValueError('trace frame capacity must be 1..32768')
        self.clock=clock;self.run_started=clock() if run_started is None else run_started
        self.evidence_type=evidence_type;self.max_frames=max_frames;self.stop=scheduler_stop
        self._lock=threading.RLock();self._ids={}
        self._times={key:array('d') for key in ('producer_times_s','durable_times_s','display_times_s')}
        self._keys={key:array('Q') for key in self._times}
        self._source_index={};self._sources=array('B');self._durable_sources=array('B');self._cpu_sources=array('B')
        self._frame_flags=array('B');self._cpu_wall=array('d');self._cpu_states=[]
        self.activity_start=self.activity_end=None;self._active_tasks=0
        self.errors=[];self.closed=False

    def _fail(self, reason):
        if len(self.errors)<8:self.errors.append(reason)
        if self.stop is not None:self.stop.set()

    def activity_begin(self):
        with self._lock:
            if self.closed:return self._fail('activity after finalization')
            now=self.clock()
            if self.activity_start is None:self.activity_start=now
            self._active_tasks+=1

    def activity_finish(self):
        with self._lock:
            if self._active_tasks<=0:return self._fail('activity finish without begin')
            self.activity_end=self.clock();self._active_tasks-=1

    @staticmethod
    def eligible(summary, point_count):
        return (summary.get('valid') is True and summary.get('fused') is True
                and type(point_count) is int and point_count>0
                and summary.get('reason')!='before_registered_motion_window')

    @staticmethod
    def _source(metadata):
        if not isinstance(metadata,dict):return 0
        backend=metadata.get('processing_backend');success=metadata.get('PL_success')
        if backend=='PL_FINAL_V3' and success is True:return 1
        if backend=='PS_V3_EXPLICIT_FALLBACK' and success is False:return 2
        if backend=='MOCK_ONLY_PS_V3_EXPLICIT_FALLBACK' and success is False:return 5
        if backend=='SOFTWARE_PFO3_FIXTURE_ONLY':return 4
        if backend in ('PS_IMAGE_MULTIRUN_V1_NOT_DEPLOYED_PL','PS_IMAGE_MULTIRUN_V1','PMC2_PS_LINKED'):
            return 3
        return 0

    def _row(self,identity):
        identity=tuple(identity)
        if len(identity)!=2 or any(type(value) is not int or not 0<=value<2**64 for value in identity):
            self._fail('invalid source frame identity');return None
        if identity not in self._source_index:
            if len(self._source_index)>=self.max_frames:self._fail('source trace frame capacity reached');return None
            self._source_index[identity]=len(self._sources)
            self._sources.append(0);self._durable_sources.append(0);self._cpu_sources.append(0);self._frame_flags.append(0)
            self._cpu_wall.append(0.);self._cpu_states.append(None)
        return self._source_index[identity]

    @staticmethod
    def _record_metadata(record):
        # Writer has already validated the immutable MBR1 record. Parse only
        # its bounded JSON metadata, never observations or Raw pixels.
        payload=record.get('profile_payload')
        if type(payload) is not bytes or payload[:4]!=b'MBR1' or len(payload)<8:return None
        length=struct.unpack_from('<I',payload,4)[0]
        if length>1024*1024 or length>len(payload)-8:raise ValueError('bounded MBR trace metadata required')
        return json.loads(payload[8:8+length])['metadata'].get('frame_metadata')

    def _cpu_event(self,outcome):
        if not isinstance(outcome,dict):return self._fail('invalid CPU takeover event')
        provenance=outcome.get('cpu_provenance',{})
        if not isinstance(provenance,dict) or not isinstance(provenance.get('identity'),dict):return self._fail('invalid CPU event provenance')
        expected=provenance['identity']
        if outcome.get('PL_success') is not False or outcome.get('backend')!='PS_V3_EXPLICIT_FALLBACK':
            return self._fail('CPU takeover event claims hardware success')
        source=provenance.get('CPU_result_source')
        code=5 if source=='MOCK_ONLY_ARM_V3' else 2 if source=='ARM_V3_EXPLICIT_FALLBACK' else 0
        if not code:return self._fail('CPU takeover event source missing')
        index=self._row((expected.get('epoch'),expected.get('frame_id')))
        if index is None:return
        if self._frame_flags[index]&CPU_EVENT:return self._fail('duplicate CPU takeover event')
        if self._sources[index] not in (0,code):return self._fail('CPU event/producer source mismatch')
        self._cpu_sources[index]=code;self._frame_flags[index]|=CPU_EVENT
        before=provenance.get('cpu_calls_before');after=provenance.get('cpu_calls_after')
        if type(before) is not int or type(after) is not int or after-before not in (0,1):
            return self._fail('invalid CPU takeover call count')
        if after>before:self._frame_flags[index]|=CPU_CALLED
        wall=provenance.get('CPU_wall_s')
        if wall is not None:
            if isinstance(wall,bool) or not isinstance(wall,(int,float)) or not math.isfinite(wall) or wall<0:
                return self._fail('invalid CPU takeover elapsed time')
            self._cpu_wall[index]=wall;self._frame_flags[index]|=CPU_TIME_KNOWN
        state=outcome.get('state')
        if state not in ('CPU_V3_READY','CPU_REJECTED_K16_OVERFLOW','CPU_REJECTED_IMAGE_QUALITY',
                         'CPU_DEGRADED_STOP','FATAL_SOURCE_FAILURE','CPU_SUPPORT_DOMAIN_REJECTED','CPU_RESOURCE_DOMAIN_REJECTED'):
            return self._fail('unknown CPU outcome state')
        self._cpu_states[index]=state

    def _event(self, key, identity, bit, *, at=None):
        with self._lock:
            if self.closed:return self._fail('event after finalization')
            now=self.clock() if at is None else at
            if not math.isfinite(now) or now<self.run_started:return self._fail('invalid event time')
            identity=tuple(identity)
            if len(identity)!=2 or any(type(v) is not int or not 0<=v<2**64 for v in identity):
                return self._fail('invalid frame identity')
            if identity not in self._ids:
                if len(self._ids)>=self.max_frames:return self._fail('trace frame capacity reached')
                self._ids[identity]=0
            state=self._ids[identity]
            if state & bit:return self._fail('duplicate '+key)
            if bit!=1 and not state & 1:return self._fail('completion without eligible XYZ')
            times=self._times[key]
            if times and now<times[-1]:return self._fail('nonmonotonic '+key)
            self._ids[identity]=state|bit;times.append(now);self._keys[key].extend(identity)

    def produced(self, capture, result):
        points=getattr(result,'delta_camera_mm',None)
        count=(len(points) if points is not None else int(result.valid_points.sum()))
        summary=result.summary();eligible=self.eligible(summary,count);identity=(capture.epoch,capture.request_id)
        with self._lock:
            if self.closed:return self._fail('source event after finalization')
            index=self._row(identity)
            if index is None:return
            if self._frame_flags[index]&OBSERVED:return self._fail('duplicate source producer')
            code=self._source(getattr(result,'frame_metadata',None))
            if self._cpu_sources[index] not in (0,code):return self._fail('producer/CPU event source mismatch')
            self._sources[index]=code;self._frame_flags[index]|=OBSERVED|(ELIGIBLE if eligible else 0)
            if summary.get('valid') is not True or count==0:self._frame_flags[index]|=INVALID
            if eligible:self._event('producer_times_s',identity,1)

    def durable_group(self, group, at):
        # Called only after payload AND frame index/commit fsync succeed.
        for item in group:
            if item.kind=='event':
                if item.payload.get('event')=='PFO3_explicit_V3_takeover':
                    with self._lock:
                        if self.closed:self._fail('CPU event after finalization')
                        else:self._cpu_event(item.payload.get('outcome'))
                continue
            if item.kind!='frame':continue
            record=item.payload;points=record.get('points_camera_mm')
            with self._lock:
                if self.closed:self._fail('source durable after finalization');continue
                index=self._row((record['capture_epoch'],record['request_id']))
                if index is None:continue
                if self._frame_flags[index]&DURABLE:self._fail('duplicate source durable');continue
                try:code=self._source(self._record_metadata(record))
                except (KeyError,ValueError,TypeError) as exc:self._fail('invalid durable source metadata: '+str(exc));code=0
                self._durable_sources[index]=code;self._frame_flags[index]|=DURABLE
                if code and self._sources[index] and code!=self._sources[index]:self._fail('producer/durable source mismatch')
            if self.eligible(record['summary'],0 if points is None else len(points)):
                self._event('durable_times_s',(record['capture_epoch'],record['request_id']),2,at=at)

    def displayed(self, receipt, *, hardware_confirmed=False):
        sink=receipt.get('sink_receipt') or {}
        vdma=(isinstance(sink,dict) and sink.get('hdmi_acknowledged') is True
              and sink.get('acknowledgement_basis')=='VDMA frame event and active park store; not optical proof'
              and type(sink.get('active_store')) is int and 0<=sink['active_store']<3
              and type(sink.get('update_count')) is int and sink['update_count']>0
              and sink.get('fake_owner') is not True)
        if (hardware_confirmed is True and receipt.get('hdmi_acknowledged') is True
                and vdma
                and receipt.get('original_valid') is True and receipt.get('original_fused') is True
                and receipt.get('points',0)>0):
            self._event('display_times_s',(receipt['capture_epoch'],receipt['request_id']),4,
                        at=receipt['completed_monotonic_s'])

    def _source_summary(self,trace):
        counts={name:0 for name in SOURCE_NAMES};durable={name:0 for name in SOURCE_NAMES}
        processed=invalid=non_effective=unprocessed_exceptions=0
        def cpu_summary(code):
            indices=[i for i,flags in enumerate(self._frame_flags) if flags&CPU_EVENT and self._cpu_sources[i]==code]
            measured=[self._cpu_wall[i] for i in indices if self._frame_flags[i]&CPU_TIME_KNOWN]
            states={}
            for i in indices:
                state=self._cpu_states[i] or 'invalid_event';states[state]=states.get(state,0)+1
            return dict(attempts=len(indices),worker_calls=sum(bool(self._frame_flags[i]&CPU_CALLED) for i in indices),
                worker_wall_samples=len(measured),worker_wall_s_known_total=sum(measured),
                worker_wall_s_known_max=max(measured) if measured else None,
                worker_wall_s_known_mean=sum(measured)/len(measured) if measured else None,
                worker_calls_without_elapsed=sum(bool(self._frame_flags[i]&CPU_CALLED and not self._frame_flags[i]&CPU_TIME_KNOWN) for i in indices),
                outcomes=states,timing_scope='coordinator CPU worker elapsed when supplied; missing/timeout time is unknown, never assumed500ms')
        for i,flags in enumerate(self._frame_flags):
            if flags&OBSERVED:
                processed+=1;counts[SOURCE_NAMES[self._sources[i]]]+=1
                invalid+=bool(flags&INVALID);non_effective+=not bool(flags&ELIGIBLE)
            if flags&DURABLE:durable[SOURCE_NAMES[self._durable_sources[i]]]+=1
            if flags&CPU_EVENT and not flags&OBSERVED:unprocessed_exceptions+=1
        missing_cpu_events=sum(bool(flags&OBSERVED and self._sources[i] in (2,5) and not flags&CPU_EVENT)
            for i,flags in enumerate(self._frame_flags))
        summary=dict(processed_frames=processed,invalid_processed_frames=invalid,
            invalid_processed_frames_without_durable=sum(bool(flags&OBSERVED and flags&INVALID and not flags&DURABLE)
                for flags in self._frame_flags),
            exception_frames_without_result=unprocessed_exceptions,invalid_or_missing_result_frames=invalid+unprocessed_exceptions,
            non_effective_processed_frames=non_effective,producer_source_frames=counts,durable_source_frames=durable,
            CPU_takeover=cpu_summary(2),MOCK_ONLY_CPU_takeover=cpu_summary(5),
            CPU_producer_frames_without_takeover_event=missing_cpu_events,
            classification_complete=counts['unknown']==0 and missing_cpu_events==0 and all(not(flags&ELIGIBLE) or self._durable_sources[i]!=0
                for i,flags in enumerate(self._frame_flags)),
            policy='PL-only requires verified processing_backend=PL_FINAL_V3 and PL_success is True; CPU requires explicit CPU backend and False; missing never defaults to PL')
        groups={'PL_only':1,'CPU_takeover':2,'MOCK_ONLY_CPU_takeover':5}
        source_events={name:{key:[] for key in self._times} for name in groups}
        for key,events in ((key,trace[key]) for key in self._times):
            keys=trace['event_frame_keys_flat'][key]
            for j,at in enumerate(events):
                index=self._source_index.get((keys[2*j],keys[2*j+1]))
                if index is None:continue
                code=self._durable_sources[index] if key=='durable_times_s' else self._sources[index]
                for name,expected in groups.items():
                    if code==expected:source_events[name][key].append(at);break
        trace['source_events']=source_events;trace['source_statistics']=summary
        duration=trace['duration_s'];active=trace['active_duration_s'];total=trace['total_run_duration_s']
        source_rates={}
        for name,events in source_events.items():
            windows={key:dict(full=minimum_window_rate(values,duration),
                active=minimum_window_rate([at for at in values if at<=active],active)) for key,values in events.items()}
            source_rates[name]=dict(event_counts={key:len(values) for key,values in events.items()},windows=windows,
                after_activity_start_average_hz={key:len(values)/duration for key,values in events.items()},
                total_run_average_hz={key:len(values)/total for key,values in events.items()})
        return summary,source_rates

    def finalize(self, *, resource, data_correct, bounded_queues, feature_discovery_included):
        with self._lock:
            self.closed=True;finished=self.clock();start=self.activity_start;end=self.activity_end
            complete=bool(not self.errors and start is not None and end is not None
                          and self._active_tasks==0 and self.run_started<=start<end<=finished)
            all_durable=all(state & 2 for state in self._ids.values())
            all_durable=all_durable and all(not(flags&OBSERVED) or flags&DURABLE for flags in self._frame_flags)
            if not all_durable:complete=False
            # evaluate_trace uses an activity-relative origin. Cold startup is
            # separate; total average includes startup, drain, close and export.
            origin=start if start is not None else self.run_started
            duration=max(finished-origin,1e-9)
            active=end-origin if complete or (end is not None and end>origin) else 1e-9
            trace=dict(schema='R22_FORMAL_TRACE_V1',clock='time.perf_counter',
                duration_s=duration,active_duration_s=min(active,duration),
                total_run_duration_s=max(finished-self.run_started,1e-9),
                cold_start_s=None if start is None else start-self.run_started,
                activity_start_perf_s=start,activity_end_perf_s=end,run_start_perf_s=self.run_started,
                finished_perf_s=finished,trace_complete=complete,errors=list(self.errors),
                unique_frames=len(self._ids),max_frames=self.max_frames,
                trace_collection_memory_budget_bytes=20*1024*1024,
                trace_finalization_total_peak_budget_bytes=32*1024*1024,
                evidence_type=self.evidence_type,CPU_total_capacity_pct=resource.get('CPU_total_capacity_pct'),
                mem_available_min_bytes=resource.get('mem_available_min_bytes'),
                data_correct=bool(data_correct and complete and all_durable),
                bounded_queues=bounded_queues is True,
                feature_discovery_included=feature_discovery_included)
            trace['event_frame_keys_flat']={}
            trace['event_frame_key_layout']='pairs of [capture_epoch, request_id], aligned with each timestamp list'
            for key,times in self._times.items():
                trace[key]=[];keys=trace['event_frame_keys_flat'][key]=[]
                for i,at in enumerate(times):
                    if at>=origin:
                        trace[key].append(at-origin);keys.extend(self._keys[key][2*i:2*i+2])
            result=evaluate_trace(trace)
            sources,rates=self._source_summary(trace)
            result['source_statistics']=sources;result['source_rates']=rates
            pl_goals={key:bool(rates['PL_only']['windows'][key]['active']['available'] and
                rates['PL_only']['windows'][key]['active']['minimum_hz']>=10.) for key in ('producer_times_s','durable_times_s')}
            result['PL_only_producer_durable_goals']=pl_goals
            result['PL_only_display_goal']=bool(rates['PL_only']['windows']['display_times_s']['active']['available'] and
                rates['PL_only']['windows']['display_times_s']['active']['minimum_hz']>=5.)
            result['PL_processing_persistence_verified']=bool(self.evidence_type=='REAL_BOARD_COMPLETE_CHAIN' and complete
                and sources['classification_complete'] and all(pl_goals.values())
                and all(result['goals'][key] for key in ('cpu','memory','data_correct','bounded_queues')))
            result['PL_only_performance_verified']=bool(result['PL_processing_persistence_verified'] and result['PL_only_display_goal'])
            result['PL_only_verification_scope']='PL-only active5s producer/durable>=10Hz and confirmed HDMI>=5Hz plus complete/source/CPU/memory/data/bounded checks'
            result['rate_scope']='existing totals are effective valid/fused XYZ including qualified CPU recovery; PL-only is separately filtered by verified metadata'
            result['trace_complete']=complete
            result['goals']['trace_complete']=complete
            result['observed_goals_passed']=all(result['goals'].values())
            result['board_performance_verified']=bool(self.evidence_type=='REAL_BOARD_COMPLETE_CHAIN'
                                                      and result['observed_goals_passed'])
            result['total_run_duration_s']=trace['total_run_duration_s']
            result['total_run_average_hz']={key:len(trace[key])/trace['total_run_duration_s'] for key in self._times}
            result['after_activity_start_average_hz']=result['full_average_hz']
            result['full_average_hz']=dict(result['total_run_average_hz'])
            result['activity_interval_definition']='first actual motion/capture task start through last task completion; drains excluded'
            return trace,result


def finish_formal_trace(app, report, output):
    trace=getattr(app,'performance_trace',None)
    if trace is None:return
    captured,result=trace.finalize(resource=report.get('resource_monitor',{}),
        data_correct=report.get('passed') is True and report.get('writer',{}).get('durable') is True,
        bounded_queues=report.get('worker_unconfirmed') is False,
        feature_discovery_included=report.get('feature_discovery_included') is True)
    budget=getattr(app.writer,'storage_budget',None);token=None
    # Compact streaming JSON avoids a second entire string/bytes payload.
    # Allow the original float/identity pair and one source-filtered timestamp
    # per event, plus bounded source counters and CPU timing summaries.
    bound=112*sum(len(captured[key]) for key in trace._times)+131072
    try:
        if budget is not None:token=budget.reserve(bound)
        written=0
        with (Path(output)/'performance_trace.json').open('xb') as stream:
            for chunk in json.JSONEncoder(allow_nan=False,separators=(',',':')).iterencode(captured):
                data=chunk.encode('ascii')
                if written+len(data)+1>bound:raise BufferError('trace serialization bound exceeded')
                stream.write(data);written+=len(data)
            stream.write(b'\n');written+=1;stream.flush();os.fsync(stream.fileno())
        if token is not None:budget.commit(token,written)
    except BaseException as exc:
        result.update(trace_complete=False,observed_goals_passed=False,board_performance_verified=False,
                      PL_only_performance_verified=False,PL_processing_persistence_verified=False,
                      trace_save_error=type(exc).__name__+': '+str(exc))
    report['performance_acceptance']=result
    report['performance_source_statistics']=result.get('source_statistics',{})
    report['PL_only_performance_verified']=result.get('PL_only_performance_verified',False)
    report['PL_processing_persistence_verified']=result.get('PL_processing_persistence_verified',False)
    if app.source=='real_continuous_scan' and result['board_performance_verified'] is not True:
        report['functional_passed']=report.get('passed') is True
        report['passed']=False
