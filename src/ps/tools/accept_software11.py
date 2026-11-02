"""Read-only R26 performance audit and allocation-free capacity preflight.

No hardware loading, camera, motor, deletion, formatting or test execution.
Existing whole-run CPU averages and ACK fractions never certify active targets.
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from acceptance_r22 import minimum_window_rate
from resource_policy import capacity_plan
from tools.board_replay_r22 import valid_vdma_ack


def _finite(value, name, minimum=0.):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<minimum:
        raise ValueError('invalid '+name)
    return float(value)


def _goal(value, threshold, *, lower=False, reason=None):
    if value is None:
        return dict(status='UNMEASURED',value=None,threshold=threshold,reason=reason)
    return dict(status='PASS' if (value>=threshold if lower else value<=threshold) else 'FAIL',
                value=value,threshold=threshold)


def percentile(values, fraction):
    values=sorted(values)
    if not values:return None
    position=(len(values)-1)*fraction;lo=math.floor(position);hi=math.ceil(position)
    return values[lo]+(values[hi]-values[lo])*(position-lo)


def active_resources(samples, start, end):
    unavailable=dict(cpu=None,memory=None,scope='UNALIGNED_OR_INCOMPLETE_ACTIVE_TELEMETRY')
    if start is None or end is None or len(samples)<2:return unavailable
    start=_finite(start,'activity start');end=_finite(end,'activity end')
    if end<=start:raise ValueError('empty active interval')
    rows=[]
    for sample in samples:
        at=_finite(sample.get('perf_s'),'resource timestamp')
        total=sample.get('cpu_total_ticks');idle=sample.get('cpu_idle_ticks')
        mem=sample.get('MemAvailable_bytes')
        if any(type(v) is not int or v<0 for v in (total,idle,mem)) or idle>total:
            raise ValueError('invalid resource counters')
        rows.append((at,total,idle,mem))
    if any(b[0]<=a[0] or b[1]<a[1] or b[2]<a[2] for a,b in zip(rows,rows[1:])):
        raise ValueError('resource counter/time regression')
    if rows[0][0]>start or rows[-1][0]<end:return unavailable
    total=idle=0.;gaps=[]
    for a,b in zip(rows,rows[1:]):
        overlap=max(0.,min(b[0],end)-max(a[0],start))
        if not overlap:continue
        ratio=overlap/(b[0]-a[0]);dt=b[1]-a[1];di=b[2]-a[2]
        if di>dt:raise ValueError('resource idle ticks exceed total delta')
        total+=dt*ratio;idle+=di*ratio;gaps.append(b[0]-a[0])
    memory=[row[3] for row in rows if start<=row[0]<=end]
    # Counter interpolation at partial boundary intervals is a sampled estimate,
    # explicitly not the monitor's complete-invocation CPU average.
    return dict(cpu=100*(1-idle/total) if total>0 else None,
                memory=min(memory) if memory else None,
                max_sample_gap_s=max(gaps) if gaps else None,
                scope='ACTIVE_INTERVAL_OVERLAP_WEIGHTED_OS_TICKS')


def audit_run(result, *, resources=(), display_records=None, strict_readback=None):
    active=_finite(result.get('active_duration_s'),'active duration')
    if active<=0:raise ValueError('positive active interval required')
    windows={};goals={}
    absolute=result.get('activity_start_perf_s')
    for key,threshold in (('producer_times_s',10.),('durable_times_s',10.),('display_times_s',5.)):
        values=result.get(key)
        if key=='display_times_s' and display_records is not None and absolute is not None:
            values=[_finite(row.get('ack_perf_s'),'ACK timestamp')-absolute for row in display_records
                    if valid_vdma_ack(row.get('receipt'))]
            values.sort()
        if values is None:
            windows[key]=dict(available=False,reason='timestamp trace missing')
            goals[key]=_goal(None,threshold,lower=True,reason='timestamp trace missing')
            continue
        checked=[_finite(value,key) for value in values]
        if any(b<a for a,b in zip(checked,checked[1:])):raise ValueError('unordered '+key)
        window=minimum_window_rate([value for value in checked if value<=active],active)
        windows[key]=window
        goals[key]=_goal(window['minimum_hz'],threshold,lower=True,
                         reason='no complete five-second window')
    resource=active_resources(resources,absolute,
        result.get('activity_end_perf_s',absolute+active if absolute is not None else None))
    goals['active_cpu_pct']=_goal(resource['cpu'],80.,reason=resource['scope'])
    goals['available_memory_bytes']=_goal(resource['memory'],128*1024**2,lower=True,reason=resource['scope'])
    feature=result.get('feature_discovery_s')
    if feature is not None:feature=_finite(feature,'feature duration')
    goals['feature_analysis_s']=_goal(feature,30.,reason='feature timer missing')
    ages=[]
    for row in display_records or ():
        if not valid_vdma_ack(row.get('receipt')):continue
        source=row.get('source_capture_monotonic_ns');ack=row.get('ack_monotonic_ns')
        if type(source) is not int or type(ack) is not int or source<=0:continue
        if ack<source:raise ValueError('display ACK predates source image')
        if absolute is not None and not absolute<=row.get('ack_perf_s',-1)<=absolute+active:continue
        ages.append((ack-source)/1e9)
    goals['source_to_display_age_p95_s']=_goal(percentile(ages,.95),.5,
        reason='source image and valid ACK timestamps unavailable; submit age is not source age')
    goals['strict_saved_readback']=dict(status='PASS' if strict_readback is True else
        'FAIL' if strict_readback is False else 'UNMEASURED',
        reason='strict iterator exhausted against actual saved payload/index' if strict_readback is not None
               else 'run data_correct flag alone does not certify independent readback')
    goals['actual_pl_execution']=dict(status='PASS' if result.get('actual_PL_execution') is True else 'UNMEASURED')
    frame_counts=[result.get(key) for key in ('frames','requested_frames','valid_XYZ_profiles')]
    known=all(type(value) is int and value>0 for value in frame_counts)
    goals['frame_completion']=dict(status=('PASS' if len(set(frame_counts))==1 else 'FAIL')
        if known else 'UNMEASURED',frames=result.get('frames'),requested=result.get('requested_frames'),
        valid_XYZ_profiles=result.get('valid_XYZ_profiles'))
    calls=[result.get(key) for key in ('ARM_pixel_extraction_calls','ARM_branch_link_calls','ARM_Raw_quality_calls')]
    goals['normal_arm_image_calls']=dict(status=('PASS' if all(value==0 for value in calls) else 'FAIL')
        if all(type(value) is int and value>=0 for value in calls) else 'UNMEASURED')
    statuses=[item['status'] for item in goals.values()]
    status='FAIL' if 'FAIL' in statuses else 'UNMEASURED' if 'UNMEASURED' in statuses else 'PASS'
    return dict(schema='R26_SOFTWARE11_ACCEPTANCE_V1',status=status,goals=goals,
                windows=windows,resources=resource,source_age_sample_count=len(ages),
                scope='PERFORMANCE_AND_STRICT_SAVED_READBACK_ONLY',
                physical_accuracy_verified=False,
                algorithm_accuracy_acceptance='SEPARATE_HOLDOUT_SCORE_REQUIRED')


def capacity_preflight(*, max_frame_bytes, existing_data_bytes, filesystem_free_bytes,
                       fixed_output_bytes=0):
    for name,value in (('max_frame_bytes',max_frame_bytes),('fixed_output_bytes',fixed_output_bytes)):
        if type(value) is not int or value<0 or (name=='max_frame_bytes' and value==0):
            raise ValueError('nonnegative fixed bytes and positive frame bound required')
    plans={}
    for name,frames in (('six_240_frame_runs',240*3*2),('continuous_30min_at_10hz',18000)):
        plan=capacity_plan(frames,max_frame_bytes,existing_data_bytes=existing_data_bytes,
                           filesystem_free_bytes=filesystem_free_bytes)
        needed=((frames*max_frame_bytes+fixed_output_bytes)*5+3)//4
        plan['budget_bytes']=needed;plan['reasons']=[]
        if existing_data_bytes+needed>plan['limits']['data_bytes']:plan['reasons'].append('data_quota')
        if filesystem_free_bytes-needed<plan['limits']['free_reserve_bytes']:plan['reasons'].append('filesystem_reserve')
        plan['admitted']=not plan['reasons'];plan.update(frame_count=frames,
            fixed_output_bytes=fixed_output_bytes,max_frame_bytes=max_frame_bytes,
            frame_bound_scope='ALL_RAW_PFO3_PRIMARY_MBR_XYZ_PREVIEW_INDEX_EXPORT_TELEMETRY')
        plans[name]=plan
    return dict(schema='R26_SOFTWARE11_CAPACITY_V1',plans=plans,allocation_performed=False,
                input_warning='frame bound must include all outputs; observed compression average is not a bound')


def _read(path, *, jsonl=False):
    path=Path(path)
    if path.stat().st_size>64*1024**2:raise ValueError('acceptance input exceeds64MiB')
    with path.open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()] if jsonl else json.load(stream)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    modes=parser.add_subparsers(dest='command',required=True)
    audit=modes.add_parser('audit');audit.add_argument('--result',type=Path,required=True)
    audit.add_argument('--resources',type=Path);audit.add_argument('--display-records',type=Path)
    audit.add_argument('--strict-records',type=Path)
    capacity=modes.add_parser('capacity')
    for name in ('max-frame-bytes','existing-data-bytes','filesystem-free-bytes'):
        capacity.add_argument('--'+name,type=int,required=True)
    capacity.add_argument('--fixed-output-bytes',type=int,default=0)
    args=parser.parse_args(argv)
    if args.command=='capacity':
        report=capacity_preflight(max_frame_bytes=args.max_frame_bytes,existing_data_bytes=args.existing_data_bytes,
            filesystem_free_bytes=args.filesystem_free_bytes,fixed_output_bytes=args.fixed_output_bytes)
    else:
        result=_read(args.result);readback=None
        if args.strict_records:
            # Importing the disk reader is software-only; no PYNQ/device import.
            from candidate.storage_r21 import iter_records
            count=sum(1 for row in iter_records(args.strict_records) if row['kind']=='frame')
            readback=count==result.get('frames') and count>0
        displays=_read(args.display_records,jsonl=args.display_records.suffix=='.jsonl') if args.display_records else None
        report=audit_run(result,resources=_read(args.resources,jsonl=True) if args.resources else (),
                         display_records=displays,strict_readback=readback)
    print(json.dumps(report,indent=2,allow_nan=False))
    return 0 if (report.get('status')=='PASS' or args.command=='capacity' and
                 all(plan['admitted'] for plan in report['plans'].values())) else 2


if __name__=='__main__':raise SystemExit(main())
