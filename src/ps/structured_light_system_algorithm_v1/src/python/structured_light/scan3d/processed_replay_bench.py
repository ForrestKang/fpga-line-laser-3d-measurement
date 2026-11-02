"""Bounded saved-real-data benchmark of production SLC1/XYZ and HDMI display.

Software-modeled compact packets replace unavailable historical DMA packets.
Preview stitching uses an explicitly assumed axis, never measurement admission.
This module has no camera, motor, SCCB, or FPGA-programming entry point.
"""
from pathlib import Path
import dataclasses
import hashlib
import json
import os
import platform
import time

import cv2
import numpy as np

from .continuous_processor import ContinuousProcessor
from .dashboard import DashboardRenderer
from .pipeline import ScanCalibration, DEFAULT_STRIPE_CONFIG, DEFAULT_SELECTION
from .processed_display import ProcessedObserver
from .processed_replay import prepare_manifest_replay
from .stream_chunk_writer import ChunkedAsyncWriter
from .turntable import TurntableModel


def _write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


class _CanvasSink:
    def __init__(self, remote=None):
        self.remote = remote
        self.last_canvas = None
    def show_canvas(self, canvas):
        self.last_canvas = canvas
        if self.remote is not None:
            return self.remote.show_canvas(canvas)
        return {'hdmi_acknowledged': False, 'source': 'CPU rendered canvas only'}
    def close(self):
        if self.remote is not None:
            return self.remote.close()


def run_replay(*, dataset, camera, plane, axis, output, repeats=20,
               hdmi_manifest=None, persist=True, asynchronous_display=False, display_capacity=1,
               shared_canvas=False, replay_policy='slc1', point_raster_manifest=None, owner_scene=False):
    if type(repeats) is not int or not 1 <= repeats <= 100:
        raise ValueError('repeats must be in 1..100')
    if type(asynchronous_display) is not bool:
        raise ValueError('asynchronous_display must be bool')
    if type(shared_canvas) is not bool or (shared_canvas and hdmi_manifest is None):
        raise ValueError('shared_canvas requires an explicit bool and an HDMI manifest')
    if replay_policy not in ('slc1','ridge_raw_v4','ridge_primary_v4','ridge_primary_v4_batch'):
        raise ValueError('explicit supported replay policy required')
    if (type(owner_scene) is not bool or (owner_scene and
            (shared_canvas or hdmi_manifest is None or point_raster_manifest is None
             or replay_policy!='ridge_primary_v4_batch'))):
        raise ValueError('owner_scene requires replay primary batch, native manifest, HDMI and exclusive scene transport')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    cv2.setNumThreads(1)
    initialized_at = time.perf_counter()
    calibration = ScanCalibration.load(camera, plane)
    ridge_replay = replay_policy != 'slc1'
    if ridge_replay:
        from .ridge_replay_bench import prepare_ridge_manifest_replay, RidgeReplayProcessor
        data = prepare_ridge_manifest_replay(dataset, DEFAULT_STRIPE_CONFIG)
        modeled_cache = output/'MODELED_SLC2_ONLY'
        modeled_cache.mkdir()
        for entry in data.entries:
            with (modeled_cache/('request_%010d.slc2.bin'%entry.request_id)).open('xb') as stream:
                stream.write(entry.payload)
    else:
        data = prepare_manifest_replay(dataset, DEFAULT_STRIPE_CONFIG)
    declared = json.loads(Path(dataset).read_text(encoding='utf-8'))
    for key, actual in [('frozen_camera_sha256',calibration.camera_sha256),
                        ('frozen_plane_sha256',calibration.plane_sha256)]:
        if key in declared and declared[key] != actual:
            raise ValueError('replay calibration binding mismatch: '+key)
    model_json = json.loads(Path(axis).read_text(encoding='utf-8'))
    model = TurntableModel.from_json(model_json)
    if model.calibrated:
        raise ValueError('this replay benchmark requires an explicitly assumed axis')
    for key, actual in [('camera_sha256',calibration.camera_sha256),('plane_sha256',calibration.plane_sha256)]:
        if key in model_json and model_json[key] != actual:
            raise ValueError('assumed preview axis belongs to another calibration')
    _write_new(output/'INPUT_PROVENANCE.json', dict(prepared=data.provenance,
        entries=[e.provenance for e in data.entries], preview_axis=model_json,
        camera_sha256=calibration.camera_sha256,plane_sha256=calibration.plane_sha256))
    report = dict(passed=False, source='saved_real_data_replay', motion_start_count=0,
        guard_arm_count=0, camera_capture_count=0, sensor_write_count=0,
        overlay_download_count=0, physical_accuracy_verified=False,
        measurement_eligible=False, axis_validated=False, timing_validated=False,
        modeled_slc1=not ridge_replay, modeled_slc2=ridge_replay,
        replay_policy=replay_policy, rtl_dma_captured=False, acquisition_included=False,
        unique_real_frames=len(data.entries), replay_repeats=repeats,
        angle_sources=data.provenance['angle_sources'],
        angle_span_deg=float(np.degrees(max(e.angle_rad for e in data.entries)-min(e.angle_rad for e in data.entries))),
        machine=platform.machine(), hdmi_requested=hdmi_manifest is not None,
        asynchronous_display=asynchronous_display, display_capacity=display_capacity,
        shared_canvas_requested=shared_canvas,
        native_measurement_width=752, native_measurement_height=480,
        preparation_s=data.preparation_s, preparation_excluded_from_steady_state_fps=True,
        persistence=persist, cycles=[], profiles=[])
    observer = sink = writer = remote = hardware_lock = None
    rows=[]
    benchmark_started = None
    cpu_started = None
    try:
        renderer=(None if owner_scene else DashboardRenderer(**({'point_raster_manifest':point_raster_manifest}
            if point_raster_manifest is not None else {})))
        if hdmi_manifest is not None:
            # The separate owner validates active bit and on-disk artifacts;
            # attaches metadata only (download=False), never programs FPGA.
            from ..p2.hdmi_owner import RemoteHDMIOutput
            from ..motion.lifecycle import HardwareProcessLock
            hardware_lock=HardwareProcessLock(label='r6_saved_replay_hdmi').acquire()
            os.environ['SL_HDMI_PROCESSED_CANVAS']='1'
            remote=RemoteHDMIOutput(hdmi_manifest,owner_log=output/'hdmi_owner.log',
                timeout_s=5.,startup_timeout_s=60.,preview_only=True,
                hardware_lock_fd=hardware_lock._handle.fileno(),
                **({'shared_canvas':True} if shared_canvas else {}),
                **({'shared_scene':True,'scene_raster_manifest':point_raster_manifest} if owner_scene else {}))
            remote.configure()
        if owner_scene:
            from .scene_replay import SceneReplaySink,OwnerSceneReplayObserver
            sink=SceneReplaySink(remote)
            observer=OwnerSceneReplayObserver(source='replay',sink=sink,preview_axis=model,
                asynchronous=asynchronous_display,capacity=display_capacity)
            report['scene_owner_capability']=remote.scene_capability
        else:
            sink=_CanvasSink(remote)
            observer=ProcessedObserver(source='replay',sink=sink,preview_axis=model,
                renderer=renderer,asynchronous=asynchronous_display,capacity=display_capacity)
        def processor():
            if ridge_replay:
                return RidgeReplayProcessor(variant=replay_policy,calibration=calibration,
                    stripe_config=data.stripe_config,exposure_lines=480,exposure_revision=0)
            return ContinuousProcessor(calibration=calibration,stripe_config=data.stripe_config,
                selection=DEFAULT_SELECTION,mode='static',exposure_lines=480,exposure_revision=0)
        first=data.entries[0]
        warm=processor().process(first.capture(),feature_config_fingerprint=data.stripe_config.fingerprint)
        report['warmup_display']=observer.consume(first.capture(),warm,
            preview_angle_rad=first.angle_rad,preview_angle_source=first.angle_source)
        observer.flush()
        observer.reset_preview()
        display_before=observer.summary()
        first_counted_sequence=display_before['submitted']+1
        report['initialization_s']=time.perf_counter()-initialized_at
        benchmark_started=time.perf_counter();cpu_started=time.process_time()
        with (output/'frames.jsonl').open('x',encoding='utf-8') as index:
            for cycle in range(repeats):
                # The original IDs/epoch are immutable; each replay cycle has
                # its own processor and persistence namespace. No invented IDs.
                proc=processor()
                if persist:
                    writer=ChunkedAsyncWriter(output/'records'/('cycle_%03d'%cycle),
                        capacity=16,group_size=4,max_delay_s=.4)
                cycle_started=time.perf_counter()
                for entry in data.entries:
                    started=time.perf_counter()
                    capture=entry.capture()
                    decoded=time.perf_counter()
                    result=proc.process(capture,feature_config_fingerprint=data.stripe_config.fingerprint)
                    xyz=time.perf_counter()
                    selection_evidence={}
                    if ridge_replay:
                        selection_evidence={key:hashlib.sha256(np.asarray(value,dtype=dtype).tobytes()).hexdigest()
                            for key,value,dtype in (
                                ('center_q12_sha256',result.center_q12,'<i8'),
                                ('stripe_accepted_sha256',result.stripe_accepted,np.uint8),
                                ('valid_points_sha256',result.valid_points,np.uint8),
                                ('points_camera_mm_sha256',result.points_camera_mm,'<f8'),
                                ('strict_xyz_sha256',result.points_camera_mm[result.valid_points],'<f8'))}
                        selection_evidence['reasons_sha256']=hashlib.sha256(
                            json.dumps(result.reasons,separators=(',',':')).encode()).hexdigest()
                        selection_evidence.update(replay_policy=replay_policy,
                            selection_fingerprint=proc.selection_v4.fingerprint,center_policy='original_q12',
                            modeled_slc2=True,rtl_dma_captured=False)
                    if writer is not None:
                        writer.submit(dict(request_id=capture.request_id,capture_epoch=capture.epoch,
                            raw=capture.raw,raw_sha256=capture.raw_sha256,profile_payload=capture.profile_payload,
                            points_camera_mm=result.points_camera_mm[result.valid_points],points_axis_mm=None,
                            summary={**result.summary(),**selection_evidence,'source':'saved_real_replay','measurement_eligible':False},
                            motion=None,exposure={'effect_known':False,'measurement_eligible':False,
                                'observation_only':True,'source':'historical metadata unavailable'}))
                    submitted=time.perf_counter()
                    display=observer.consume(capture,result,preview_angle_rad=entry.angle_rad,
                        preview_angle_source=entry.angle_source)
                    completed=time.perf_counter()
                    record=dict(cycle=cycle,request_id=entry.request_id,epoch=entry.epoch,
                        source_raw_sha256=entry.raw_sha256,angle_source=entry.angle_source,
                        angle_rad=entry.angle_rad,modeled_slc1=not ridge_replay,
                        modeled_slc2=ridge_replay,measurement_eligible=False,
                        observation_xyz_points=result.valid_point_count,profile_valid=result.profile_valid,
                        formal_fused=result.fused,quality=result.quality.status,
                        pl_primary_rows=result.pl_primary_rows,raw_fallback_rows=result.raw_fallback_rows,
                        decode_s=decoded-started,selection_xyz_quality_s=xyz-decoded,
                        writer_submit_s=submitted-xyz,display=display,frame_total_s=completed-started)
                    record.update(selection_evidence)
                    if ridge_replay:
                        record.update(selection_only_s=proc.last_selection_s,
                                      processor_packet_decode_s=proc.last_packet_decode_s)
                    rows.append(record)
                    index.write(json.dumps(record,allow_nan=False)+'\n')
                if writer is not None:
                    drain_started=time.perf_counter()
                    receipt=writer.close(timeout_s=10.)
                    drain_s=time.perf_counter()-drain_started
                    _write_new(output/'records'/('cycle_%03d'%cycle)/'RESULT.json',
                        {'writer':dataclasses.asdict(receipt),'source':'saved_real_replay',
                         'physical_accuracy_verified':False,'measurement_eligible':False})
                    report['cycles'].append(dict(cycle=cycle,writer=dataclasses.asdict(receipt),
                        elapsed_s=time.perf_counter()-cycle_started,writer_drain_s=drain_s))
                    if not receipt.durable or not receipt.worker_joined:
                        raise RuntimeError('replay storage did not durably drain')
                    writer=None
            index.flush();os.fsync(index.fileno())
        processing_elapsed=time.perf_counter()-benchmark_started
        flush_started=time.perf_counter()
        observer.flush()
        report['final_display_flush_s']=time.perf_counter()-flush_started
        close_started=time.perf_counter()
        display_after=observer.close(timeout_s=10.)
        report['display_close_s']=time.perf_counter()-close_started
        if not display_after['passed'] or not display_after['closed']:
            raise RuntimeError('display owner close or worker join not confirmed')
        if owner_scene and (sink.snapshot_error is not None or sink.last_canvas is None):
            report['artifact_failure']=sink.snapshot_error or 'missing final owner scene canvas'
            raise RuntimeError('final owner scene snapshot failed: '+report['artifact_failure'])
        elapsed=time.perf_counter()-benchmark_started
        cpu=time.process_time()-cpu_started
        counted_display={key:display_after[key]-display_before[key] for key in
            ('submitted','rendered','acknowledged','hdmi_acknowledged','dropped','failed')}
        display_records=[row for row in display_after['records']
                         if row['submission_sequence'] >= first_counted_sequence]
        ack=counted_display['hdmi_acknowledged']
        nonempty=sum(row['observation_xyz_points']>0 for row in rows)
        report.update(passed=True,counted_frames=len(rows),elapsed_s=elapsed,
            full_chain_fps=len(rows)/elapsed,nonempty_xyz_frames=nonempty,
            processing_elapsed_s=processing_elapsed,processing_fps=len(rows)/processing_elapsed,
            processing_elapsed_includes_writer_drain=True,
            full_chain_elapsed_includes_display_flush_close_and_writer_drain=True,
            counted_display=counted_display,
            rendered_full_chain_fps=counted_display['rendered']/elapsed,
            sink_acknowledged_full_chain_fps=counted_display['acknowledged']/elapsed,
            last_submitted_request_id=rows[-1]['request_id'],
            writer_drained_frames=sum(c['writer']['written'] for c in report['cycles']),
            writer_drain_s=sum(c['writer_drain_s'] for c in report['cycles']),
            nonempty_xyz_full_chain_fps=nonempty/elapsed,hdmi_acknowledged_frames=ack,
            hdmi_acknowledged_full_chain_fps=ack/elapsed,
            quality_accepted_frames=sum(row['profile_valid'] for row in rows),
            formal_fused_frames=sum(row['formal_fused'] for row in rows),
            cpu_foreground_one_core_pct=100*cpu/elapsed,
            cpu_foreground_total_capacity_pct=100*cpu/elapsed/(os.cpu_count() or 1),
            cpu_foreground_s=cpu,
            cpu_accounting='process_time includes main, display and writer threads; one-core percent may exceed 100; separate HDMI owner excluded',
            foreground_cpu_excludes_separate_hdmi_owner=remote is not None,
            final_preview_points=observer.summary()['preview_points'])
        report['stage_ms']={key:dict(median=1000*float(np.median(vals)),p95=1000*float(np.percentile(vals,95)))
            for key,vals in {
                'decode':[r['decode_s'] for r in rows],
                'selection_xyz_quality':[r['selection_xyz_quality_s'] for r in rows],
                'stitch':[r['display']['stitch_s'] for r in rows],
                'render':[r['render_s'] for r in display_records],
                'hdmi_or_cpu_sink':[r['show_s'] for r in display_records],
                'frame_total':[r['frame_total_s'] for r in rows]}.items()}
        report['stage_sample_counts']={'processing':len(rows),'render_and_sink':len(display_records)}
        if ridge_replay:
            report['v4_stage_ms']={key:dict(median=1000*float(np.median([r[key] for r in rows])),
                p95=1000*float(np.percentile([r[key] for r in rows],95)))
                for key in ('selection_only_s','processor_packet_decode_s')}
            report['selection_fingerprint']=proc.selection_v4.fingerprint
        # Preserve actual per-render/per-owner receipts after asynchronous
        # drain. No fabricated record is added for a dropped display request.
        report['renderer_stage_records']=[dict(submission_sequence=r['submission_sequence'],
            request_id=r['request_id'],timings=r.get('renderer_timings',{})) for r in display_records]
        report['point_raster_requested']='native' if point_raster_manifest is not None else 'python'
        report['point_raster_counted_backends']={key:sum(
            r.get('renderer_timings',{}).get('point_raster_backend')==key for r in display_records)
            for key in ('python','native','model_cache','mesh')}
        if getattr(renderer,'point_raster',None) is not None:
            report['point_raster']=renderer.point_raster.summary()
        report['owner_stage_records']=[dict(submission_sequence=r['submission_sequence'],
            request_id=r['request_id'],receipt=r.get('sink_receipt')) for r in display_records]
        def timing_summary(records):
            values={}
            for record in records:
                for key,value in (record or {}).items():
                    if (key.endswith(('_s','_ms')) and type(value) in (int,float)
                            and np.isfinite(value)):
                        values.setdefault(key,[]).append(value*(1000 if key.endswith('_s') else 1))
            return {key:dict(samples=len(vals),median_ms=float(np.median(vals)),
                p95_ms=float(np.percentile(vals,95))) for key,vals in values.items()}
        report['renderer_stage_ms']=timing_summary([r.get('renderer_timings') for r in display_records])
        report['owner_stage_ms']=timing_summary([r.get('sink_receipt') for r in display_records])
        report['owner_scene']=owner_scene
        if owner_scene:
            report['final_scene_snapshot_s']=sink.snapshot_s
            report['scene_final_snapshot']=getattr(remote,'scene_final_snapshot_metadata',None)
            report['scene_counted_owner_service_cpu_s']=sum(r['sink_receipt']['scene_owner_cpu_s'] for r in display_records)
            report['scene_cpu_scope']='Counted owner scene validation/render/present service CPU; excludes startup/control JSON/reply/final snapshot/close. Foreground process_time excludes this separate owner. Whole-run combined CPU is reported by the scene A/B worker.'
        if sink.last_canvas is not None:
            saved=cv2.imwrite(str(output/'LAST_REPLAY_FRAME.png'),sink.last_canvas)
            if owner_scene and not saved:
                report['artifact_failure']='final owner scene PNG encoding/write failed'
                raise RuntimeError(report['artifact_failure'])
        cloud=(np.concatenate(observer._preview_chunks) if observer._preview_chunks else np.empty((0,3)))
        with (output/'PREVIEW_ONLY.ply').open('x',encoding='ascii') as f:
            f.write('ply\nformat ascii 1.0\ncomment REPLAY ASSUMED AXIS NO METROLOGY\n')
            f.write('element vertex %d\nproperty float x\nproperty float y\nproperty float z\nend_header\n'%len(cloud))
            np.savetxt(f,cloud,fmt='%.7f')
    except BaseException as exc:
        report.update(passed=False,failure=type(exc).__name__+': '+str(exc),
            completed_frames=len(rows),partial=True)
    finally:
        if writer is not None:
            try:report['unfinished_writer']=dataclasses.asdict(writer.close(timeout_s=10.))
            except BaseException as exc:report['writer_cleanup_error']=str(exc)
        if observer is not None:
            try:
                report['display']=observer.close(timeout_s=10.)
                if not report['display']['passed']:report['passed']=False
            except BaseException as exc:report.update(passed=False,display_cleanup_error=str(exc))
        elif remote is not None:
            try:remote.close()
            except BaseException as exc:report.update(passed=False,display_cleanup_error=str(exc))
        if (observer is not None and report.get('display',{}).get('closed')
                and getattr(observer.renderer,'point_raster',None) is not None):
            observer.renderer.point_raster.close()
            report['point_raster']=observer.renderer.point_raster.summary()
        if hardware_lock is not None:
            if remote is not None and remote.process.poll() is None:
                # The owner inherited the open file description. Retain it on
                # quarantine; never unlock while its CMA buffers remain live.
                report.update(passed=False,owner_still_alive=True,external_recovery_required=True)
                if hardware_lock._handle is not None:
                    hardware_lock._handle.close()
                    hardware_lock._handle=None
            else:
                hardware_lock.release()
                report['hardware_lock_released']=True
        report['full_run_elapsed_including_preparation_and_exports_s']=time.perf_counter()-initialized_at
        report['boundary']='Cached real Raw and software-modeled SLC1, production XYZ with unchanged strict geometry gate, assumed-axis nominal-angle preview stitch, CPU render and optional actual VDMA park ACK, asynchronous durable records. Full-chain elapsed includes writer drain, final display flush and confirmed owner close; initialization/preparation and PNG/PLY evidence exports excluded and separately timed. Actual rendered/ACK counts exclude warmup. Repeats add load, not angular coverage. No live acquisition or physical precision claim.'
        if ridge_replay:
            report['boundary']=report['boundary'].replace('software-modeled SLC1',
                'software-modeled SLC2/RPG1 with '+replay_policy+' (same fixed V4 policy, original Q12)')
        if owner_scene:
            report['boundary']+=' Saved-replay owner-scene opt-in: Dashboard/native rendering occurs only in the existing HDMI owner; final canvas fetch occurs once after display join, before owner close, and is included in full-chain elapsed. PNG encoding remains after that timed close.'
        _write_new(output/'RESULT.json',report)
    return report
