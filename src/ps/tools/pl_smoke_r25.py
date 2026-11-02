"""One real DDR->PL->DDR frame, then separately compare the integer oracle.

Attach only. No bit download, camera capture, HDMI, motor or automatic retry.
Reference execution after DMA is verification work, excluded from speed claims.
"""
from pathlib import Path
import argparse,hashlib,json,os,platform,sys,time
ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT/'structured_light_system_algorithm_v1/src/python',ROOT/'structured_light_system_geometry_v1/src/python'):
    sys.path.insert(0,str(p))
import numpy as np

def main():
    p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--input-root',type=Path,required=True)
    p.add_argument('--target',choices=('T05','T06'),default='T05');p.add_argument('--frame',type=int,default=0)
    args=p.parse_args()
    if platform.system()!='Linux' or platform.machine() not in ('armv7l','armv7'):
        raise RuntimeError('actual ARM board required; no connection made')
    if not 0<=args.frame<240:raise ValueError('bounded frozen input index')
    from tools.pl_replay_r25 import capacity_admission,save_json,replay_identity,replay_metadata,NormalImageGuard
    from tools.board_replay_r22 import quarantine_owner
    from candidate.replay_input_r21 import NativeImageReplay
    from candidate.pfo3_transport_r25 import ActualPLTransportR25,resolve_manifest
    from candidate.pfo3_processor_r25 import PLFinalProcessorR25
    from structured_light.motion.lifecycle import HardwareProcessLock
    from pynq import Overlay
    if args.output.exists():raise FileExistsError(args.output)
    budget,plan=capacity_admission(args.output,1)
    source=NativeImageReplay(args.input_root/args.target);raw=source.read_frame(args.frame)
    gray=np.frombuffer(raw,np.uint8).reshape(480,752);manifest=resolve_manifest(args.manifest)
    args.output.mkdir();lock=HardwareProcessLock(label='r25_one_frame_actual_PL_no_motion').acquire()
    driver=None;result={'passed':False,'FPGA_loads':0,'motor_START':0,'camera_commands':0,
        'geometry_evidence_type':'SIMULATION_ONLY','physical_accuracy_verified':False,'capacity':plan,
        'acceptance_requires_clean_process_exit_and_CLEANUP_receipt':True}
    def binary(name,data):
        ticket=budget.reserve((len(data)*5+3)//4)
        with (args.output/name).open('xb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        budget.commit(ticket,len(data))
    try:
        binary('input.gray8.bin',raw)
        driver=ActualPLTransportR25(Overlay(manifest['bit_absolute'],download=False),manifest=manifest,hardware_lock=lock)
        identity=replay_identity(args.frame,1)
        started=time.perf_counter();frame,primary,receipt,admission=driver.run(gray,identity=identity)
        transaction=time.perf_counter()-started
        binary('primary.bin',primary);binary('final.pfo3',frame.payload)
        result.update(actual_PL_execution=True,receipt=receipt,transaction_including_input_copy_s=transaction)
        if frame.raw_required:raise ValueError('smoke input unexpectedly requires ARM takeover')
        # The actual hardware payload is already immutable and saved above.
        from candidate.v3_reference_r25 import extract_v3,V3Config
        from candidate.pfo3_reference_adapter_r25 import records_from_reference
        started=time.perf_counter();reference=extract_v3(gray,V3Config());reference_s=time.perf_counter()-started
        np.testing.assert_array_equal(frame.records,records_from_reference(reference))
        np.testing.assert_array_equal(frame.rows.view('<u4').reshape(-1,16),reference.row_data)
        f=reference.frame
        expected=dict(qualified_count=f.local_count,measurement_count=f.measurement_count,preview_count=f.preview_count,
            sat250_count=f.saturated_pixels,sat255_count=f.saturated255_pixels,measure_valid_rows=f.measurement_rows,
            preview_valid_rows=f.preview_rows,ambiguous_rows=f.ambiguous_rows,measure_status=f.measure_status,
            preview_status=f.preview_status,measure_median_peak_x2=f.measure_median_peak_x2,
            measure_median_width_x2=f.measure_median_width_x2,preview_median_peak_x2=f.preview_median_peak_x2,
            preview_median_width_x2=f.preview_median_width_x2,raw_peak_median_x2=f.raw_peak_median_x2,
            frame_decisions=int(f.measurement_usable)|(int(f.preview_usable)<<1)|(int(f.flags)<<8))
        for name,value in expected.items():
            if frame.header[name]!=value:raise AssertionError('actual PL summary mismatch: '+name)
        processor=PLFinalProcessorR25.from_virtual_config(args.input_root/args.target/'calibration.json')
        with NormalImageGuard() as guard:
            geometry=processor.process_final(gray,replay_metadata(source.frames[args.frame],identity),
                payload=frame.payload,admission=admission)
        binary('XYZ_axis_float64.bin',geometry.delta_axis_mm.tobytes())
        result.update(passed=bool(len(frame.records)>0 and geometry.valid_points.any()),
            integer_reference_equal=True,candidate_field_checks=len(frame.records)*16,
            row_field_checks=len(frame.rows)*16,independent_reference_wall_s=reference_s,
            ARM_reference_verification_calls=1,reference_excluded_from_normal_path=True,
            geometry_guard=guard.report(),valid_XYZ_points=int(geometry.valid_points.sum()),
            PL_cycles=frame.header['processing_cycles_lo']+(frame.header['processing_cycles_hi']<<32),
            performance_acceptance=False,performance_scope='one frame correctness only, no stable FPS claim')
        save_json(args.output/'SMOKE.json',result,budget)
        print(json.dumps(result));return 0 if result['passed'] else 1
    except BaseException as exc:
        result.update(error=type(exc).__name__+': '+str(exc),passed=False)
        if driver is not None and not driver.active:
            try:
                driver.output.invalidate()
                binary('INVALID_DMA_BUFFER.bin',bytes(memoryview(driver.output)))
            except BaseException as dump_error:result['dump_error']=str(dump_error)
        try:save_json(args.output/'ERROR.json',result,budget)
        except BaseException:pass
        raise
    finally:
        safe=driver is None
        if driver is not None:
            try:driver.close();safe=driver.closed and not driver.active
            except BaseException:safe=False
        if safe:
            try:save_json(args.output/'CLEANUP.json',dict(dma_quiescent=True,buffers_released=True,
                hardware_owner_confirmed_closed=True,reference_worker_outstanding=False),budget)
            finally:lock.release()
        else:quarantine_owner(args.output)

if __name__=='__main__':raise SystemExit(main())
