"""Display already computed profiles without repeating raw stripe extraction.

Formal fusion belongs exclusively to ContinuousProcessor. Replay accumulation
is separate, explicitly assumed and never changes ProcessedProfile validity.
Live static observation displays only the current camera section.
"""
from __future__ import annotations

from collections import deque
import math
import threading
import time

import cv2
import numpy as np

from sl_geometry.geometry import unrotate_points
from .dashboard import DashboardRenderer, DashboardStatus
from .turntable import TurntableModel


REPLAY_BANNER = 'REPLAY / AXIS PENDING / NO METROLOGY'
LIVE_STATIC_BANNER = 'STATIC XYZ / AXIS PENDING / NO TURN-TABLE FUSION'


class ProcessedObserver:
    def __init__(self, *, processor=None, source, sink=None, preview_axis=None,
                 renderer=None, asynchronous=None, capacity=1, max_cloud_points=250_000,
                 performance_trace=None, trace_hdmi_hardware=False):
        if source not in {'replay', 'real_continuous_scan', 'live_static'}:
            raise ValueError('explicit replay, real_continuous_scan or live_static source required')
        if type(capacity) is not int or capacity < 1:
            raise ValueError('positive display queue capacity required')
        if type(max_cloud_points) is not int or not 1 <= max_cloud_points <= 250_000:
            raise ValueError('preview capacity must be 1..250000')
        if source == 'real_continuous_scan' and preview_axis is not None:
            raise ValueError('formal display cannot use an assumed preview axis')
        if source == 'live_static' and preview_axis is not None:
            raise ValueError('live static display cannot use an assumed preview axis')
        if preview_axis is not None:
            if not isinstance(preview_axis, TurntableModel) or preview_axis.calibrated:
                raise ValueError('replay preview requires an explicit uncalibrated TurntableModel')
            preview_axis.__post_init__()
        if asynchronous is None:
            asynchronous = source != 'replay'
        if type(asynchronous) is not bool:
            raise ValueError('asynchronous must be bool')
        self.processor, self.source, self.sink = processor, source, sink
        self.performance_trace=performance_trace
        self.trace_hdmi_hardware=trace_hdmi_hardware is True
        self._title = (REPLAY_BANNER if source == 'replay' else LIVE_STATIC_BANNER
                       if source == 'live_static' else 'CONTINUOUS SCAN')
        self.preview_axis = preview_axis
        self.renderer = renderer if renderer is not None else DashboardRenderer()
        self.asynchronous, self.capacity = asynchronous, capacity
        self.max_cloud_points = max_cloud_points
        self._condition = threading.Condition()
        self._queue = deque()
        self._active = False
        self._closing = self._closed = False
        self._error = None
        self._close_error = None
        self._sink_close_receipt = None
        self._worker_unconfirmed = False
        self._started = time.perf_counter()
        self._finished = None
        self._counts = dict(submitted=0, rendered=0, acknowledged=0, hdmi_acknowledged=0, dropped=0, failed=0)
        self._records = deque(maxlen=4096)
        self._failures = deque(maxlen=4096)
        self._dropped_ids = deque(maxlen=4096)
        self._preview_chunks = []
        self._preview_buffer = None
        self._preview_count = self._preview_revision = self._preview_generation = 0
        self._snapshot = None
        self._last_rendered = None
        self._thread = None
        if asynchronous:
            self._thread = threading.Thread(target=self._run, name='processed_hdmi', daemon=True)
            self._thread.start()

    def _check(self):
        if self._closing or self._closed:
            raise RuntimeError('processed display is closing or closed')
        if self._error is not None:
            raise RuntimeError('processed display failed: ' + self._error)

    def consume(self, capture, result, *, preview_angle_rad=None, preview_angle_source=None):
        if (capture.request_id != result.request_id or capture.epoch != result.capture_epoch):
            raise ValueError('display capture/profile identity mismatch')
        if len(capture.raw) != 752*480:
            raise ValueError('display requires complete native 752x480 raw')
        if preview_angle_rad is not None:
            if (self.source != 'replay' or self.preview_axis is None or isinstance(preview_angle_rad, bool)
                    or not isinstance(preview_angle_rad, (int, float)) or not math.isfinite(preview_angle_rad)):
                raise ValueError('finite replay angle and explicit assumed axis required')
            if preview_angle_source is None:
                preview_angle_source = 'indeterminate_preview_angle'
            if not isinstance(preview_angle_source, str) or not preview_angle_source.strip():
                raise ValueError('preview angle source must be explicit nonempty text')
        elif preview_angle_source is not None:
            raise ValueError('preview angle source requires a preview angle')
        item = (capture, result, preview_angle_rad, time.perf_counter(), preview_angle_source)
        with self._condition:
            self._check()
            if not self.asynchronous and self._active:
                raise RuntimeError('synchronous display already active')
            self._counts['submitted'] += 1
            # Freeze the processed section and its revision at submission.
            # Display eviction must not discard assumed-preview sections, and
            # a later processor snapshot must not be painted beside older raw.
            try:
                points_info = self._points(result, preview_angle_rad)
            except BaseException as exc:
                self._record_error(exc, item)
                raise
            item += (points_info, self._preview_revision, self._preview_generation,
                     self._counts['submitted'])
            if self.asynchronous:
                if len(self._queue) >= self.capacity:
                    old = self._queue.popleft()
                    self._dropped_ids.append(old[0].request_id)
                    self._counts['dropped'] += 1
                self._queue.append(item)
                self._condition.notify_all()
                return dict(queued=True, acknowledged=False, request_id=capture.request_id,
                            capture_epoch=capture.epoch, submission_sequence=item[8],
                            points_s=points_info[4], stitch_s=points_info[4] if self.source == 'replay' else 0.,
                            display_dropped=self._counts['dropped'], source=self.source,
                            title=self._title,
                            hdmi_acknowledged=False, physical_accuracy_verified=False)
            self._active = True
        try:
            return self._consume(item)
        except BaseException as exc:
            self._record_error(exc, item)
            raise
        finally:
            with self._condition:
                self._active = False
                self._condition.notify_all()

    def _points(self, result, preview_angle):
        started = time.perf_counter()
        snapshot_revision = None
        kind = 'camera_section'
        points = np.asarray(result.points_camera_mm)[result.valid_points]
        if self.source == 'live_static':
            # Observational XYZ is neither an accumulated model nor evidence of
            # valid metrology. Never request a fused snapshot for this source.
            kind = 'live_static_camera_section'
            revision = ('live_static', result.capture_epoch, result.request_id, self._counts['submitted'])
        elif self.source == 'real_continuous_scan':
            if result.fused:
                if self.processor is None:
                    raise RuntimeError('formal fused display requires the owning processor snapshot')
                if self._snapshot is None or self._snapshot.revision < result.cloud_revision:
                    # R22 may expose a bounded display-only snapshot. Full
                    # measurement data remains in its independently saved spool.
                    snapshot=getattr(self.processor,'display_snapshot',self.processor.snapshot)
                    self._snapshot = snapshot()
                if self._snapshot.revision < result.cloud_revision:
                    raise RuntimeError('processor snapshot precedes the processed profile')
            if self._snapshot is not None:
                points = self._snapshot.points_axis_mm
                snapshot_revision = self._snapshot.revision
                kind = 'processor_fused_cloud'
            elif not result.fused:
                # A camera section is not a registered multi-frame model.
                points = np.empty((0, 3), float)
            revision = ('formal', snapshot_revision)
        elif preview_angle is not None:
            if self._preview_count + len(points) > self.max_cloud_points:
                raise BufferError('assumed replay preview capacity exceeded; reset between rounds')
            transformed = unrotate_points(points, self.preview_axis.axis_point_mm,
                                          self.preview_axis.axis_direction, preview_angle)
            if len(transformed):
                if self._preview_buffer is None:
                    self._preview_buffer = np.empty((self.max_cloud_points, 3), float)
                begin = self._preview_count
                self._preview_buffer[begin:begin+len(transformed)] = transformed
                chunk = self._preview_buffer[begin:begin+len(transformed)].view()
                chunk.setflags(write=False)
                self._preview_chunks.append(chunk)
                self._preview_count += len(transformed)
                self._preview_revision += 1
            # Appends write only after this prefix. The worker may safely read
            # the immutable prefix while processing accepts the next section.
            points = (self._preview_buffer[:self._preview_count].view()
                      if self._preview_buffer is not None else np.empty((0, 3), float))
            points.setflags(write=False)
            kind = 'assumed_replay_preview'
            revision = ('replay', self._preview_generation, self._preview_revision)
        else:
            revision = ('section', result.capture_epoch, result.request_id, self._counts['submitted'])
        return points, revision, snapshot_revision, kind, time.perf_counter()-started

    def _consume(self, item):
        capture, result, preview_angle, queued_at, preview_angle_source = item[:5]
        started = time.perf_counter()
        points, revision, snapshot_revision, kind, points_s = item[5]
        validity = getattr(self.processor, 'measurement_validity', None)
        replay = self.source == 'replay'
        live_static = self.source == 'live_static'
        formal = self.source == 'real_continuous_scan'
        elapsed = max(time.perf_counter()-self._started, 1e-9)
        # The persistent full-width banner carries metrology restrictions.
        # Keep this narrow mode cell clear of the frame ID and angle cells.
        status = DashboardStatus(mode='REPLAY' if replay else 'STATIC' if live_static else 'SCAN',
            source='replay' if replay else 'live', quality=result.quality.status,
            frame_number=capture.request_id,
            display_fps=self._counts['acknowledged']/elapsed,
            angle_deg=(math.degrees(preview_angle) if preview_angle is not None
                       else math.degrees(result.angle_rad) if not live_static and result.angle_rad is not None else None),
            axis_calibrated=bool(formal and validity is not None and validity.axis_valid),
            timing_calibrated=bool(formal and validity is not None and validity.timing_valid),
            volume_calibrated=bool(formal and validity is not None and validity.volume_valid))
        rendering = time.perf_counter()
        canvas = self.renderer.render(capture.raw, points, status=status, cloud_revision=revision)
        renderer_timings = dict(getattr(self.renderer, 'timings', {}))
        if replay or live_static:
            # Always visible, including debug=False and English-font fallback.
            cv2.rectangle(canvas, (0, 0), (1279, 25), (15, 20, 45), -1)
            cv2.putText(canvas, self._title, (20, 19), cv2.FONT_HERSHEY_SIMPLEX,
                        .62, (80, 210, 255), 1, cv2.LINE_AA)
        if live_static:
            # This strip is above the panels and never modifies native pixels.
            rejection_counts = dict(getattr(result, 'rejection_counts', {}))
            detail = (f'LIVE CAMERA / CURRENT FRAME / REQUEST {capture.request_id}'
                      f' / EPOCH {capture.epoch} / XYZ {len(points)}')
            if not len(points):
                detail += ' / WAITING FOR VALID XYZ'
            detail += f' / Q {status.quality.upper()[:12]}'
            if rejection_counts:
                reason = max(rejection_counts, key=rejection_counts.get)
                reason_text = str(reason).encode('ascii', errors='replace').decode('ascii')[:28]
                detail += f' / {reason_text}:{rejection_counts[reason]}'
            cv2.rectangle(canvas, (0, 70), (1279, 91), (15, 20, 45), -1)
            text_width = cv2.getTextSize(detail, cv2.FONT_HERSHEY_SIMPLEX, .47, 1)[0][0]
            cv2.putText(canvas, detail, (20, 86), cv2.FONT_HERSHEY_SIMPLEX,
                        .47*min(1., 1240/max(text_width, 1)), (80, 210, 255), 1, cv2.LINE_AA)
        rendered = time.perf_counter()
        with self._condition:
            self._counts['rendered'] += 1
            if live_static:
                # Keep one canvas reference, with no per-frame image copy or
                # encoding. Retrieval copies only after the worker is closed.
                self._last_rendered = dict(canvas=canvas, source=self.source, title=self._title,
                    request_id=capture.request_id, capture_epoch=capture.epoch,
                    quality=result.quality.status, rejection_counts=rejection_counts,
                    points=len(points), point_source=kind, rendered=True,
                    display_state='observational_xyz' if len(points) else 'waiting_for_valid_xyz',
                    original_valid=result.valid, original_fused=result.fused,
                    axis_calibrated=False, timing_calibrated=False, volume_calibrated=False,
                    acknowledged=False, hdmi_acknowledged=False, physical_accuracy_verified=False)
        sink_receipt = None
        if self.sink is not None:
            show = getattr(self.sink, 'show_canvas', None)
            sink_receipt = show(canvas) if show is not None else self.sink(canvas)
        finished = time.perf_counter()
        acknowledged = self.sink is not None
        hdmi_ack = bool(isinstance(sink_receipt, dict) and sink_receipt.get('hdmi_acknowledged') is True)
        receipt = dict(source=self.source, title=self._title,
            request_id=capture.request_id, capture_epoch=capture.epoch, rendered=True,
            submission_sequence=item[8], cloud_revision=revision,
            acknowledged=acknowledged, hdmi_acknowledged=hdmi_ack, queued=False,
            point_source=kind, points=len(points), snapshot_revision=snapshot_revision,
            preview_revision=item[6] if replay else None,
            preview_generation=item[7] if replay else None,
            angle_source=preview_angle_source if preview_angle is not None else result.angle_source,
            preview_axis_fingerprint=self.preview_axis.fingerprint if replay and self.preview_axis else None,
            axis_calibrated=status.axis_calibrated, physical_accuracy_verified=False,
            original_valid=result.valid, original_fused=result.fused,
            queue_wait_s=max(0., started-queued_at-points_s), points_s=points_s,
            snapshot_s=points_s if not replay else 0., stitch_s=points_s if replay else 0.,
            render_s=rendered-rendering, show_s=finished-rendered,
            renderer_timings=renderer_timings,
            total_s=finished-started+points_s, submit_to_ack_s=finished-queued_at,
            completed_monotonic_s=finished, sink_receipt=sink_receipt)
        if live_static:
            receipt.update(quality=result.quality.status,
                display_state='observational_xyz' if len(points) else 'waiting_for_valid_xyz',
                rejection_counts=rejection_counts, timing_calibrated=False, volume_calibrated=False)
        with self._condition:
            if live_static:
                self._last_rendered.update(acknowledged=acknowledged, hdmi_acknowledged=hdmi_ack)
            self._records.append(receipt)
            self._counts['acknowledged'] += int(acknowledged)
            self._counts['hdmi_acknowledged'] += int(hdmi_ack)
        if self.performance_trace is not None:
            self.performance_trace.displayed(receipt,hardware_confirmed=self.trace_hdmi_hardware)
        return receipt

    def _record_error(self, exc, item):
        with self._condition:
            self._error = type(exc).__name__ + ': ' + str(exc)
            self._counts['failed'] += 1
            self._failures.append(dict(request_id=item[0].request_id, capture_epoch=item[0].epoch,
                error=self._error, submit_to_failure_s=time.perf_counter()-item[3],
                source=self.source, title=self._title,
                acknowledged=False, hdmi_acknowledged=False))
            while self._queue:
                self._dropped_ids.append(self._queue.popleft()[0].request_id)
                self._counts['dropped'] += 1
            self._condition.notify_all()

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._closing or self._error is not None)
                if self._error is not None or (self._closing and not self._queue):
                    return
                item = self._queue.popleft()
                self._active = True
            try:
                self._consume(item)
            except BaseException as exc:
                self._record_error(exc, item)
                return
            finally:
                with self._condition:
                    self._active = False
                    self._condition.notify_all()

    def flush(self, timeout_s=10.):
        deadline = time.perf_counter()+timeout_s
        with self._condition:
            while self._queue or self._active:
                remaining = deadline-time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError('processed display did not drain')
                self._condition.wait(remaining)
            if self._error is not None:
                raise RuntimeError('processed display failed: '+self._error)
        return self.summary()

    def reset_preview(self):
        if self.source != 'replay':
            raise RuntimeError('only replay preview can be reset')
        with self._condition:
            self._check()
            if self._queue or self._active:
                raise RuntimeError('drain display before resetting replay preview')
            self._preview_chunks.clear()
            self._preview_count = self._preview_revision = 0
            self._preview_generation += 1

    def summary(self):
        with self._condition:
            elapsed = (self._finished if self._finished is not None else time.perf_counter())-self._started
            return dict(source=self.source, title=self._title,
                **self._counts, capacity=self.capacity, asynchronous=self.asynchronous,
                pending=len(self._queue), active=self._active, error=self._error,
                close_error=self._close_error, closed=self._closed,
                sink_close_receipt=self._sink_close_receipt,
                worker_unconfirmed=self._worker_unconfirmed,
                passed=self._error is None and self._close_error is None and not self._worker_unconfirmed,
                elapsed_s=elapsed, display_fps=self._counts['acknowledged']/elapsed if elapsed else 0.,
                hdmi_acknowledged_fps=self._counts['hdmi_acknowledged']/elapsed if elapsed else 0.,
                records=list(self._records), failures=list(self._failures),
                dropped_request_ids=list(self._dropped_ids),
                preview_points=self._preview_count, preview_generation=self._preview_generation,
                preview_axis=self.preview_axis.to_json() if self.preview_axis else None,
                axis_calibrated=False if self.source in {'replay', 'live_static'} else None,
                physical_accuracy_verified=False)

    def last_rendered_canvas(self):
        """Copy the exact last static BGR canvas after confirmed close.

        Returns None when no frame was rendered. The returned ``canvas`` is a
        separate, read-only uint8 array; the other fields identify that frame.
        No encoding or copying occurs on the per-frame rendering path.
        """
        with self._condition:
            if self.source != 'live_static':
                raise RuntimeError('last rendered canvas is only retained for live_static')
            if not self._closed or self._worker_unconfirmed or self._active:
                raise RuntimeError('close and join processed display before retrieving canvas')
            if self._last_rendered is None:
                return None
            snapshot = dict(self._last_rendered)
            snapshot['rejection_counts'] = dict(snapshot['rejection_counts'])
            snapshot['canvas'] = snapshot['canvas'].copy()
            snapshot['canvas'].setflags(write=False)
            return snapshot

    def close(self, timeout_s=10.):
        with self._condition:
            if self._closed:
                return self.summary()
            self._closing = True
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                self._worker_unconfirmed = True
                raise TimeoutError('display worker unconfirmed; retain sink allocations')
        else:
            with self._condition:
                if self._active:
                    self._worker_unconfirmed = True
                    raise RuntimeError('synchronous display active; retain sink allocations')
        self._worker_unconfirmed = False
        close = getattr(self.sink, 'close', None)
        if close is not None:
            try:
                self._sink_close_receipt = close()
            except BaseException as exc:
                self._close_error = type(exc).__name__+': '+str(exc)
                self._worker_unconfirmed = True
                self._closed = False
                # Keep the sink reference and refuse to certify cleanup while
                # its owner/DMA stop remains unconfirmed.
                return self.summary()
        self._closed = True
        self._finished = time.perf_counter()
        return self.summary()


__all__ = ['ProcessedObserver', 'REPLAY_BANNER', 'LIVE_STATIC_BANNER']
