"""Shared finite scanning application: PL profiles, exposure, storage and rescan.

Imports are hardware-free. The offline fixture exercises these same production
callbacks; its synthetic geometry never constitutes physical calibration.
"""
from __future__ import annotations
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import threading
import time
import numpy as np

from .continuous_exposure import ExposureController
from .continuous_processor import ContinuousProcessor
from .continuous_timing import TimingBinder
from .stream_exposure import ExposureCoordinator
from .stream_session import StreamSession, StreamConfig
from .stream_writer import BoundedAsyncWriter, WriterFailure


def write_json_new(path, value):
    with Path(path).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.flush(); os.fsync(f.fileno())


class ScanApplication:
    """One capture epoch, one initial turn and at most one quality rescan.

    All controller changes occur in the scheduler's explicit drained barrier.
    Writer overload is a hard failure; accepted measurement frames never drop.
    """
    def __init__(self, *, calibration, stripe_config, selection, axis, volume,
                 timing_models, coordinator: ExposureCoordinator,
                 writer: BoundedAsyncWriter, source: str, processed_observer=None):
        if source not in {'synthetic_fixture', 'real_continuous_scan'}:
            raise ValueError('explicit source required')
        if processed_observer is not None and not callable(processed_observer):
            raise TypeError('processed_observer must be callable')
        self.calibration, self.stripe_config, self.selection = calibration, stripe_config, selection
        self.axis, self.volume, self.timing_models = axis, volume, dict(timing_models)
        self.coordinator, self.writer, self.source = coordinator, writer, source
        self.processed_observer = processed_observer
        self.processor = None
        self.previous_receipt = None
        self.last_request_id = None
        self.processing_seconds = []

    def timing_model(self, lines, revision):
        if lines not in self.timing_models:
            raise ValueError('target exposure timing evidence missing before SCCB transaction')
        return replace(self.timing_models[lines], exposure_revision=revision)

    def begin_motion(self, metadata, turn):
        if turn == 0:
            if self.processor is not None: raise RuntimeError('first motion already bound')
            ctrl = self.coordinator.controller
            binder = TimingBinder(capture_epoch=metadata['capture_epoch'], motor_epoch=metadata['motor_epoch'],
                motor_accepted_id=metadata['motor_accepted_id'], exposure_revision=ctrl.revision,
                model=self.timing_model(ctrl.lines, ctrl.revision))
            self.processor = ContinuousProcessor(calibration=self.calibration, stripe_config=self.stripe_config,
                selection=self.selection, mode='continuous', exposure_lines=ctrl.lines,
                exposure_revision=ctrl.revision, axis=self.axis, volume=self.volume,
                timing_binder=binder, exposure_controller=ctrl)
        elif turn == 1 and self.processor is not None and self.previous_receipt is not None:
            self.processor.begin_motion_epoch(capture_epoch=metadata['capture_epoch'],
                motor_epoch=metadata['motor_epoch'], motor_accepted_id=metadata['motor_accepted_id'],
                previous_receipt=self.previous_receipt, capture_joined=True,
                processing_drained=True, guard_was_revoked=True)
        else: raise RuntimeError('only the explicitly drained second motion epoch may follow')
        return True

    def process(self, capture):
        started = time.monotonic()
        if self.processor is None: raise RuntimeError('motion must be explicitly bound before processing')
        metadata = capture.motion_metadata or {}
        for field in ('effect_known', 'measurement_eligible', 'transition', 'exposure_revision', 'exposure_lines'):
            if field not in metadata: raise ValueError('coordinator exposure metadata missing: ' + field)
        result = self.processor.process(capture)
        trace=getattr(self,'performance_trace',None)
        if trace is not None:trace.produced(capture,result)
        summary = result.summary()
        summary.update(source=self.source, width=capture.diagnostics.width, height=capture.diagnostics.height)
        self.coordinator.observe(result.quality.status, capture.request_id)
        exposure = {k: metadata[k] for k in metadata if k.startswith('exposure_') or k in
                    {'effect_known','measurement_eligible','transition','observation_only','effect_evidence_sha256','effect_evidence_reference'}}
        if not capture.profile_payload: raise ValueError('original SLP2 payload required for persistence')
        self.writer.submit(dict(request_id=capture.request_id, capture_epoch=capture.epoch,
            raw=capture.raw, raw_sha256=capture.raw_sha256, profile_payload=capture.profile_payload,
            points_camera_mm=result.points_camera_mm[result.valid_points],
            points_axis_mm=result.points_axis_mm[result.valid_points] if result.fused else None,
            summary=summary, motion=metadata, exposure=exposure))
        self.last_request_id = capture.request_id
        # Persistence accepts the measurement before display/diagnostic work.
        # Observer errors propagate to the scheduler; a failed observer must
        # never silently turn into a successful scan or discard this record.
        if self.processed_observer is not None:
            self.processed_observer(capture, result)
        self.processing_seconds.append(time.monotonic()-started)
        return summary

    def exposure_pending(self): return self.coordinator.has_pending

    def apply_exposure(self, device, *, camera_idle):
        op = self.coordinator.pending
        if op is None: raise RuntimeError('no pending proposal')
        # Timing AND effect validation precede every sensor write.
        self.timing_model(op.lines, op.revision)
        if self.last_request_id is None: raise RuntimeError('no capture boundary')
        # A durable intent precedes any SCCB mutation. Owner remains free to
        # refresh its permit while this worker waits on disk/SCCB deadlines.
        self.writer.submit_event({'event':'exposure_intent','revision':op.revision,
                                  'lines':op.lines,'boundary':self.last_request_id})
        receipt = self.writer.flush(timeout_s=2.)
        if not receipt.durable: raise RuntimeError('exposure intent not durable; sensor unchanged')
        return self.coordinator.apply_at_boundary(device, self.last_request_id,
            capture_drained=True, processing_drained=True, camera_idle=camera_idle)

    def exposure_committed(self, change):
        if change.get('accepted') is not True: raise RuntimeError('exposure not accepted')
        self.processor.begin_exposure_revision(change['lines'], change['revision'],
            self.timing_model(change['lines'], change['revision']),
            capture_drained=True, processing_drained=True, camera_idle=True)
        self.writer.submit_event({'event':'exposure_committed', 'change': json.loads(json.dumps(change))})

    def turn_complete(self, receipt, turn, missing):
        self.previous_receipt = dict(receipt)
        self.writer.submit_event({'event':'rotation_complete','turn':turn,'motion_receipt':receipt,
                                  'missing_bins':list(missing), 'additional_turn_required':bool(missing)})


def finish_application(app, report, output, *, elapsed, height_bins=((0.,8.),(8.,16.),(16.,24.))):
    """A scan is successful only when every accepted record is durable."""
    try: receipt = app.writer.close(timeout_s=5.)
    except WriterFailure as exc: receipt = exc.receipt
    report.update(writer=asdict(receipt), source=app.source, elapsed_s=elapsed,
                  physical_accuracy_verified=False, measurements=[], cloud_points=0)
    if not receipt.durable or receipt.worker_unconfirmed:
        report['passed'] = False
        report['persistence_failure'] = receipt.error or 'writer completion not confirmed'
    report['exposure_history'] = json.loads(json.dumps(app.coordinator.history))
    spool=getattr(app,'cloud_spool',None)
    if not report.get('worker_unconfirmed'):
        try:
            if app.processor is not None:
                if spool is not None:
                    from candidate.formal_resources_r22 import export_cloud_ply
                    report['cloud_points']=spool.count
                    export_cloud_ply(spool,Path(output)/'cloud.ply',app.source)
                    spool.export_npy(Path(output)/'cloud.npy')
                    if report['passed']:
                        if getattr(app.processor,'supports_streamed_measurement',False) is True:
                            report['measurements']=[asdict(x) for x in app.processor.measure_diameters(height_bins_mm=height_bins)]
                            if any(x['reason']=='resource_scratch_budget' for x in report['measurements']):
                                report.update(passed=False,measurement_status='not_established_resource_budget')
                        elif spool.count*24>spool.snapshot_bytes:
                            report.update(passed=False,measurement_status='not_established_resource_budget',
                                          measurement_reason='complete cloud saved; bounded final feature analysis required')
                        else:
                            report['measurements']=[asdict(x) for x in app.processor.measure_diameters(height_bins_mm=height_bins)]
                else:
                    cloud=app.processor.snapshot().points_axis_mm
                    report['cloud_points']=len(cloud)
                    with (Path(output)/'cloud.ply').open('x',encoding='ascii') as f:
                        f.write('ply\nformat ascii 1.0\ncomment source '+app.source+'\n')
                        f.write('comment physical_accuracy_verified false\n')
                        f.write(f'element vertex {len(cloud)}\nproperty float x\nproperty float y\nproperty float z\nend_header\n')
                        np.savetxt(f,cloud,fmt='%.7f');f.flush();os.fsync(f.fileno())
                    if report['passed']:
                        report['measurements']=[asdict(x) for x in app.processor.measure_diameters(height_bins_mm=height_bins)]
        except BaseException as exc:
            report.update(passed=False,finalization_error=type(exc).__name__+': '+str(exc),
                          partial_evidence_preserved=True)
        finally:
            if spool is not None:
                try:
                    spool.close();report['cloud_spool_closed']=True
                except BaseException as exc:
                    report.update(passed=False,cloud_spool_closed=False,cloud_spool_error=str(exc))
    report['processing_s_median'] = float(np.median(app.processing_seconds)) if app.processing_seconds else None
    monitor=getattr(app,'resource_monitor',None)
    if monitor is not None:
        report['resource_monitor']=monitor.close(timeout_s=2.)
        r=report['resource_monitor']
        report['resource_goals_passed']=bool(r['worker_joined'] and not r['error'] and
            not r['stop_reason'] and r['memory_goal_passed'] and r['CPU_goal_passed'])
        if not report['resource_goals_passed']:report['passed']=False
    if getattr(app,'performance_trace',None) is not None:
        from candidate.formal_trace_r22 import finish_formal_trace
        finish_formal_trace(app,report,output)
    write_json_new(Path(output)/'RESULT.json', report)
    return report


def run_offline(output, *, scenario='weak_start', frames_per_turn=120, profile_hz=30,
                angle_bins=36, writer_sink=None, persistence='files'):
    from .stream_offline import SyntheticBackend, synthetic_components
    output = Path(output); output.mkdir(parents=True,exist_ok=False)
    components = synthetic_components()
    if persistence not in {'files','chunked'}:raise ValueError('unknown persistence backend')
    if persistence=='chunked':
        from .stream_chunk_writer import ChunkedAsyncWriter
        writer=ChunkedAsyncWriter(output/'records',capacity=16,sink=writer_sink,group_size=4,max_delay_s=.20)
    else:writer = BoundedAsyncWriter(output/'records', capacity=32, sink=writer_sink)
    coordinator = ExposureCoordinator(ExposureController(line_values=(30,60,120), initial_lines=60, hysteresis_frames=3),
        effect_models=components.pop('effect_models'))
    app = ScanApplication(**components, coordinator=coordinator, writer=writer, source='synthetic_fixture')
    backend = SyntheticBackend(app, scenario=scenario, frames_per_turn=frames_per_turn)
    start=time.monotonic()
    report=StreamSession(backend,app.process,config=StreamConfig(profile_hz=profile_hz,angle_bins=angle_bins)).run()
    report.update(virtual_motion=True, time_compressed=True, real_motor_commands=0,
                  synthetic_frame_dimensions=[32,24], scenario=scenario)
    return finish_application(app,report,output,elapsed=time.monotonic()-start)
