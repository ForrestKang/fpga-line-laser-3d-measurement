"""Bounded latest-only display process, independent of lossless measurements.

Anonymous shared buffers support fork on Linux and spawn in offline tests.
Only the display child talks to the HDMI owner. An unconfirmed hardware close
quarantines that child and the parent must retain its hardware lock.
"""
from __future__ import annotations

import ctypes
import math
import multiprocessing as mp
from pathlib import Path
import time
import traceback

import numpy as np


class DisplayStartupError(RuntimeError):
    def __init__(self, message, display):
        super().__init__(message)
        self.display = display


def _real_owner(config):
    import os
    from structured_light.p2.hdmi_owner import RemoteHDMIOutput
    os.environ['SL_HDMI_PROCESSED_CANVAS'] = '1'
    return RemoteHDMIOutput(config['manifest'], owner_log=config['owner_log'],
                            timeout_s=5, startup_timeout_s=60, preview_only=True,
                            hardware_lock_fd=config['hardware_lock_fd'], shared_canvas=True)


def _renderer(config):
    return ReplayDashboardRenderer(point_raster_manifest=config.get('raster_manifest'))


class ReplayDashboardRenderer:
    """Cache static dashboard labels; redraw only raw/model and short ASCII facts.

    The production DashboardRenderer remains unchanged. Its measured-point
    projection/native raster and full native camera panel are reused exactly.
    Static cards explicitly remain unvalidated because this replay has no
    physical measurement or online feature-analysis result.
    """

    def __init__(self, *, point_raster_manifest=None):
        import cv2
        from structured_light.scan3d.dashboard import DashboardRenderer, DashboardStatus
        self.base_renderer = DashboardRenderer(point_raster_manifest=point_raster_manifest)
        self.template = self.base_renderer.render(np.zeros((480, 752), np.uint8),
                                                   np.empty((0, 3)),
                                                   status=DashboardStatus(mode='SIMULATION', source='replay'))
        # Reserve dynamic regions so no old frame/angle/point count persists.
        cv2.rectangle(self.template, (240, 24), (900, 82), (28, 17, 7), -1)
        cv2.rectangle(self.template, (400, 96), (1255, 120), (28, 17, 7), -1)
        self.full_cloud_points = 0
        self.timings = {}

    def render(self, gray, points, *, status, cloud_revision=None):
        import cv2
        from structured_light.scan3d.dashboard import CAMERA_IMAGE, MODEL_VIEW
        started = time.perf_counter()
        if gray.shape != (480, 752) or gray.dtype != np.uint8:
            raise ValueError('native gray8 frame required')
        canvas = np.empty_like(self.template)
        cx, cy, cw, ch = CAMERA_IMAGE
        mx, my, mw, mh = MODEL_VIEW
        canvas[:cy] = self.template[:cy]
        canvas[cy+ch:] = self.template[cy+ch:]
        canvas[cy:cy+ch, :cx] = self.template[cy:cy+ch, :cx]
        canvas[cy:cy+ch, cx+cw:mx] = self.template[cy:cy+ch, cx+cw:mx]
        canvas[cy:cy+ch, mx+mw:] = self.template[cy:cy+ch, mx+mw:]
        static_s = time.perf_counter()-started
        t = time.perf_counter()
        cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR, dst=canvas[cy:cy+ch, cx:cx+cw])
        camera_s = time.perf_counter()-t
        t = time.perf_counter()
        self.base_renderer._point_raster_receipt = {}
        model, hit = self.base_renderer._model_tile(points, None, cloud_revision, -32.)
        canvas[my:my+mh, mx:mx+mw] = model
        model_s = time.perf_counter()-t
        t = time.perf_counter()
        text = (f'FRAME {status.frame_number}   ANGLE {status.angle_deg:.2f} deg'
                f'   PROGRESS {status.progress_pct:.1f}%')
        cv2.putText(canvas, text, (250, 49), cv2.FONT_HERSHEY_SIMPLEX, .48, (243, 235, 226), 1, cv2.LINE_AA)
        cv2.putText(canvas, 'SIMULATION / AXIS & TIME & VOLUME: PHYSICALLY UNVALIDATED',
                    (250, 75), cv2.FONT_HERSHEY_SIMPLEX, .40, (96, 190, 255), 1, cv2.LINE_AA)
        cv2.putText(canvas, f'VISIBLE {len(points):,} / FULL OBSERVED {self.full_cloud_points:,}',
                    (810, 113), cv2.FONT_HERSHEY_SIMPLEX, .43, (170, 153, 132), 1, cv2.LINE_AA)
        self.timings = dict(renderer='r19_cached_static_dashboard', static_copy_ms=static_s*1000,
                            camera_copy_ms=camera_s*1000, model_ms=model_s*1000,
                            dynamic_ui_ms=(time.perf_counter()-t)*1000,
                            render_ms=(time.perf_counter()-started)*1000, model_cache_hit=hit,
                            **self.base_renderer._point_raster_receipt)
        return canvas


def _display_worker(shared, connection, config, owner_factory, renderer_factory):
    import cv2
    from structured_light.scan3d.dashboard import DashboardStatus
    cv2.setNumThreads(1)
    gray_buffer, cloud_buffer, state, condition = shared
    gray = np.frombuffer(gray_buffer, np.uint8).reshape(480, 752)
    cloud = np.frombuffer(cloud_buffer, np.float64).reshape(-1, 3)
    owner = None
    clean = False
    error = None
    last_canvas = None
    try:
        owner = owner_factory(config)
        owner.configure()
        renderer = renderer_factory(config)
        connection.send(dict(kind='ready', pid=mp.current_process().pid))
        while True:
            with condition:
                while state['pending'] == 0 and state['closing'] == 0:
                    condition.wait()
                if state['pending'] == 0:
                    break
                index, angle = int(state['frame_id']), float(state['angle'])
                count = int(state['points'])
                # Full-resolution raw is always retained. Only visualization
                # points are decimated; every XYZ remains in shared storage.
                stride = max(1, math.ceil(count / config['visible_points']))
                snapshot = cloud[:count:stride].copy()
                image = gray.copy()
                state['pending'] = 0
            started = time.perf_counter()
            if isinstance(renderer, ReplayDashboardRenderer):
                renderer.full_cloud_points = count
            status = DashboardStatus(mode='SIMULATION', source='replay',
                                     progress_pct=min(100., 100*(index+1)/config['frame_total']),
                                     angle_deg=float(np.degrees(angle)), quality='simulation',
                                     frame_number=index)
            canvas = renderer.render(image, snapshot, status=status,
                                     cloud_revision=(index, count))
            cv2.rectangle(canvas, (0, 0), (1279, 23), (0, 50, 80), -1)
            cv2.putText(canvas, 'SIMULATED INPUT / OBSERVED XYZ / NO PHYSICAL MEASUREMENT',
                        (12, 17), cv2.FONT_HERSHEY_SIMPLEX, .49, (255, 235, 160), 1, cv2.LINE_AA)
            render_s = time.perf_counter() - started
            started = time.perf_counter()
            receipt = owner.show_canvas(canvas)
            # show_canvas validates its shared-canvas ACK before returning.
            if not isinstance(receipt, dict):
                raise RuntimeError('HDMI owner did not return an ACK mapping')
            last_canvas = canvas
            connection.send(dict(kind='ack', frame_id=index, cloud_points=count,
                                 visible_points=len(snapshot), display_stride=stride,
                                 render_s=render_s, show_s=time.perf_counter()-started,
                                 renderer_timings=dict(getattr(renderer, 'timings', {})),
                                 receipt=receipt))
        if last_canvas is not None and config.get('last_canvas'):
            if not cv2.imwrite(config['last_canvas'], last_canvas):
                raise RuntimeError('failed to save last acknowledged HDMI canvas')
    except BaseException:
        error = traceback.format_exc()
        connection.send(dict(kind='error', error=error))
    finally:
        close_receipt = None
        close_started = time.perf_counter()
        if owner is not None:
            try:
                close_receipt = owner.close()
                clean = bool(close_receipt.get('closed_confirmed') and
                             close_receipt.get('owner_exited') and close_receipt.get('returncode') == 0)
            except BaseException:
                error = (error or '') + '\nOWNER CLOSE:\n' + traceback.format_exc()
        # A constructor failure might already have spawned an owner. Only an
        # explicit successful owner close establishes safe lock release.
        connection.send(dict(kind='closed', clean=clean, owner_close=close_receipt, error=error,
                             owner_close_s=time.perf_counter()-close_started))
        if not clean and config.get('quarantine_on_error', True):
            while True:
                time.sleep(.5)
        connection.close()


class ProcessDisplay:
    """One in-flight render plus one pending frame, no measurement queue loss.

    ``submit`` appends only this frame's new observed points. It never copies
    the cumulative cloud. The parent drains small ACK records on every submit
    and throughout close, avoiding a blocked child pipe when measurements end.
    """

    def __init__(self, *, max_points, frame_total, config, visible_points=12000,
                 start_method='fork', owner_factory=None, renderer_factory=None,
                 startup_timeout_s=65.):
        if type(max_points) is not int or max_points < 1:
            raise ValueError('positive finite shared point capacity required')
        if type(visible_points) is not int or visible_points < 1:
            raise ValueError('positive visible point limit required')
        context = mp.get_context(start_method)
        self.gray_buffer = context.RawArray(ctypes.c_ubyte, 752*480)
        self.cloud_buffer = context.RawArray(ctypes.c_double, max_points*3)
        self.condition = context.Condition()
        # Shared dict values use independent RawValues under one condition.
        self.state = _SharedState(context)
        self.records, self.messages = [], []
        self.submitted = self.dropped = 0
        self.error = None
        self.closed_receipt = None
        self._final_result = None
        self._closing = False
        self._pipe_closed = False
        self._gray = np.frombuffer(self.gray_buffer, np.uint8).reshape(480, 752)
        self._cloud = np.frombuffer(self.cloud_buffer, np.float64).reshape(-1, 3)
        self.connection, child = context.Pipe(duplex=False)
        settings = dict(config, visible_points=visible_points, frame_total=frame_total)
        # Disabling quarantine is an offline mock-only escape hatch.
        if settings.get('quarantine_on_error') is False and owner_factory is None:
            raise ValueError('real HDMI owner must quarantine unconfirmed cleanup')
        self.process = context.Process(target=_display_worker,
                                       args=((self.gray_buffer, self.cloud_buffer, self.state,
                                              self.condition), child, settings,
                                             owner_factory or _real_owner,
                                             renderer_factory or _renderer),
                                       name='r19-hdmi-render', daemon=False)
        self.process.start()
        child.close()
        deadline = time.monotonic() + startup_timeout_s
        while not any(row['kind'] == 'ready' for row in self.messages):
            self._drain()
            if self.error or not self.process.is_alive() or time.monotonic() >= deadline:
                raise DisplayStartupError(self.error or 'display startup did not confirm readiness', self)
            self.connection.poll(.01)

    def _drain(self):
        while not self._pipe_closed:
            try:
                if not self.connection.poll():
                    break
                message = self.connection.recv()
            except (EOFError, BrokenPipeError, OSError):
                self._pipe_closed = True
                break
            self.messages.append(message)
            if message['kind'] == 'ack':
                self.records.append({key: value for key, value in message.items() if key != 'kind'})
            elif message['kind'] == 'error':
                self.error = message['error']
            elif message['kind'] == 'closed':
                self.closed_receipt = message

    def submit(self, gray, points_delta, index, angle):
        self._drain()
        if self._closing or self.error or not self.process.is_alive():
            raise RuntimeError(self.error or 'display is closed or worker exited')
        image, points = np.asarray(gray), np.asarray(points_delta)
        if image.shape != (480, 752) or image.dtype != np.uint8:
            raise ValueError('full 752x480 uint8 raw required')
        if points.ndim != 2 or points.shape[1:] != (3,) or not np.isfinite(points).all():
            raise ValueError('finite observed XYZ delta required')
        if not math.isfinite(float(angle)) or type(index) is not int:
            raise ValueError('finite angle and integer frame ID required')
        with self.condition:
            first = int(self.state['points'])
            if first + len(points) > len(self._cloud):
                raise ValueError('shared cloud capacity exceeded; no measurement points dropped')
            self._cloud[first:first+len(points)] = points
            self._gray[:] = image
            self.dropped += int(self.state['pending'] != 0)
            self.submitted += 1
            self.state['points'] = first + len(points)
            self.state['frame_id'], self.state['angle'] = index, angle
            self.state['pending'] = 1
            self.condition.notify_all()

    def close(self, timeout_s=30.):
        if self._final_result is not None:
            if self._final_result['error']:
                raise RuntimeError(self._final_result['error'])
            return self._final_result
        self._closing = True
        with self.condition:
            self.state['closing'] = 1
            self.condition.notify_all()
        deadline = time.monotonic() + timeout_s
        while self.process.is_alive() and time.monotonic() < deadline:
            self._drain()
            self.process.join(min(.02, max(0., deadline-time.monotonic())))
        self._drain()
        joined = not self.process.is_alive()
        clean = bool(joined and self.closed_receipt and self.closed_receipt['clean'] and
                     self.process.exitcode == 0)
        error = self.error
        if not clean:
            error = error or 'display/HDMI cleanup unconfirmed; retain hardware lock; do not terminate child'
        if not error and self.submitted != len(self.records) + self.dropped:
            error = 'display ACK/drop accounting mismatch'
        result = dict(submitted=self.submitted, acknowledged=len(self.records), dropped=self.dropped,
                      records=self.records, worker_joined=joined, worker_exitcode=self.process.exitcode,
                      owner_closed=clean, close_receipt=self.closed_receipt, error=error,
                      cloud_points=int(self.state['points']), measurement_frames_dropped=0)
        self._final_result = result
        if joined:
            self.connection.close()
        if error:
            raise RuntimeError(error)
        return result

    @property
    def cleanup_confirmed(self):
        return bool(self.closed_receipt and self.closed_receipt['clean'] and
                    not self.process.is_alive() and self.process.exitcode == 0)


class _SharedState:
    """Picklable fixed metadata; callers hold the shared condition."""

    def __init__(self, context):
        self.values = {name: context.RawValue(ctypes.c_double if name == 'angle' else ctypes.c_int64, 0)
                       for name in ('pending', 'closing', 'points', 'frame_id', 'angle')}

    def __getitem__(self, name):
        return self.values[name].value

    def __setitem__(self, name, value):
        self.values[name].value = value
