"""Finite stationary Raw/SLC2 pixel benchmark; no motion or metric geometry.

The existing stage-static artifact, handoff, guard and generation lifecycle is
retained. Only camera capture START is permitted. HDMI ACKs come from the
separate allocation owner. Tests inject every hardware-facing dependency.
"""
from pathlib import Path
from types import SimpleNamespace
from collections import Counter
from dataclasses import dataclass, asdict
import argparse
import hashlib
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT/'src/python', ROOT.parent/'structured_light_system_geometry_v1/src/python', ROOT/'board/scripts'):
    if str(_path) not in sys.path: sys.path.insert(0, str(_path))

import cv2
import numpy as np
from structured_light.scan3d.processed_display import ProcessedObserver

REGISTERS = (0, 1, 2, 3, 4, 5, 6, 7, 0x0D, 0x0F, 0x35, 0x0B, 0xAF, 0xD5)
FIXED_REGISTERS = (0, 1, 2, 3, 4, 5, 6, 7, 0x0D, 0x0F, 0xD5)
BANNER = 'LIVE V4 PIXEL / CALIBRATION PENDING / NO mm'
PLOT = (824, 128, 416, 480)
_QUARANTINED = []  # Strong references; main keeps this process alive on unconfirmed cleanup.


def save_new(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())


def _text(canvas, text, x, y, scale=.48, color=(190, 210, 225)):
    cv2.putText(canvas, str(text), (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


def pixel_background(roi):
    canvas = np.zeros((720, 1280, 3), np.uint8)
    canvas[:] = (25, 19, 12)
    _text(canvas, BANNER, 24, 32, .72, (80, 210, 255))
    _text(canvas, 'LIVE RAW GRAY / NATIVE 752 x 480', 24, 112)
    _text(canvas, 'SAME-FRAME ROI / u,v PIXELS', 824, 112)
    _text(canvas, f'ROI {tuple(roi)} / GREEN ACCEPT / ORANGE RETAINED REJECT', 24, 642)
    _text(canvas, 'LASER STATE UNVERIFIED / MOTOR 12V OFF (OPERATOR) / NO XYZ', 24, 704)
    return canvas


def render_pixel_canvas(raw, stripe, roi, request_id, transition, *, background=None):
    if not isinstance(raw, bytes) or len(raw) != 752*480:
        raise ValueError('pixel display requires immutable native 752x480 Raw')
    if (stripe.center_q12.shape != (480,) or stripe.accepted.shape != (480,)
            or stripe.retained.shape != (480,) or len(stripe.reasons) != 480):
        raise ValueError('pixel display requires complete same-frame selected rows')
    canvas = (background if background is not None else pixel_background(roi)).copy()
    gray = np.frombuffer(raw, np.uint8).reshape(480, 752)
    cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR, dst=canvas[128:608, 24:776])
    x0, y0, x1, y1 = roi
    px, py, width, height = PLOT
    crop = cv2.resize(gray[y0:y1, x0:x1], (width, height), interpolation=cv2.INTER_NEAREST)
    cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR, dst=canvas[py:py+height, px:px+width])
    for mask, color in ((stripe.retained & ~stripe.accepted, (0, 165, 255)),
                        (stripe.accepted, (0, 255, 0))):
        rows = np.flatnonzero(mask)
        if np.any((rows < y0) | (rows >= y1)):
            raise ValueError('display pixel sample outside admitted ROI')
        u = stripe.center_q12[rows]/4096.
        if np.any((u < x0) | (u >= x1)):
            raise ValueError('display center outside admitted ROI')
        xs = np.clip(np.rint((u-x0)*width/(x1-x0)).astype(np.intp), 1, width-2)+px
        ys = np.clip(((rows-y0)*height/(y1-y0)).astype(np.intp), 0, height-2)+py
        for dy in (0, 1):
            for dx in (-1, 0, 1): canvas[ys+dy, xs+dx] = color
    reasons = Counter(reason for reason in stripe.reasons[y0:y1] if reason is not None)
    _text(canvas, f'REQUEST {request_id} / ACCEPT {int(stripe.accepted.sum())} / '
        f'RETAINED REJECT {int((stripe.retained & ~stripe.accepted).sum())}', 24, 76, .58)
    _text(canvas, 'EXPOSURE TRANSITION / EXCLUDED FROM STATISTICS' if transition else
        'STATIONARY PIXEL OBSERVATION / NOT METROLOGY', 24, 670, .5, (80, 210, 255))
    for index, (reason, count) in enumerate(reasons.most_common(3)):
        _text(canvas, f'{reason}: {count}', 824, 48+18*index, .42, (0, 190, 255))
    return canvas


@dataclass(frozen=True)
class PixelResult:
    request_id: int
    capture_epoch: int
    stripe: object
    roi: tuple
    transition: bool


class ConfirmedSink:
    def __init__(self, remote): self.remote = remote
    def show_canvas(self, canvas):
        receipt = self.remote.show_canvas(canvas)
        if not isinstance(receipt, dict) or receipt.get('hdmi_acknowledged') is not True:
            raise RuntimeError('HDMI actual current-store acknowledgement missing')
        return receipt
    def close(self):
        receipt = self.remote.close()
        if (not isinstance(receipt, dict) or receipt.get('closed_confirmed') is not True
                or receipt.get('owner_exited') is not True or receipt.get('returncode', 0) != 0):
            raise RuntimeError('HDMI owner close/exit acknowledgement missing')
        return receipt


class PixelObserver(ProcessedObserver):
    """Use the tested latest queue and ownership close, with pixel-only data."""
    def __init__(self, remote, roi):
        self._pixel_background = pixel_background(roi)
        super().__init__(source='live_static', sink=ConfirmedSink(remote), renderer=object(),
                         asynchronous=True, capacity=1)
        self._title = BANNER
    def _points(self, result, preview_angle):
        return (None, ('pixel', result.capture_epoch, result.request_id, self._counts['submitted']),
                None, 'same_frame_pixel', 0.)
    def _consume(self, item):
        capture, result, _, queued_at, _ = item[:5]
        started = time.perf_counter()
        canvas = render_pixel_canvas(capture.raw, result.stripe, result.roi, capture.request_id,
            result.transition, background=self._pixel_background)
        rendered = time.perf_counter()
        rejection = dict(Counter(reason for reason in result.stripe.reasons if reason not in (None, 'roi_excluded')))
        with self._condition: self._counts['rendered'] += 1
        sink_receipt = self.sink.show_canvas(canvas)
        finished = time.perf_counter()
        receipt = dict(request_id=capture.request_id, capture_epoch=capture.epoch,
            submission_sequence=item[8], cloud_revision=item[5][1], source='live_static_pixel',
            title=BANNER, rendered=True, acknowledged=True, hdmi_acknowledged=True,
            transition=result.transition, rejection_counts=rejection,
            accepted_rows=int(result.stripe.accepted.sum()), measurement_eligible=False,
            xyz_computed=False, physical_accuracy_verified=False,
            queue_wait_s=started-queued_at, render_s=rendered-started,
            show_s=finished-rendered, total_s=finished-started,
            submit_to_ack_s=finished-queued_at, sink_receipt=sink_receipt)
        with self._condition:
            self._counts['acknowledged'] += 1; self._counts['hdmi_acknowledged'] += 1
            self._records.append(receipt)
            self._last_rendered = dict(receipt, canvas=canvas)
        return receipt


def runtime_factory(manifest, **kwargs):
    from structured_light.motion import capture_handoff
    from structured_light.motion.pynq_runtime import create_stage_b_runtime
    from structured_light.scan3d.stream_ridge import RidgePynqTransport
    previous = capture_handoff.HandoffPynqTransport
    capture_handoff.HandoffPynqTransport = RidgePynqTransport
    try:
        runtime = create_stage_b_runtime(manifest, stage='static', load=kwargs['load'],
            attach=not kwargs['load'], power_off_confirmation=kwargs['power_off_proof'],
            loaded_overlay_receipt=kwargs['receipt'], output=kwargs['output'],
            no_display=True, threshold=60, stripe_roi=kwargs['quality_roi'])
    finally:
        capture_handoff.HandoffPynqTransport = previous
    runtime.transport.frame_motion_metadata_supported = True
    return runtime


def qa_frame(frame, stripe, config, selection):
    from structured_light.scan3d.stream_ridge import compare_ridge_raw
    from structured_light.stripe_v1.candidates import extract_candidates
    from structured_light.stripe_v1.selection_v4 import select_candidates_v4
    gray = np.frombuffer(frame.raw, np.uint8).reshape(480, 752)
    comparison = compare_ridge_raw(frame.compact_packet, gray)
    if not comparison['passed']: raise RuntimeError('PL ridge flags differ from independent Raw ridge QA')
    slow = select_candidates_v4(gray, extract_candidates(gray, config), config, selection)
    np.testing.assert_array_equal(stripe.center_q12, [r.candidate.center_q12 if r.candidate else 0 for r in slow.rows])
    np.testing.assert_array_equal(stripe.accepted, [r.candidate is not None and r.reason is None for r in slow.rows])
    if stripe.reasons != tuple(r.reason for r in slow.rows): raise RuntimeError('fast/slow V4 reason mismatch')
    return dict(passed=True, request_id=frame.request_id, capture_epoch=frame.epoch,
        ridge_raw_rows_checked=comparison['checked_rows'], slow_v4_exact=True,
        v4_accepted_rows=int(stripe.accepted.sum()),
        old_v3_accepted_rows=sum(r.candidate is not None and r.v3_reason is None for r in slow.rows),
        modeled_slc2=False, source='sampled_same_capture_Raw_and_received_SLC2', measurement_eligible=False)


def dependencies(source):
    for path in (source/'src/python', source/'board/scripts'):
        if str(path) not in sys.path: sys.path.insert(0, str(path))
    import continuous_no_laser as base
    from continuous_board import close_static_resources
    from continuous_scan import _restore_row_feature_mode
    from structured_light.scan3d.stream_capture import capture_features, exclusive_capture_generation
    from structured_light.scan3d.stream_backend import GenerationCheckedMMIO
    from structured_light.scan3d.stream_chunk_writer import ChunkedAsyncWriter
    from structured_light.scan3d.ridge_candidates import select_ridge_candidates
    from structured_light.p2.camera_exposure import manual_plan
    from structured_light.p2.camera_exposure_trace_r13 import TracedSCCBAccess
    def display_factory(runtime, manifest, output, *, shared_canvas=False):
        from structured_light.p2.hdmi_owner import RemoteHDMIOutput
        previous = os.environ.get('SL_HDMI_PROCESSED_CANVAS')
        try:
            os.environ['SL_HDMI_PROCESSED_CANVAS'] = '1'
            options = {'shared_canvas':True} if shared_canvas else {}
            return RemoteHDMIOutput(manifest, owner_log=output/'HDMI_OWNER.log', timeout_s=5.,
                startup_timeout_s=60., preview_only=True, hardware_lock_fd=runtime.hardware_lock._handle.fileno(), **options)
        finally:
            if previous is None: os.environ.pop('SL_HDMI_PROCESSED_CANVAS', None)
            else: os.environ['SL_HDMI_PROCESSED_CANVAS'] = previous
    return SimpleNamespace(base=base, runtime_factory=runtime_factory, close=close_static_resources,
        restore_mode=_restore_row_feature_mode, capture=capture_features, scope=exclusive_capture_generation,
        gated_mmio=GenerationCheckedMMIO, access=TracedSCCBAccess, manual_plan=manual_plan,
        writer_factory=ChunkedAsyncWriter, display_factory=display_factory,
        select=select_ridge_candidates, qa=qa_frame)


def run_bench(args, *, deps=None):
    if type(args.frames) is not int or not 3 <= args.frames <= 1000:
        raise ValueError('frames must be an integer in 3..1000')
    if type(args.exposure_lines) is not int or not 1 <= args.exposure_lines <= 32765:
        raise ValueError('exposure-lines must be an integer in 1..32765')
    if type(args.capacity) is not int or not 1 <= args.capacity <= 64:
        raise ValueError('capacity must be an integer in 1..64')
    for value in (args.capture_timeout, args.writer_timeout, args.display_timeout):
        if type(value) not in (int, float) or not np.isfinite(value) or not 0 < value <= 60:
            raise ValueError('timeouts must be finite in (0,60] seconds')
    concurrent = getattr(args, 'concurrent', False)
    processing_capacity = getattr(args, 'processing_capacity', 3)
    processing_timeout = getattr(args, 'processing_timeout', args.writer_timeout)
    if type(processing_capacity) is not int or not 1 <= processing_capacity <= 64:
        raise ValueError('processing-capacity must be in 1..64')
    if type(processing_timeout) not in (int,float) or not np.isfinite(processing_timeout) or not 0 < processing_timeout <= 60:
        raise ValueError('processing-timeout must be finite in (0,60] seconds')
    deps = deps or dependencies(args.source); base = deps.base
    roi = base._quality_roi(tuple(args.roi))
    operator_gate = base._validate_operator_mode(load=args.load, power_off_proof=args.power_off_proof, receipt=args.receipt)
    operator = None
    if args.operator_state is not None:
        from camera_target_live_r12 import validate_operator
        operator = json.loads(args.operator_state.read_text(encoding='utf-8')); validate_operator(operator)
    output = Path(args.output); output.mkdir(parents=True, exist_ok=False)
    from structured_light.stripe_v1.types import StripeConfig
    from structured_light.stripe_v1.selection_v2 import SelectionConfigV2
    from structured_light.stripe_v1.selection_v3 import SelectionConfigV3
    from structured_light.stripe_v1.selection_v4 import SelectionConfigV4
    from structured_light.scan3d.stream_ridge import RidgePacket
    config = StripeConfig(fixed_threshold=60, roi=roi)
    selection = SelectionConfigV4(legacy=SelectionConfigV3(selection=SelectionConfigV2(
        association_max_distance_px=3, association_max_row_gap=1, minimum_segment_rows=3),
        ridge_asym_max_dn=10, max_segment_width_span_px=12))
    state = dict(schema='r14.static_ridge_bench.v1', passed=False, failure=None,
        source='fresh_static_camera_Raw_SLC2', requested_frames=args.frames, frames=[], qa=[],
        capture_count=0, transition_frames=3, eligible_statistics_frames=0,
        display_requested=args.display, operator_gate=operator_gate, operator_state=operator,
        physical_laser_state_verified=False, motion_authorized=False, motor_start_count=0, guard_arm_count=0,
        optical_validation_passed=False, passed_scope='data transport, pixel-policy QA, persistence and clean ownership only',
        overlay_download_count=0, xyz_computed=False, measurement_eligible=False, physical_accuracy_verified=False,
        exposure_lines=args.exposure_lines, gain_code=16, actual_effect_known=False,
        stripe_config_fingerprint=config.fingerprint, selection_fingerprint=selection.fingerprint,
        roi=roi, center_policy='original_q12', captured_frames_are_pixel_observations=True,
        row_feature_dma=dict(enabled=False, format='SLC2', policy='RPG1', admission_attempted=False),
        restore=dict(attempted=False, verified=False, status='not_modified'), cleanup=None)
    state.update(concurrent_processing=concurrent, measurement_queue_policy='bounded FIFO; no captured measurement drops',
        display_queue_policy='latest frame; intermediate display submissions may drop')
    state['xyz_computed']=getattr(deps,'geometry_enabled',False) is True
    state['geometry_declaration']=getattr(deps,'geometry_declaration',None)
    runtime = scope = access = writer = display = remote = pipeline = None
    processing_joined = True
    submitted_all = set()
    generation = None; trace = []; snapshot = {}; backup = {}; pending = {}; submitted = set()
    modified = bus_fault = display_attempted = False
    writer_joined = display_joined = True; worker_unconfirmed = False
    previous = last_id = epoch = None
    current_capture = None; current_record = None
    started = time.perf_counter(); cpu_started = None; loop_started = None
    qa_indices = {2, args.frames//2, args.frames-1}

    def trace_save():
        path=output/'SCCB_TRACE.json.tmp'
        with path.open('w',encoding='utf-8') as stream:
            json.dump(base._json_safe(trace),stream,allow_nan=False)
            stream.flush();os.fsync(stream.fileno())
        path.replace(output/'SCCB_TRACE.json')

    def check_motor():
        base._camera_idle(runtime.controller)
        guard, motor = base._safe_board_snapshot(runtime)
        if not base._same_motor_identity(state['motor_before'], motor):
            raise RuntimeError('motor command identity changed during stationary pixel capture')
        return guard, motor
    def read_sensor():
        nonlocal bus_fault
        try: return {reg:int(access.read(reg)) for reg in REGISTERS}
        except BaseException: bus_fault=True; raise
    def release_committed():
        if writer is not None:
            waiting = set(writer.pending_request_ids)
            for identity in list(submitted-waiting): pending.pop(identity, None); submitted.discard(identity)
    def enter_scope():
        nonlocal scope, generation
        candidate = deps.scope(runtime); candidate.__enter__(); scope=candidate
        generation=runtime.session._gate.generation

    def process_frame(work):
        # This callback owns pending/submitted and CPU-only mutable processors.
        frame=work.frame; index=work.index; captured=work.captured_at
        frame_started=work.capture_started; capture_timing=work.capture_timing
        release_committed()
        packet=frame.compact_packet
        selection_started=time.perf_counter()
        stripe,primary=deps.select(packet,np.frombuffer(frame.raw,np.uint8).reshape(480,752),config,selection)
        selected=time.perf_counter(); transition=index<3
        process_selected=getattr(deps,'process_selected',None)
        extra=process_selected(frame,stripe,config,selection,transition) if process_selected is not None else None
        geometry_finished=time.perf_counter()
        reasons=dict(Counter(reason for reason in stripe.reasons[roi[1]:roi[3]] if reason is not None))
        summary=dict(request_id=frame.request_id,capture_epoch=frame.epoch,index=index,
            transition=transition,statistics_eligible=not transition,measurement_eligible=False,xyz_computed=False,
            center_policy='original_q12',selection_fingerprint=selection.fingerprint,
            stripe_config_fingerprint=config.fingerprint,roi=roi,profile_format='SLC2',
            pl_primary_rows=int(primary.sum()),raw_fallback_rows=int((~primary[roi[1]:roi[3]]).sum()),
            accepted_rows=int(stripe.accepted.sum()),retained_rejected_rows=int((stripe.retained & ~stripe.accepted).sum()),
            rejection_counts=reasons,center_q12=stripe.center_q12.tolist(),accepted=stripe.accepted.tolist(),
            retained=stripe.retained.tolist(),start=stripe.start.tolist(),end=stripe.end.tolist(),
            reasons=stripe.reasons,primary_rows=primary.tolist())
        current_record=dict(request_id=frame.request_id,capture_epoch=frame.epoch,raw=frame.raw,
            raw_sha256=frame.raw_sha256,profile_payload=frame.profile_payload,
            points_camera_mm=None,points_axis_mm=None,summary=summary,motion=frame.motion_metadata,
            exposure=dict(exposure_lines=args.exposure_lines,gain_code=16,AF=0,transition=transition,
                effect_known=False,measurement_eligible=False,observation_only=True))
        if extra is not None:
            if not isinstance(extra,dict): raise TypeError('process_selected must return a record update dict or None')
            if set(extra)-{'points_camera_mm','points_axis_mm','summary','display_result'}:
                raise ValueError('unsupported process_selected record update')
            for key in ('points_camera_mm','points_axis_mm'):
                if key in extra: current_record[key]=extra[key]
            current_record['summary'].update(extra.get('summary',{}))
        pack_summary=getattr(deps,'pack_summary',None)
        if pack_summary is not None:
            current_record['summary']=pack_summary(current_record['summary'])
        pending[frame.request_id]=current_record
        qa_started=time.perf_counter()
        if index in qa_indices:
            qa=deps.qa(frame,stripe,config,selection)
            if qa.get('passed') is not True:raise RuntimeError('same-frame sampled V4 QA failed')
            state['qa'].append(dict(qa,index=index,transition=transition))
        qa_finished=time.perf_counter()
        try:
            writer.submit(current_record)
        except BufferError:
            if not concurrent: raise
            # Drain the accepted prefix before retrying the unaccepted record.
            receipt=writer.flush(timeout_s=args.writer_timeout)
            if not receipt.durable: raise TimeoutError('writer backpressure did not durably drain')
            writer.submit(current_record)
        submitted.add(frame.request_id); submitted_all.add(frame.request_id)
        written=time.perf_counter()
        display_receipt=None
        if display is not None:
            display_result=(extra.get('display_result') if extra is not None else None)
            if display_result is None:
                display_result=PixelResult(frame.request_id,frame.epoch,stripe,roi,transition)
            display_receipt=display.consume(frame,display_result)
        finished=time.perf_counter()
        state['frames'].append(dict(index=index,request_id=frame.request_id,capture_epoch=frame.epoch,
            raw_sha256=frame.raw_sha256,transition=transition,accepted_rows=summary['accepted_rows'],
            pl_primary_rows=summary['pl_primary_rows'],raw_fallback_rows=summary['raw_fallback_rows'],
            rejection_counts=reasons,capture_s=captured-frame_started,selection_s=selected-selection_started,
            capture_to_processing_wait_s=selection_started-captured,
            geometry_s=geometry_finished-selected,qa_s=qa_finished-qa_started,writer_submit_s=written-qa_finished,
            display_submit_s=finished-written,frame_total_s=finished-frame_started,
            capture_timing=capture_timing,display_submission=display_receipt))

    try:
        record, artifacts = base._validate_policy_manifest(args.manifest)
        state['artifact_gate'] = dict(passed=True, artifacts=base._json_safe(artifacts))
        runtime = deps.runtime_factory(args.manifest, load=args.load, power_off_proof=args.power_off_proof,
            receipt=args.receipt, output=output/'runtime', quality_roi=roi)
        state['overlay_download_count'] = int(args.load)
        # Motor status is generation gated. Acquire the existing exclusive
        # scope before any board snapshot, and retain it through idle mode
        # admission, capture and restoration. _handoff_state still enforces
        # no in-flight buffers/recovery; no capture/display/writer exists yet.
        enter_scope()
        state['guard_before'], state['motor_before'] = base._safe_board_snapshot(runtime)
        state['row_feature_dma']['admission_attempted'] = True
        if runtime.transport.admit_row_feature_dma(args.manifest, workers_joined=True, enable=False) is not True:
            raise RuntimeError('row-feature prerequisite admission missing')
        if (runtime.transport.admit_ridge_dma(args.manifest, workers_joined=True) is not True
                or runtime.transport.ridge_policy_dma_enabled is not True):
            raise RuntimeError('actual SLC2 mode3 admission was not confirmed')
        state['row_feature_dma']['enabled'] = True
        last_id = int(runtime.transport.mmio.request_id); epoch = int(runtime.transport.mmio.epoch)
        # A fresh load exposes reset ID0 before START and also accepts ID0
        # first. Completed attach carries last_accepted_id and must accept its
        # uint32 successor. Use the production allocator for both contracts.
        next_id, epoch, recovery_token = runtime.transport._expected_start_identity(last_id, epoch)
        if recovery_token is not None:
            raise RuntimeError('stationary capture requires a clean load or completed attach, not recovery')
        state['initial_capture_identity'] = dict(request_id=last_id, epoch=epoch,
            expected_first_request_id=next_id, expected_first_epoch=epoch)
        access = deps.access(deps.gated_mmio(runtime.transport._diag_mmio, runtime.session._gate), trace, trace_save)
        snapshot = read_sensor()
        state['initial_registers'] = {hex(reg):value for reg,value in snapshot.items()}
        if snapshot[0] != 0x1324 or (snapshot[4], snapshot[3]) != (752, 480):
            raise RuntimeError('native sensor identity/752x480 window mismatch')
        backup = {reg:snapshot[reg] for reg in base.RESTORE_ORDER}
        plan = deps.manual_plan(snapshot, coarse_rows=args.exposure_lines, gain_code=16)
        save_new(output/'SENSOR_BACKUP.json', dict(schema='r13.static_ridge_sensor_backup.v1',
            initial_registers=state['initial_registers'], restore_registers={hex(k):v for k,v in backup.items()},
            plan=list(plan), manifest=str(args.manifest), receipt=str(args.receipt),
            bit_sha256=artifacts['files']['bit']['sha256'], measurement_eligible=False))
        try:
            for reg,value in plan: check_motor(); modified=True; access.write_verified(reg,value)
            actual = {reg:int(access.read(reg)) for reg in base.RESTORE_ORDER}
            if actual != {0x35:16, 0x0B:args.exposure_lines, 0xAF:0}:
                raise RuntimeError('manual exposure/gain/AF readback mismatch')
            state['manual_readback'] = {hex(k):v for k,v in actual.items()}
        except BaseException: bus_fault=True; raise
        writer = deps.writer_factory(output/'frames', capacity=args.capacity, group_size=4, max_delay_s=.05)
        if args.display:
            display_attempted=True
            options={'shared_canvas':True} if getattr(args,'shared_canvas',False) else {}
            remote=deps.display_factory(runtime,args.manifest,output,**options); remote.configure()
            display=getattr(deps,'observer_factory',PixelObserver)(remote,roi)
        loop_started=time.perf_counter(); cpu_started=time.process_time()
        if concurrent:
            from structured_light.scan3d.stream_concurrent import BoundedFrameProcessor
            pipeline=BoundedFrameProcessor(process_frame,capacity=processing_capacity)
        for index in range(args.frames):
            if pipeline is not None: pipeline.wait_for_capacity(processing_timeout)
            check_motor(); capture_timing={}
            frame_started=time.perf_counter()
            frame=deps.capture(runtime.controller,timeout_s=args.capture_timeout,
                require_motion_metadata=True,timing_s=capture_timing)
            current_capture=frame; state['capture_count'] += 1
            if (type(frame.request_id) is not int or type(frame.epoch) is not int
                    or frame.request_id != next_id or frame.epoch != epoch):
                raise RuntimeError('capture request ID/epoch did not advance exactly')
            previous=base._check_motion_metadata(frame.motion_metadata,frame.request_id,frame.epoch,state['motor_before'],previous)
            check_motor()
            if not isinstance(frame.raw,bytes) or len(frame.raw)!=752*480 or not isinstance(frame.profile_payload,bytes):
                raise RuntimeError('complete immutable native Raw/SLC2 payload required')
            packet=frame.compact_packet
            if (not isinstance(packet,RidgePacket) or packet.payload != frame.profile_payload
                    or packet.request_id!=frame.request_id or packet.epoch!=frame.epoch):
                raise RuntimeError('capture did not deliver the same-frame admitted SLC2 packet')
            captured=time.perf_counter()
            if pipeline is not None:
                pipeline.submit(index,frame,capture_started=frame_started,captured_at=captured,
                    capture_timing=capture_timing)
            else:
                process_frame(SimpleNamespace(frame=frame,index=index,captured_at=captured,
                    capture_started=frame_started,capture_timing=capture_timing))
            last_id=frame.request_id; next_id=runtime.transport._next_request_id(last_id)
            current_capture=current_record=None
        state['capture_complete']=True
        state['capture_producer_elapsed_s']=time.perf_counter()-loop_started
        if pipeline is not None: pipeline.drain(processing_timeout)
        state['processing_complete']=True
        state['processing_elapsed_s']=time.perf_counter()-loop_started
    except BaseException as exc:
        base._record_error(state,'run',exc)
    finally:
        if pipeline is not None:
            processing_close_started=time.perf_counter()
            state['processing_pipeline']=pipeline.close(processing_timeout)
            state['processing_drain_s']=time.perf_counter()-processing_close_started
            processing_joined=state['processing_pipeline']['worker_joined']
            if state['processing_pipeline']['error'] is not None:
                base._record_error(state,'processing_close',RuntimeError(state['processing_pipeline']['error']))
        if not processing_joined:
            writer_joined=display_joined=False
            state['writer']=dict(durable=False,worker_joined=False,error='processing remains active; downstream close deferred')
            state['display_close_deferred']='processing worker remains active'
        if writer is not None and processing_joined:
            drain_started=time.perf_counter()
            try: receipt=writer.close(timeout_s=args.writer_timeout)
            except BaseException as exc:
                base._record_error(state,'writer_close',exc); receipt=getattr(exc,'receipt',None)
            state['writer_drain_s']=time.perf_counter()-drain_started
            if receipt is None:
                writer_joined=False; state['writer']=dict(durable=False,worker_joined=False,error='close unconfirmed')
            else:
                state['writer']=base._json_safe(asdict(receipt))
                writer_joined=receipt.worker_joined is True and receipt.worker_unconfirmed is False
                if not receipt.durable: state['passed']=False
            if writer_joined: release_committed()
        if display is not None and processing_joined:
            flush_started=time.perf_counter()
            try: display.flush(timeout_s=args.display_timeout)
            except BaseException as exc: base._record_error(state,'display_flush',exc)
            state['display_flush_s']=time.perf_counter()-flush_started
            try: state['display']=display.close(timeout_s=args.display_timeout)
            except BaseException as exc:
                base._record_error(state,'display_close',exc); state['display']=display.summary()
            display_joined=state['display']['closed'] and not state['display']['worker_unconfirmed']
            if not state['display']['passed']: state['passed']=False
            if display_joined and state['display']['passed']:
                snapshot_canvas=display.last_rendered_canvas()
                if snapshot_canvas is not None:
                    try:
                        path=output/'LAST_PIXEL_CANVAS.png'
                        if not cv2.imwrite(str(path),snapshot_canvas.pop('canvas')):raise OSError('PNG export failed')
                        snapshot_canvas['png_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
                        save_new(output/'LAST_PIXEL_CANVAS.json',snapshot_canvas)
                    except BaseException as exc:base._record_error(state,'pixel_export',exc)
        elif display_attempted and processing_joined:
            display_joined=False
            if remote is not None:
                try:
                    state['display_close']=ConfirmedSink(remote).close(); display_joined=True
                except BaseException as exc:base._record_error(state,'display_startup_close',exc)
        worker_unconfirmed=not processing_joined or not writer_joined or not display_joined
        if writer_joined and processing_joined:
            if pipeline is not None:
                for work in pipeline.unprocessed():
                    frame=work.frame
                    if frame.request_id not in submitted_all and frame.request_id not in pending:
                        pending[frame.request_id]=dict(request_id=frame.request_id,capture_epoch=frame.epoch,
                            raw=frame.raw,profile_payload=frame.profile_payload,
                            summary=dict(reason='processing failed before writer submission',measurement_eligible=False))
            if (current_capture is not None and current_capture.request_id not in pending
                    and current_capture.request_id not in submitted_all):
                frame=current_capture
                pending[('capture',state['capture_count'])]=dict(request_id=frame.request_id,capture_epoch=frame.epoch,
                    raw=frame.raw,profile_payload=frame.profile_payload,summary=dict(reason='capture/selection rejected',measurement_eligible=False))
            for number,item in enumerate(pending.values()):
                try:
                    folder=output/'uncommitted'/f'capture_{number:04d}';folder.mkdir(parents=True,exist_ok=False)
                    for name,key in (('raw.gray8.bin','raw'),('profile.slc2.bin','profile_payload')):
                        with (folder/name).open('xb') as stream:stream.write(item[key]);stream.flush();os.fsync(stream.fileno())
                    save_new(folder/'record.json',base._json_safe({k:v for k,v in item.items() if k not in ('raw','profile_payload')}))
                    state.setdefault('uncommitted_captures',[]).append(dict(request_id=item['request_id'],path=str(folder),preserved=True))
                except BaseException as exc:base._record_error(state,'preserve_uncommitted',exc)
        if runtime is not None and not worker_unconfirmed:
            try:
                if scope is None and getattr(runtime.transport,'feature_mode_restore_required',False):enter_scope()
                state['guard_after'],state['motor_after']=check_motor()
                state['motor_identity_unchanged']=True
                if modified and access is not None:
                    if not bus_fault:
                        try:
                            final=read_sensor();state['final_registers_before_restore']={hex(k):v for k,v in final.items()}
                            if (any(final[reg]!=snapshot[reg] for reg in FIXED_REGISTERS)
                                    or (final[0x35],final[0x0B],final[0xAF])!=(16,args.exposure_lines,0)):
                                raise RuntimeError('fixed sensor geometry/mode/manual settings changed')
                        except BaseException as exc:bus_fault=True;base._record_error(state,'final_sensor_readback',exc)
                    if bus_fault:
                        state['restore']=dict(verified=False,status='sccb_fault_no_blind_rollback',external_recovery_required=True)
                        state['external_recovery_required']=True
                    else:
                        state['restore']=base._restore_sensor(access,backup,backup_complete=True)
                        if not state['restore']['verified']:
                            state['external_recovery_required']=True
                            if state['restore'].get('status')=='restore_failed_sccb_fault':
                                bus_fault=True
                            base._record_error(state,'sensor_restore',RuntimeError(
                                '; '.join(state['restore'].get('errors',[]))
                                or state['restore'].get('status','unverified sensor restore')))
            except BaseException as exc:
                base._record_error(state,'final_identity',exc)
            deps.restore_mode(runtime,state,generation=generation,workers_joined=True,writer_joined=writer_joined)
            if (getattr(runtime.transport,'feature_mode_restore_required',False)
                    or state['row_feature_dma'].get('restore_status') in ('unconfirmed','skipped_invalid_generation')):
                worker_unconfirmed=True
        # A revoke failure must not fall through to runtime.close/CMA release.
        # Use the existing scope once, then pass no scope to the shared helper.
        if scope is not None:
            try:scope.__exit__(None,None,None)
            except BaseException as exc:
                worker_unconfirmed=True;base._record_error(state,'generation_close',exc)
                state['generation_close_error']=type(exc).__name__+': '+str(exc)
        deps.close(state,scope=None,display=None,runtime=runtime,profiler=None,output=output,
            worker_unconfirmed=worker_unconfirmed)
        pool=getattr(runtime,'r14_dma_pool',None) if runtime is not None else None
        if pool is not None:
            state['dma_pool']=pool.summary()
            if not state['dma_pool']['closed']:
                worker_unconfirmed=True
                state['external_recovery_required']=True
                base._record_error(state,'dma_pool_close',RuntimeError(
                    state['dma_pool']['error'] or 'physical DMA pool release unconfirmed'))
        if worker_unconfirmed and runtime is not None:
            _QUARANTINED.append(dict(runtime=runtime,scope=scope,writer=writer,display=display,remote=remote,
                pending=pending,current_capture=current_capture,current_record=current_record,pipeline=pipeline))
            lock=getattr(runtime,'hardware_lock',None)
            # Closing this duplicate preserves an HDMI child's inherited lock;
            # never issue LOCK_UN or release allocations while its close is unknown.
            if not display_joined and lock is not None and getattr(lock,'_handle',None) is not None:
                lock._handle.close();lock._handle=None
            state['cleanup']=dict(closed=False,capture_handoff=None,worker_unconfirmed=True)
            state.update(quarantined=True,external_recovery_required=True,
                quarantine_policy='RESULT/DONE published; foreground process and strong references retained; no automatic retry/termination')
        state['sccb_fault']=bus_fault; state['sccb_trace']=trace
        elapsed=time.perf_counter()-loop_started if loop_started is not None else None
        state['full_chain_elapsed_s']=elapsed
        state['captured_full_chain_fps']=state['capture_count']/elapsed if elapsed else None
        durable_count=state.get('writer',{}).get('written')
        state['durable_full_chain_fps']=durable_count/elapsed if elapsed and durable_count is not None else None
        state['full_chain_fps']=state['durable_full_chain_fps']
        state['full_chain_fps_numerator']='writer durable written frame count'
        state['full_chain_fps_denominator']='loop start through processing/writer/display drain, owner close, sensor restore and runtime close'
        state['processed_frame_count']=len(state['frames'])
        if writer_joined and writer is not None:
            state['writer_batches']=base._json_safe(list(getattr(writer,'batch_timings',())))
        state['full_chain_includes_QA_writer_drain_display_flush_owner_close_sensor_restore_runtime_close']=True
        state['elapsed_including_initialization_s']=time.perf_counter()-started
        eligible=[row for row in state['frames'] if not row['transition']]
        state['eligible_statistics_frames']=len(eligible)
        state['eligible_mean_accepted_rows']=float(np.mean([r['accepted_rows'] for r in eligible])) if eligible else None
        if cpu_started is not None and elapsed:
            cpu=time.process_time()-cpu_started
            state.update(cpu_foreground_s=cpu,cpu_foreground_one_core_pct=100*cpu/elapsed,
                cpu_foreground_total_capacity_pct=100*cpu/elapsed/(os.cpu_count() or 1),
                cpu_accounting='foreground all threads includes render/writer; separate HDMI owner excluded')
        state['passed']=bool(state['failure'] is None and state.get('capture_complete') and state.get('processing_complete')
            and state.get('writer',{}).get('durable') and state['writer'].get('written')==args.frames
            and (not args.display or state.get('display',{}).get('passed') is True)
            and state['restore'].get('verified') and not worker_unconfirmed
            and state.get('cleanup',{}).get('closed') and state['cleanup'].get('capture_handoff')
            and not state.get('generation_close_error') and not state.get('cleanup_error')
            and not state.get('external_recovery_required'))
        if state.get('display') and elapsed:
            state['actual_rendered_fps']=state['display']['rendered']/elapsed
            state['actual_hdmi_acknowledged_fps']=state['display']['hdmi_acknowledged']/elapsed
        save_new(output/'RESULT.json',base._json_safe(state))
        save_new(output/'DONE.json',dict(schema='r14.static_ridge_bench.done.v1',passed=state['passed'],
            quarantined=state.get('quarantined',False),external_recovery_required=state.get('external_recovery_required',False),
            process_will_exit=not state.get('quarantined',False),exit_code=0 if state['passed'] else 1))
    return state


def wait_quarantined():
    while True:
        try:time.sleep(1.)
        except KeyboardInterrupt:continue


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','manifest','output'):parser.add_argument('--'+name,type=Path,required=True)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--receipt',type=Path);mode.add_argument('--load',action='store_true')
    parser.add_argument('--power-off-proof',type=Path)
    parser.add_argument('--operator-state',type=Path)
    parser.add_argument('--frames',type=int,default=120)
    parser.add_argument('--exposure-lines',type=int,default=80)
    parser.add_argument('--roi',type=int,nargs=4,default=(220,225,420,450))
    parser.add_argument('--display',action='store_true')
    parser.add_argument('--shared-canvas',action='store_true',help='explicit shared-memory HDMI canvas transport')
    parser.add_argument('--capacity',type=int,default=16)
    parser.add_argument('--concurrent',action='store_true',help='explicit bounded capture/CPU processing overlap')
    parser.add_argument('--processing-capacity',type=int,default=3)
    parser.add_argument('--processing-timeout',type=float,default=10.)
    parser.add_argument('--capture-timeout',type=float,default=2.)
    parser.add_argument('--writer-timeout',type=float,default=10.)
    parser.add_argument('--display-timeout',type=float,default=10.)
    args=parser.parse_args(argv)
    try:result=run_bench(args)
    except Exception as exc:
        print(json.dumps(dict(passed=False,entry_error=type(exc).__name__+': '+str(exc),
            quarantined=bool(_QUARANTINED))),flush=True)
        if _QUARANTINED:wait_quarantined()
        return 2
    print(json.dumps({key:result.get(key) for key in ('schema','passed','capture_count','eligible_statistics_frames',
        'full_chain_fps','actual_hdmi_acknowledged_fps','failure','cleanup','quarantined')}),flush=True)
    if result.get('quarantined'):
        # The host reads DONE rather than waiting for exit. Leaving Python here
        # could destruct unreleased CMA buffers or drop the exclusive lock.
        wait_quarantined()
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
