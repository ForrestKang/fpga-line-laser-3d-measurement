"""Bounded optional-J2 storage probe over saved actual-PL evidence; no devices.

NOT_COMPARABLE: J2 currently stores a complete independent preview MBR/Raw/source
record, while production v1 stores only preview XYZ plus its hash index. This
tool deliberately does not run a misleading ABBA comparison of unequal work.
It measures J2-only with real fsync, strict readback and byte equivalence.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import shutil
import struct
import sys
import time

import numpy as np

RUNTIME=Path(__file__).resolve().parents[1]
for directory in (RUNTIME,RUNTIME/'structured_light_system_algorithm_v1/src/python',
                  RUNTIME/'structured_light_system_geometry_v1/src/python'):
    if str(directory) not in sys.path:sys.path.insert(0,str(directory))

# Geometry's package initializer imports checkerboard helpers even though this
# tool never operates a camera. Reuse the existing recorded dependency route.
from tools.offline_scan_r26 import ensure_cv2
CV2_DEPENDENCY=ensure_cv2()
from candidate import storage_r21 as legacy
from candidate.storage_journal_v2_r26 import MultiBranchJournalWriterV2,JournalReaderV2
from resource_policy import StorageBudget,capacity_plan,BudgetExceeded

MAX_PREPARED=64*1024**2
MAX_SCRATCH=32*1024**2


class _NoHistory:
    def append(self,points):
        if points.ndim!=2 or points.shape[1]!=3 or not np.isfinite(points).all():
            raise ValueError('finite preview points required')


def _hash(data):return hashlib.sha256(data).hexdigest()


def _measurement(record):
    meta=record['metadata']
    result=dict(request_id=meta['request_id'],capture_epoch=meta['capture_epoch'],
        raw=record['raw'],raw_sha256=meta['raw_sha256'],profile_payload=record['profile_payload'],
        points_camera_mm=record['points_camera_mm'],points_axis_mm=record['points_axis_mm'],
        summary=record['summary'],motion=meta.get('motion'),exposure=meta.get('exposure'))
    result.update({key:record[key] for key in legacy._SOURCE_PAYLOADS if key in record})
    return result


def derive_preview(record,processor):
    """Use production geometry on saved PL observations, never measurement XYZ."""
    from tools.offline_scan_r26 import ReplayAdmissionR26
    from candidate.pfo3_r25 import decode,identity,HEADER_FIELDS
    meta=record['profile']['frame_metadata']
    payload=record['source_pfo3_payload'];primary=record['source_pfo3_primary']
    ident=identity(dict(zip(HEADER_FIELDS,struct.unpack_from('<64I',payload))))
    frame=decode(payload,expected_identity=ident)
    receipt=meta.get('PL_transport_receipt',{})
    if (receipt.get('actual_PL_execution') is not True or receipt.get('assembled_sha256')!=_hash(payload)
            or receipt.get('input_sha256')!=record['metadata']['raw_sha256']
            or receipt.get('primary_stream_sha256')!=_hash(primary)):
        raise ValueError('saved actual-PL transport/source binding is missing')
    fingerprint=struct.pack('<8I',*(frame.header['fingerprint'+str(i)] for i in range(8))).hex()
    admission=ReplayAdmissionR26(ident=identity(frame.header),parameter_fingerprint=fingerprint,
        bit_sha256=receipt['bit_sha256'],stream_sha256=_hash(payload),
        input_raw_sha256=_hash(record['raw']),primary_sha256=_hash(primary),receipt=receipt)
    if getattr(processor,'calibration_id',None)!=meta.get('calibration_id'):
        raise ValueError('supplied simulation geometry calibration ID differs from saved run')
    incoming=dict(meta,request_id=record['metadata']['request_id'],
                  capture_epoch=record['metadata']['capture_epoch'])
    gray=np.frombuffer(record['raw'],np.uint8).reshape(480,752)
    profile=processor.process_final(gray,incoming,payload=payload,admission=admission,mode='preview')
    profile.frame_metadata.update(offline_replay=True,PL_execution_in_this_process=False,
        evidence_class='SAVED_ACTUAL_PL_PACKET_OFFLINE_REPLAY')
    summary=legacy._json(json.dumps(profile.summary(),allow_nan=False))
    result=_measurement(record)
    result.update(profile_payload=legacy.encode_profile(profile,summary),summary=summary,
                  points_camera_mm=profile.delta_camera_mm,points_axis_mm=profile.delta_axis_mm)
    if legacy.decode_profile(result['profile_payload'])['frame_metadata']['pfo3_mode']!='preview':
        raise ValueError('production geometry did not produce an independent preview profile')
    return result


def prepare_saved(source,calibration,*,limit=32):
    """Exhaust complete source once, retain only a finite <=64MiB prepared set."""
    if type(limit) is not int or not 1<=limit<=32:raise ValueError('limit must be 1..32')
    from candidate.pfo3_processor_r25 import PLFinalProcessorR25
    source=Path(source);calibration=Path(calibration)
    result=legacy._json((source/'RESULT.json').read_bytes())
    if result.get('data_correct') is not True or result.get('actual_PL_execution') is not True:
        raise ValueError('source must be a completed saved actual-PL run')
    processor=PLFinalProcessorR25.from_virtual_config(calibration,max_cloud_points=limit*legacy.MAX_POINTS)
    processor._cloud=_NoHistory()
    prepared=[];resident=source_frames=0;start=time.perf_counter();cpu=time.process_time()
    for record in legacy.iter_records(source/'frames'):
        if record['kind']!='frame':continue
        source_frames+=1
        if len(prepared)>=limit:continue
        item=_measurement(record)
        item['preview_record']=derive_preview(record,processor)
        charge=legacy.MultiBranchChunkWriter._resident_bytes(item)
        if charge>MAX_SCRATCH or resident+charge>MAX_PREPARED:
            raise BufferError('finite prepared input exceeds scratch/64MiB capacity')
        prepared.append(item);resident+=charge
    if len(prepared)!=limit or source_frames!=result.get('frames'):
        raise ValueError('source frame count/EOF does not match completed run')
    return prepared,dict(source=str(source.resolve()),source_frames=source_frames,
        source_strict_iterator_exhausted=True,selected_frames=limit,
        calibration_sha256=_hash(calibration.read_bytes()),prepared_resident_charge_bytes=resident,
        calibration_binding='SUPPLIED_CONFIG_SHA_RECORDED_AND_SAVED_CALIBRATION_ID_MATCHED',
        wall_s=time.perf_counter()-start,process_cpu_s=time.process_time()-cpu,
        preview_source='PRODUCTION_PLFinalProcessorR25_PREVIEW_FROM_SAVED_PFO3',
        PL_execution_in_this_benchmark=False,geometry_evidence='SIMULATION_ONLY')


def compare_record(expected,actual):
    """Exact source/measurement and independent-preview comparison after decode."""
    for key in ('raw','profile_payload')+legacy._SOURCE_PAYLOADS:
        if expected.get(key)!=actual.get(key):raise ValueError('readback byte mismatch: '+key)
    for key in ('request_id','capture_epoch','raw_sha256','summary','motion','exposure'):
        if expected.get(key)!=actual.get(key):raise ValueError('readback metadata mismatch: '+key)
    for key in legacy._POINTS:
        before,after=expected.get(key),actual.get(key)
        if before is None or after is None:
            if before is not after:raise ValueError('readback XYZ presence mismatch')
        elif (before.dtype!=after.dtype or before.shape!=after.shape or before.tobytes()!=after.tobytes()):
            raise ValueError('readback XYZ mismatch: '+key)


def verify_journal(directory,prepared,written):
    reader=JournalReaderV2(directory,written_watermark=written)
    count=0;preview_points=measurement_points=0
    for row in reader:
        if row['kind']!='frame' or count>=len(prepared):raise ValueError('unexpected journal record')
        expected=prepared[count]
        compare_record(expected,row['record'])
        compare_record(expected['preview_record'],row['preview_record'])
        measurement_points+=len(row['record']['points_axis_mm'])
        preview_points+=len(row['preview_record']['points_axis_mm'])
        count+=1
    if count!=len(prepared) or reader.report.get('durable_confirmed') is not True:
        raise ValueError('journal incomplete or successful sync watermark unconfirmed')
    return dict(verified=True,frames=count,measurement_points=measurement_points,
                preview_points=preview_points,reader=reader.report)


def capacity_preflight(prepared,output,*,data_root,runs=1):
    output=Path(output).resolve();data_root=Path(data_root).resolve()
    if type(runs) is not int or not 1<=runs<=4:raise ValueError('runs must be 1..4')
    if output.exists():raise FileExistsError('new output ID required')
    if not data_root.is_dir() or data_root not in output.parents:
        raise ValueError('new output must be beneath an existing managed data root')
    existing=sum(path.stat().st_size for path in data_root.rglob('*') if path.is_file())
    # Include snapshot/seal/index overhead before applying the shared25% margin.
    maximum=max(legacy.MultiBranchChunkWriter._resident_bytes(item)+128*1024 for item in prepared)
    # The strict writer uses the same resident-charge bound plus25% headroom.
    plan=capacity_plan(len(prepared)*runs,maximum,existing_data_bytes=existing,
                       filesystem_free_bytes=int(shutil.disk_usage(data_root).free))
    if not plan['admitted']:raise BudgetExceeded('storage probe capacity refused: '+str(plan['reasons']))
    return StorageBudget(existing,lambda:int(shutil.disk_usage(data_root).free)),plan


def run_journal(directory,prepared,budget):
    directory=Path(directory)
    start=time.perf_counter();cpu=time.process_time()
    writer=MultiBranchJournalWriterV2(directory,capacity=32,byte_capacity=MAX_PREPARED,
        scratch_bytes=MAX_SCRATCH,group_size=8,max_delay_s=.05,raw_codec='zlib1',
        storage_budget=budget,validation_phase='worker')
    try:
        for item in prepared:writer.submit_wait(item,timeout_s=10.)
        receipt=writer.close(timeout_s=30.)
        if not receipt.durable or receipt.written!=len(prepared):
            raise RuntimeError('journal write not durably completed')
    finally:
        # Idempotent normal close; failure close remains an error, never success.
        writer.close(timeout_s=30.)
    save_wall=time.perf_counter()-start;save_cpu=time.process_time()-cpu
    start=time.perf_counter();cpu=time.process_time()
    readback=verify_journal(directory,prepared,receipt.written)
    return dict(backend='OPTIONAL_JOURNAL_V2',frames=len(prepared),
        write_wall_s=save_wall,write_process_cpu_s=save_cpu,
        storage_only_frames_per_s=len(prepared)/save_wall,
        readback_wall_s=time.perf_counter()-start,readback_process_cpu_s=time.process_time()-cpu,
        receipt=asdict(receipt),strict_readback=readback,
        io_accounting=writer.io_accounting,successful_batches=writer.batch_count,
        batch_quantiles={key:{'p50_s':float(np.percentile([s[key] for s in writer.batch_stats],50)),
                              'p95_s':float(np.percentile([s[key] for s in writer.batch_stats],95)),
                              'p99_s':float(np.percentile([s[key] for s in writer.batch_stats],99))}
                         for key in ('batch_s','journal_fsync_s','thread_cpu_s')},
        peak_pending_frames=writer.peak_pending_items,peak_pending_bytes=writer.peak_pending_bytes,
        PL_execution_in_this_benchmark=False,full_chain_performance_verified=False,
        files_bytes=sum(path.stat().st_size for path in directory.rglob('*') if path.is_file()))


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True,help='completed saved actual-PL target directory, e.g.T05_01')
    p.add_argument('--calibration',type=Path,required=True,help='matching frozen simulation geometry config')
    p.add_argument('--output',type=Path,required=True,help='new unique output directory')
    p.add_argument('--data-root',type=Path,required=True,help='existing dedicated managed data directory')
    p.add_argument('--limit',type=int,choices=range(1,33),default=32)
    p.add_argument('--runs',type=int,choices=range(1,5),default=1)
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    if args.output.exists():raise FileExistsError('new output ID required')
    prepared,preparation=prepare_saved(args.source,args.calibration,limit=args.limit)
    budget,capacity=capacity_preflight(prepared,args.output,data_root=args.data_root,runs=args.runs)
    args.output.mkdir(parents=True,exist_ok=False)
    result=dict(schema='R26_J2_STORAGE_PROBE_V1',comparison_status='NOT_COMPARABLE',
        reason='J2 persists full preview MBR+Raw+sources; production V1 persists preview XYZ+hash index only; no equivalent-work ABBA performed',
        input_preparation=preparation,capacity=capacity,runs=[],
        execution_host=dict(system=platform.system(),machine=platform.machine(),
                            node=platform.node(),python=sys.version,cv2_dependency=CV2_DEPENDENCY),
        persistence_workload=dict(measurement='complete legacy record including Raw/PFO3/primary/MBR/XYZ',
            preview='independent full Raw/PFO3/primary/preview MBR/preview XYZ; more work than V1 preview spool',
            inputs_prepared_before_timing=True,fsync_enabled=True,
            queue_frames=32,queue_bytes=MAX_PREPARED,scratch_bytes=MAX_SCRATCH),
        PL_execution_in_this_benchmark=False,camera_commands=0,motor_commands=0,
        physical_accuracy_verified=False,full_chain_performance_verified=False)
    from tools.pl_replay_r25 import save_json
    try:
        for index in range(args.runs):
            result['runs'].append(run_journal(args.output/('j2_%02d'%(index+1)),prepared,budget))
        result['status']='STORAGE_ONLY_STRICT_READBACK_PASSED'
        save_json(args.output/'RESULT.json',result,budget)
    except BaseException as exc:
        result.update(status='FAILED',error=type(exc).__name__+': '+str(exc),automatic_retry=False)
        save_json(args.output/'ERROR.json',result,budget)
        raise
    print(json.dumps(dict(output=str(args.output),status=result['status'],comparison_status='NOT_COMPARABLE')))
    return result


if __name__=='__main__':main()
