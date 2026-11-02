"""Formal continuous scan entry, plus hardware-free and no-laser checks.

scan requires real evidence and fresh powered-motion authority. This release
has no validated real axis/timing/volume bundle, so its supplied template is
deliberately PENDING. offline never loads PYNQ or operates a real device.
"""
from __future__ import annotations
import argparse, dataclasses, hashlib, json, signal, sys, threading, time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
for p in (ROOT/'src/python',ROOT.parent/'structured_light_system_geometry_v1/src/python',ROOT/'board/scripts'):
    if str(p) not in sys.path:sys.path.insert(0,str(p))
if ((ROOT.parent/'candidate').is_dir() or (ROOT.parent/'reference_candidate').is_dir()) and str(ROOT.parent) not in sys.path:
    sys.path.insert(0,str(ROOT.parent))


def preflight(args):
    from structured_light.scan3d.stream_bundle import load_scan_bundle
    bundle=load_scan_bundle(args.bundle)
    manifest=json.loads(Path(args.manifest).read_text())
    # The runtime additionally verifies every artifact hash and the loaded
    # overlay receipt. This check runs before importing hardware libraries.
    if manifest.get('frame_motion_metadata',{}).get('magic')!=0x43534d31:
        raise ValueError('CSM1 manifest required')
    return bundle


def _close_scan_writer(writer,report):
    """Join persistence before hardware cleanup; retain a failed sink receipt."""
    from structured_light.scan3d.stream_writer import WriterFailure
    try:
        receipt=writer.close(timeout_s=5.)
    except WriterFailure as exc:
        receipt=exc.receipt
    except BaseException as exc:
        report.update(passed=False,writer_close_error=type(exc).__name__+': '+str(exc))
        return False
    report['writer_before_runtime_cleanup']=dataclasses.asdict(receipt)
    joined=receipt.worker_joined is True and receipt.worker_unconfirmed is False
    if not joined or not receipt.durable:
        report.update(passed=False,persistence_failure=receipt.error or 'writer completion not confirmed')
    return joined


def _restore_row_feature_mode(runtime,report,*,generation,workers_joined,writer_joined):
    """Only the joined, current owner may restore mode before scope exit."""
    mode=report['row_feature_dma']
    restore_required=getattr(runtime.transport,'feature_mode_restore_required',False)
    if not mode['enabled'] and not restore_required:
        return
    if workers_joined is not True or writer_joined is not True:
        mode['restore_status']='skipped_unjoined_workers'
        report.update(passed=False,external_recovery_required=True)
        return
    session=runtime.session
    if generation is None or session._poisoned or getattr(session,'_closed',False):
        mode['restore_status']='skipped_invalid_generation'
        report.update(passed=False,external_recovery_required=True)
        return
    try:
        session._gate.check(generation)
        status=runtime.controller.get_status()
        if (status.busy is not False or status.packer_busy is not False or status.error_flags
                or runtime.controller._inflight is not None):
            raise RuntimeError('feature mode restore requires an idle, error-free camera')
        session._gate.check(generation)
        if getattr(runtime.transport,'compact_policy_dma_enabled',False) or restore_required:
            session._gate.call(generation,runtime.transport.set_feature_mode,0,workers_joined=True)
        else:
            session._gate.call(generation,runtime.transport.set_row_feature_mode,False,workers_joined=True)
        mode.update(restore_status='restored',restored_mode_zero=True)
    except BaseException as exc:
        mode.update(restore_status='unconfirmed',restore_error=type(exc).__name__+': '+str(exc))
        report.update(passed=False,external_recovery_required=True)


def run_scan(args):
    run_started_perf=time.perf_counter()
    from structured_light.scan3d.stream_application import ScanApplication, finish_application, write_json_new
    from structured_light.scan3d.stream_backend import RuntimeContinuousBackend, ContinuousAdmission, GenerationCheckedMMIO
    from structured_light.scan3d.stream_exposure import ExposureCoordinator
    from structured_light.scan3d.continuous_exposure import ExposureController
    from structured_light.scan3d.stream_chunk_writer import ChunkedAsyncWriter
    from structured_light.scan3d.stream_session import StreamSession,StreamConfig
    bundle=preflight(args)
    manifest=json.loads(Path(args.manifest).read_text())
    use_pfo3=getattr(args,'processor','pl_final')=='pl_final'
    if use_pfo3 and manifest.get('pfo3',{}).get('enabled') is not True:
        raise ValueError('PL-final mode requires an explicit PFO3 firmware manifest')
    if not use_pfo3 and manifest.get('pfo3',{}).get('enabled') is True:
        raise ValueError('PFO3 production firmware cannot silently use an ARM image backend')
    body_validator=None
    if getattr(args,'pfo3_validator_manifest',None) is not None:
        if not use_pfo3:raise ValueError('PFO3 native validator requires PL-final mode')
        from candidate.pfo3_decode_native_r25 import NativeBodyValidatorR25
        body_validator=NativeBodyValidatorR25.from_manifest(args.pfo3_validator_manifest)
    if args.validate_only:
        return dict(passed=True,source='preflight_only',motion_authorized=False,
            bundle_sha256=bundle.sha256,verified_files=list(bundle.verified_files),physical_accuracy_verified=False)
    if args.motion_proof is None or args.receipt is None or args.output is None:
        raise ValueError('scan requires --motion-proof, --receipt and a new --output')
    authority=ContinuousAdmission(**json.loads(Path(args.motion_proof).read_text()))
    authority.validate(boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),bit_sha256=bundle.bit_sha256)
    # All validations above precede runtime construction or hardware access.
    from structured_light.motion import capture_handoff
    from structured_light.motion.pynq_runtime import create_stage_b_runtime
    from structured_light.scan3d.stream_capture import FeaturePynqTransport,exclusive_capture_generation
    from structured_light.p2.camera_exposure import SCCBAccess,manual_plan
    from continuous_board import close_static_resources
    from dataclasses import replace
    import math
    stream_config=StreamConfig(max_turns=authority.pulse_budget//3200,
                              profile_hz=getattr(args,'profile_hz',12.5))
    output=Path(args.output)
    resource_admission=None
    if getattr(args,'processor','pl_final') in ('multibranch','pl_final'):
        from candidate.formal_resources_r22 import admit_formal_storage
        from candidate.storage_r21 import MultiBranchChunkWriter
        max_frames=(math.ceil(stream_config.task_timeout_s*stream_config.profile_hz)+2)*stream_config.max_turns
        storage_budget,resource_admission=admit_formal_storage(output,max_frames=max_frames,
            data_root=getattr(args,'data_root',None),frame_bound_bytes=(32 if use_pfo3 else 4)*1024*1024)
        output.mkdir(parents=True,exist_ok=False)
        from candidate.io_policy_r25 import DEFAULT_IO_POLICY
        writer=MultiBranchChunkWriter(output/'records',capacity=DEFAULT_IO_POLICY.capacity,
            group_size=DEFAULT_IO_POLICY.group_size,max_delay_s=DEFAULT_IO_POLICY.max_delay_s,
            byte_capacity=DEFAULT_IO_POLICY.byte_capacity,
            raw_codec=getattr(args,'raw_codec','zlib1'),storage_budget=storage_budget)
    else:
        output.mkdir(parents=True,exist_ok=False)
        writer=ChunkedAsyncWriter(output/'records',capacity=16,group_size=4,max_delay_s=.40)
    ctrl=ExposureController(line_values=tuple(sorted(bundle.timing_models)),initial_lines=bundle.initial_exposure_lines)
    coordinator=ExposureCoordinator(ctrl,effect_models=bundle.effect_models)
    application_class=ScanApplication
    if use_pfo3:
        from candidate.formal_pfo3_r25 import application_type
        application_class=application_type()
    elif getattr(args,'processor','pl_final')=='multibranch':
        from continuous_multibranch import MultiBranchScanApplication
        application_class=MultiBranchScanApplication
    try:
        app=application_class(calibration=bundle.calibration,stripe_config=bundle.stripe_config,
            selection=bundle.selection,axis=bundle.axis,volume=bundle.volume,
            timing_models=bundle.timing_models,coordinator=coordinator,writer=writer,source='real_continuous_scan')
    except BaseException:
        writer.close(timeout_s=5.)
        raise
    from candidate.formal_trace_r22 import FormalTrace
    performance_trace=FormalTrace(evidence_type='REAL_BOARD_COMPLETE_CHAIN',run_started=run_started_perf)
    app.performance_trace=performance_trace
    writer.performance_trace=performance_trace
    report={'passed':False,'worker_unconfirmed':False,'failure':None,'motion_receipts':[],'profiles':[]}
    feature_mode={'enabled':False,'format':'SLP2','admitted':False,
                  'manifest_declared':'row_feature_dma' in manifest}
    generation=None;session_started=False;session_completed=False;resource_monitor=None
    display=None
    rt=scope=access=pl_frontend=None;original={};trace=[];sensor_identity_verified=False;started=time.monotonic()
    stop=threading.Event();handlers={}
    performance_trace.stop=stop
    try:
        transport_class=FeaturePynqTransport
        if manifest.get('multi_candidate_dma',{}).get('enabled') is True:
            if getattr(args,'processor','multibranch')!='multibranch':
                raise ValueError('PMC2 requires the multi-candidate processor')
            from candidate.pmc2_transport_r22 import Pmc2PynqTransport
            transport_class=Pmc2PynqTransport
        capture_handoff.HandoffPynqTransport=transport_class
        rt=create_stage_b_runtime(args.manifest,stage='static',load=False,attach=True,
            loaded_overlay_receipt=args.receipt,output=output/'runtime',no_display=True,
            threshold=bundle.stripe_config.fixed_threshold,stripe_roi=bundle.stripe_config.roi)
        if rt.artifacts['files']['bit']['sha256']!=bundle.bit_sha256: raise ValueError('bundle/loaded bit mismatch')
        if getattr(args,'hdmi',False):
            import os
            from structured_light.p2.hdmi_owner import RemoteHDMIOutput
            from structured_light.scan3d.processed_display import ProcessedObserver
            from structured_light.scan3d.dashboard import DashboardRenderer
            os.environ['SL_HDMI_PROCESSED_CANVAS']='1'
            sink=RemoteHDMIOutput(args.manifest,owner_log=output/'hdmi_owner.log',
                                  timeout_s=5.,startup_timeout_s=60.,preview_only=True,
                                  hardware_lock_fd=rt.hardware_lock._handle.fileno(),shared_canvas=True)
            # Install ownership before configure so any startup failure still
            # follows the normal joined display cleanup path.
            display=ProcessedObserver(source='real_continuous_scan',sink=sink,asynchronous=True,capacity=1,
                renderer=DashboardRenderer(point_raster_manifest=getattr(args,'raster_manifest',None)),
                performance_trace=performance_trace,trace_hdmi_hardware=True)
            sink.configure()
            def observe_processed(capture,result):
                display.processor=app.processor
                return display.consume(capture,result)
            app.processed_observer=observe_processed
        rt.transport.frame_motion_metadata_supported=True
        # Acquire exclusive ownership before the first opt-in mode write.
        # Failed/poisoned/busy scope admission must leave legacy mode untouched.
        pending_scope=exclusive_capture_generation(rt);pending_scope.__enter__()
        scope=pending_scope
        generation=rt.session._gate.generation
        if use_pfo3:
            from candidate.pfo3_transport_r25 import ActualPLTransportR25
            pl_frontend=ActualPLTransportR25(rt.overlay,manifest={**manifest,
                'bit_absolute':str(rt.artifacts['bit'])},hardware_lock=rt.hardware_lock,body_validator=body_validator)
            app.pl_transport=pl_frontend
            from candidate.pfo3_exception_r25 import PFO3ExceptionCoordinatorR25
            app.bind_exception_coordinator(PFO3ExceptionCoordinatorR25(output/'pfo3_exceptions',writer.storage_budget))
            feature_mode.update(format='PFO3',admitted=True,
                input_path='same-frame camera Raw -> DDR -> actual PL; original SLP2 retained as source only')
        if manifest.get('multi_candidate_dma',{}).get('enabled') is True:
            feature_mode['admission_attempted']=True
            admitted=rt.session._gate.call(generation,rt.transport.admit_pmc2_dma,args.manifest,
                stripe_config=bundle.stripe_config,cfg_version=0,exposure_version=ctrl.revision,workers_joined=True)
            if not admitted or rt.transport.pmc2_dma_enabled is not True:
                raise RuntimeError('PMC2 admission did not confirm mode6')
            feature_mode.update(enabled=True,format='PMC2',admitted=True)
        elif manifest.get('compact_policy_dma',{}).get('enabled') is True:
            feature_mode['admission_attempted']=True
            admitted=rt.session._gate.call(generation,rt.transport.admit_compact_policy_dma,args.manifest,workers_joined=True)
            if not admitted or rt.transport.compact_policy_dma_enabled is not True:
                raise RuntimeError('compact policy DMA admission did not confirm mode2')
            feature_mode.update(enabled=True,format='SLC1',admitted=True)
        elif 'row_feature_dma' in manifest:
            feature_mode['admission_attempted']=True
            admitted=rt.transport.admit_row_feature_dma(args.manifest,workers_joined=True)
            if admitted:
                if rt.transport.row_feature_dma_enabled is not True:
                    raise RuntimeError('row feature DMA admission did not confirm mode1')
                feature_mode.update(enabled=True,format='SLF1',admitted=True)
        access=SCCBAccess(GenerationCheckedMMIO(rt.transport._diag_mmio,rt.session._gate),trace,lambda:None)
        sensor={reg:access.read(reg) for reg in (0,7,15,175,53,11,213)}
        if sensor[0]!=bundle.sensor_id:raise ValueError('actual sensor identity mismatch')
        sensor_identity_verified=True
        original={reg:sensor[reg] for reg in (175,53,11)}
        manual_plan(sensor,coarse_rows=bundle.initial_exposure_lines,gain_code=16)
        if sensor[175]&3 or sensor[53]!=16 or sensor[11]!=bundle.initial_exposure_lines:
            raise ValueError('initial manual exposure/gain does not match validated bundle')
        params=rt.controller._centroid_parameters()
        expected=dict(threshold=60,min_width=bundle.stripe_config.min_width,max_width=bundle.stripe_config.max_width,
                      min_sum=bundle.stripe_config.min_sum,min_peak=bundle.stripe_config.min_peak)
        if params!=expected or rt.controller._roi()!=bundle.stripe_config.roi:
            raise ValueError('actual PL configuration does not match validated feature configuration')
        # Validate the full processor before the first ARM; actual capture and
        # motor identities are bound later, at the first matching CSM1 frame.
        from structured_light.scan3d.continuous_processor import ContinuousProcessor
        processor_class=ContinuousProcessor
        if use_pfo3:
            from candidate.pfo3_processor_r25 import PLFinalProcessorR25
            processor_class=PLFinalProcessorR25
        elif getattr(args,'processor','pl_final')=='multibranch':
            from structured_light.scan3d.multibranch_processor_r21 import MultiBranchProcessor
            processor_class=MultiBranchProcessor
        processor_class(calibration=bundle.calibration,stripe_config=bundle.stripe_config,
            selection=bundle.selection,mode='continuous',exposure_lines=ctrl.lines,exposure_revision=0,
            axis=bundle.axis,volume=bundle.volume)
        if resource_admission is not None:
            from candidate.runtime_monitor_r22 import RuntimeMonitor
            resource_monitor=RuntimeMonitor(output/'resource_samples.jsonl',
                storage_budget=writer.storage_budget,scheduler_stop=stop)
            app.resource_monitor=resource_monitor
            resource_monitor.start()
        class Backend(RuntimeContinuousBackend):
            def move(self,count,rpm):
                performance_trace.activity_begin()
                try:return super().move(count,rpm)
                finally:performance_trace.activity_finish()
            def record_prestart(self,capture):
                exposure=coordinator.capture_metadata(capture.request_id)
                writer.submit(dict(request_id=capture.request_id,capture_epoch=capture.epoch,
                    raw=capture.raw,raw_sha256=capture.raw_sha256,profile_payload=capture.profile_payload,
                    points_camera_mm=None,points_axis_mm=None,
                    summary={'valid':False,'fused':False,'reason':'before_registered_motion_window',
                             'source':'real_continuous_scan','physical_accuracy_verified':False,
                             'width':capture.diagnostics.width,'height':capture.diagnostics.height},
                    motion=capture.motion_metadata,exposure=exposure))
            def capture(self):
                capture=super().capture()
                metadata={**capture.motion_metadata,**coordinator.capture_metadata(capture.request_id)}
                if use_pfo3:
                    metadata.update(feature_config_revision=0,exposure_start_ticks=0,exposure_end_ticks=0,
                        pfo3_exposure_timestamp_basis='not directly measured; CSM1 FV and validated PS timing model retained')
                return replace(capture,motion_metadata=metadata)
            def exposure_pending(self):return app.exposure_pending()
            def apply_exposure(self):
                status=rt.controller.get_status()
                idle=(status.busy is False and status.packer_busy is False and status.error_flags==0
                      and rt.controller._inflight is None)
                return app.apply_exposure(access,camera_idle=idle)
            def exposure_committed(self,change):
                app.exposure_committed(change)
                self.frame_config.update(exposure_lines=change['lines'],exposure_revision=change['revision'])
                if getattr(rt.transport,'pmc2_dma_enabled',False):
                    rt.session._gate.call(generation,rt.transport.configure_pmc2,bundle.stripe_config,
                        cfg_version=0,exposure_version=change['revision'],workers_joined=True)
            def turn_complete(self,*values):app.turn_complete(*values)
        backend=Backend(rt,admission=authority,frame_config=dict(stripe_config_fingerprint=bundle.stripe_config.fingerprint,
            exposure_lines=ctrl.lines,exposure_revision=0),verify_evidence=lambda: preflight(args).sha256==bundle.sha256,
            begin_motion=app.begin_motion)
        for sig in (signal.SIGINT,signal.SIGTERM):
            handlers[sig]=signal.signal(sig,lambda signum,frame:stop.set())
        session_started=True
        report=StreamSession(backend,app.process,config=stream_config,stop_event=stop).run()
        session_completed=True
        report['bundle_sha256']=bundle.sha256
        report['prestart_frames']=backend.prestart_frames
    except BaseException as exc:
        report.update(passed=False,failure={'type':type(exc).__name__,'message':str(exc)})
    finally:
        for sig,handler in handlers.items():signal.signal(sig,handler)
        report['row_feature_dma']=feature_mode
        workers_joined=not session_started or (session_completed and report.get('worker_unconfirmed') is False)
        writer_joined=_close_scan_writer(writer,report)
        display_joined=True
        if display is not None:
            try:
                report['display']=display.close(timeout_s=10.)
                display_joined=report['display']['worker_unconfirmed'] is False
                if not report['display']['passed']:report['passed']=False
            except BaseException as exc:
                report.update(passed=False,display_cleanup_error=type(exc).__name__+': '+str(exc))
                display_joined=False
        cleanup_worker_unconfirmed=not workers_joined or not writer_joined or not display_joined
        if pl_frontend is not None:
            try:
                pl_frontend.close()
                report['pfo3_cleanup']={'buffers_released':True,'dma_quiescent':True}
            except BaseException as exc:
                cleanup_worker_unconfirmed=True
                report['pfo3_cleanup']={'buffers_released':False,'error':str(exc)}
        if cleanup_worker_unconfirmed:
            report.update(passed=False,worker_unconfirmed=True,external_recovery_required=True)
        report['sensor_restore']={'verified':False,'writes':[]}
        sccb_failed=coordinator.controller.failure is not None or any('error' in x for x in trace)
        if sccb_failed:
            report.update(passed=False,external_recovery_required=True)
            report['sensor_restore']['skipped']='SCCB failure: partial state retained; no automatic retries'
        elif access is not None and sensor_identity_verified and not cleanup_worker_unconfirmed:
            try:
                for reg,value in original.items():
                    access.write_verified(reg,value);report['sensor_restore']['writes'].append({'register':reg,'value':value})
                report['sensor_restore']['verified']=all(access.read(k)==v for k,v in original.items())
            except BaseException as exc:report['sensor_restore']['error']=str(exc)
            if not report['sensor_restore']['verified']:report['passed']=False
        if rt is not None:
            _restore_row_feature_mode(rt,report,generation=generation,
                workers_joined=workers_joined and display_joined,writer_joined=writer_joined)
        try:
            close_static_resources(report,scope=scope,display=None,runtime=rt,profiler=None,output=output,
                                   worker_unconfirmed=cleanup_worker_unconfirmed)
        except BaseException as exc:
            report.update(passed=False,worker_unconfirmed=True,external_recovery_required=True,
                          runtime_cleanup_error=type(exc).__name__+': '+str(exc))
        report['sensor_trace']=trace
        report['resource_admission']=resource_admission
        report['requested_profile_hz']=stream_config.profile_hz
        exception=getattr(app,'exception_coordinator',None)
        if exception is not None:
            report['pfo3_exception']={'enabled':True,'CPU_calls':exception.cpu_calls,
                'stopped':exception.stopped,'CPU_worker_outstanding':exception.fallback._busy,
                'consecutive':exception.fallback.consecutive,'CPU_deadline_s':.5,
                'max_outstanding':1,'max_consecutive':3,
                'CPU_results_are_PL_success':False}
        if pl_frontend is not None and not pl_frontend.closed:
            from tools.board_replay_r22 import quarantine_owner
            try:write_json_new(output/'PFO3_QUARANTINE.json',{'retained_lock':True,'external_recovery_required':True})
            except BaseException:pass
            quarantine_owner(output)
    try:return finish_application(app,report,output,elapsed=time.monotonic()-started,height_bins=bundle.height_bins_mm)
    except BaseException as exc:
        if resource_monitor is not None:
            report['resource_monitor']=resource_monitor.close(timeout_s=2.)
        report.update(passed=False,finalization_error=type(exc).__name__+': '+str(exc),stage='post_runtime',
            physical_accuracy_verified=False)
        return report


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='mode',required=True)
    offline=sub.add_parser('offline');offline.add_argument('--output',type=Path,required=True)
    offline.add_argument('--scenario',choices=('normal','weak_start','occluded','capture_fault','saturated_start'),default='weak_start')
    offline.add_argument('--frames-per-turn',type=int,default=120);offline.add_argument('--angle-bins',type=int,default=60)
    offline.add_argument('--persistence',choices=('files','chunked'),default='chunked')
    scan=sub.add_parser('scan');scan.add_argument('--bundle',type=Path,required=True);scan.add_argument('--manifest',type=Path,required=True)
    scan.add_argument('--output',type=Path);scan.add_argument('--motion-proof',type=Path);scan.add_argument('--receipt',type=Path)
    scan.add_argument('--validate-only',action='store_true')
    scan.add_argument('--processor',choices=('pl_final','multibranch','single'),default='pl_final',
                      help='PL-final default; legacy PS modes require legacy firmware and remain explicit')
    scan.add_argument('--hdmi',action='store_true',help='display processed XYZ through bounded independent HDMI owner')
    scan.add_argument('--data-root',type=Path,help='managed current-data namespace; default output parent')
    scan.add_argument('--raw-codec',choices=('none','zlib1'),default='zlib1')
    scan.add_argument('--pfo3-validator-manifest',type=Path,help='explicit source/runtime/QA-bound native body validator')
    scan.add_argument('--raster-manifest',type=Path,help='explicit pixel-QA-bound native point renderer')
    scan.add_argument('--profile-hz',type=float,default=12.5,
                      help='capture scheduling target; measured useful/durable rate is separately verified')
    replay=sub.add_parser('replay',help='saved real Raw -> modeled SLC1 -> production XYZ -> assumed-axis preview; no motor')
    for name in ('dataset','camera','plane','axis','output'):
        replay.add_argument('--'+name,type=Path,required=True)
    replay.add_argument('--repeats',type=int,default=20)
    replay.add_argument('--hdmi-manifest',type=Path)
    replay.add_argument('--no-persist',action='store_true')
    args=p.parse_args(argv)
    try:
        if args.mode=='offline':
            from structured_light.scan3d.stream_application import run_offline
            result=run_offline(args.output,scenario=args.scenario,frames_per_turn=args.frames_per_turn,
                angle_bins=args.angle_bins,persistence=args.persistence)
        elif args.mode=='replay':
            from structured_light.scan3d.processed_replay_bench import run_replay
            result=run_replay(dataset=args.dataset,camera=args.camera,plane=args.plane,axis=args.axis,
                output=args.output,repeats=args.repeats,hdmi_manifest=args.hdmi_manifest,persist=not args.no_persist)
        else: result=run_scan(args)
    except Exception as exc:
        print(json.dumps({'passed':False,'stage':'entry_error','error':type(exc).__name__+': '+str(exc),
                          'real_motor_commands':None,'physical_accuracy_verified':False},ensure_ascii=False))
        return 2
    print(json.dumps({k:v for k,v in result.items() if k not in ('profiles','sensor_trace')},ensure_ascii=False,default=str))
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
