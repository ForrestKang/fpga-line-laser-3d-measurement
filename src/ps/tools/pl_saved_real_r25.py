"""Four saved real camera frames through actual PL, without optical claims.

No camera capture, CPU image extraction, geometry, motor commands or retry.
All sources are fsynced; the independent integer oracle runs later on the PC.
"""
from pathlib import Path
import argparse,hashlib,json,os,platform,sys,time
ROOT=Path(__file__).resolve().parents[1]
for path in (ROOT,ROOT/'structured_light_system_algorithm_v1/src/python',
             ROOT/'structured_light_system_geometry_v1/src/python'):sys.path.insert(0,str(path))
import numpy as np

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('manifest','input-root','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--pfo3-validator-manifest',type=Path);args=parser.parse_args()
    if platform.system()!='Linux' or platform.machine() not in ('armv7l','armv7'):
        raise RuntimeError('actual ARM Linux board required')
    from tools.pl_replay_r25 import capacity_admission,save_json,replay_identity,NormalImageGuard
    from tools.board_replay_r22 import quarantine_owner
    from candidate.pfo3_transport_r25 import ActualPLTransportR25,resolve_manifest
    from structured_light.motion.lifecycle import HardwareProcessLock
    from pynq import Overlay
    if args.output.exists():raise FileExistsError(args.output)
    source=args.input_root/'REAL_R24';metadata=(source/'INPUT_MANIFEST.json').read_bytes()
    if len(metadata)>8192:raise ValueError('bounded saved-real manifest required')
    records=json.loads(metadata)
    if records['schema']!='R25_SAVED_REAL_INPUTS' or records['source']!='R24_REAL_CAMERA_FRAMES':
        raise ValueError('explicit real-source provenance required')
    entries=records['frames']
    if len(entries)!=4 or len({x['file'] for x in entries})!=4:raise ValueError('exactly four unique frozen inputs required')
    raws=[]
    for entry in entries:
        path=(source/entry['file']).resolve()
        if path.parent!=source.resolve():raise ValueError('input outside saved-real directory')
        raw=path.read_bytes()
        if len(raw)!=360960 or len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:
            raise ValueError('saved-real source size/hash mismatch')
        raws.append(raw)
    budget,plan=capacity_admission(args.output,4)
    manifest=resolve_manifest(args.manifest)
    validator=None
    if args.pfo3_validator_manifest:
        from candidate.pfo3_decode_native_r25 import NativeBodyValidatorR25
        validator=NativeBodyValidatorR25.from_manifest(args.pfo3_validator_manifest)
    args.output.mkdir();lock=HardwareProcessLock(label='r25_saved_real_four_frames_no_motion').acquire()
    driver=None;completed=False;rows=[]
    def binary(path,data):
        ticket=budget.reserve((len(data)*5+3)//4)
        with path.open('xb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        budget.commit(ticket,len(data))
    try:
        driver=ActualPLTransportR25(Overlay(manifest['bit_absolute'],download=False),manifest=manifest,
            hardware_lock=lock,body_validator=validator)
        with NormalImageGuard() as guard:
            for index,(raw,entry) in enumerate(zip(raws,entries)):
                directory=args.output/f'frame_{index:02d}';directory.mkdir()
                binary(directory/'raw.gray8.bin',raw)
                identity=replay_identity(index,1)
                started=time.perf_counter()
                frame,primary,receipt,admission=driver.run(np.frombuffer(raw,np.uint8).reshape(480,752),identity=identity)
                elapsed=time.perf_counter()-started
                binary(directory/'primary.bin',primary);binary(directory/'final.pfo3',frame.payload)
                row=dict(frame_id=index,origin=entry,identity=identity,transport=receipt,
                    raw_required=frame.raw_required,measurement_usable=frame.frame_usable('measurement'),
                    preview_usable=frame.frame_usable('preview'),diagnostics=frame.header['diagnostic_count'],
                    qualified=frame.header['qualified_count'],measurement_observations=frame.header['measurement_count'],
                    preview_observations=frame.header['preview_count'],transaction_wall_s=elapsed,
                    actual_PL_execution=True,geometry_executed=False,physical_accuracy_verified=False)
                save_json(directory/'RECEIPT.json',row,budget);rows.append(row)
                if frame.raw_required:raise RuntimeError('saved-real PL frame requires explicit takeover; preserve and stop')
            result=dict(schema='R25_ACTUAL_PL_SAVED_REAL_FOUR',operational_passed=True,frames=rows,
                capacity=plan,**guard.report(),CPU_reference_calls=0,actual_PL_execution=True,
                geometry_executed=False,physical_accuracy_verified=False,performance_verified=False,
                input_evidence='SAVED_REAL_R24_CAMERA_IMAGES_REPLAYED_THROUGH_ACTUAL_PL',
                camera_commands=0,motor_START=0,FPGA_loads=0)
            save_json(args.output/'DONE.json',result,budget)
        completed=True;print(json.dumps(dict(operational_passed=True,frames=len(rows),guard=guard.report(),
            geometry_executed=False,performance_verified=False)));return 0
    except BaseException as exc:
        save_json(args.output/'ERROR.json',dict(error=type(exc).__name__+': '+str(exc),completed_frames=len(rows),
            partial_evidence_preserved=True,automatic_retry=False),budget)
        raise
    finally:
        safe=driver is None
        if driver is not None:
            try:driver.close();safe=driver.closed and not driver.active
            except BaseException:safe=False
        if safe:
            try:save_json(args.output/'CLEANUP.json',dict(completed=completed,dma_quiescent=True,
                hardware_owner_confirmed_closed=True,buffers_released=True),budget)
            finally:lock.release()
        else:quarantine_owner(args.output)

if __name__=='__main__':raise SystemExit(main())
