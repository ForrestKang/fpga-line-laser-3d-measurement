"""Same-frame raw and SLC1 pixel contour trial, without metric geometry."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import threading

import cv2
import numpy as np

from ..p2.hdmi_owner import RemoteHDMIOutput


CAMERA_IMAGE = (24, 128, 752, 480)
PIXEL_PLOT = (854, 128, 370, 479)
TRIAL_BANNER = 'TRIAL PIXEL PREVIEW / UNCALIBRATED / NO mm'


def _text(canvas, text, position, scale=.5, color=(185, 210, 225)):
    cv2.putText(canvas, str(text), position, cv2.FONT_HERSHEY_SIMPLEX,
                scale, color, 1, cv2.LINE_AA)


class TrialPixelDisplay:
    """Synchronous bounded IPC to the independently owned HDMI allocation.

    Layout and text rendering are warmed before configure returns. Motion and
    capture admission belong to the caller; this class never commands either.
    """

    def __init__(self, runtime, manifest, output):
        self.runtime = runtime
        self.manifest = Path(manifest)
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self._owner = None
        self._configure_started = False
        self._configured = self._closed = False
        self._worker_unconfirmed = False
        self._error = None
        self._close_receipt = None
        self._last_canvas = self._last_metadata = None
        self._lock = threading.Lock()
        self._plot_roi = None
        self._base = self._background()

    @staticmethod
    def _background(roi=(0, 0, 752, 480)):
        canvas = np.empty((720, 1280, 3), np.uint8)
        canvas[:] = (25, 19, 12)
        cv2.rectangle(canvas, (0, 0), (1279, 47), (20, 26, 47), -1)
        _text(canvas, TRIAL_BANNER, (24, 32), .76, (80, 210, 255))
        _text(canvas, 'LIVE CAMERA / NATIVE RAW GRAY 752 x 480', (24, 113), .52)
        _text(canvas, 'SAME FRAME / ACCEPTED PIXEL SAMPLES', (808, 113), .48)
        x, y, width, height = PIXEL_PLOT
        x0, y0, x1, y1 = roi
        for u in np.linspace(x0, x1, 6, dtype=int):
            px = x+round((u-x0)/(x1-x0)*width)
            cv2.line(canvas, (px, y), (px, y+height), (61, 47, 34), 1)
            _text(canvas, str(u), (px-10, y+height+20), .37)
        for v in np.linspace(y0, y1, 5, dtype=int):
            py = y+round((v-y0)/(y1-y0)*height)
            cv2.line(canvas, (x, py), (x+width, py), (61, 47, 34), 1)
            _text(canvas, str(v), (x-35, py+4), .37)
        cv2.rectangle(canvas, (x, y), (x+width, y+height), (100, 110, 120), 1)
        _text(canvas, 'u (px)', (x+width//2-20, y+height+45), .45)
        _text(canvas, 'v (px)', (805, 94), .4)
        _text(canvas, f'ZOOM ROI ({x0},{y0})-({x1},{y1})', (808, 638), .46)
        _text(canvas, 'POINTS ONLY / NO GAP FILL / NO REFERENCE COMPARISON', (24, 697), .5)
        # OpenCV text and line drawing have already run before any ARM.
        return canvas

    def configure(self):
        with self._lock:
            if self._closed or self._worker_unconfirmed:
                raise RuntimeError('trial display is closed or owner is unconfirmed')
            if self._configured:
                return dict(configured=True, owner_pid=self._owner.owner_pid)
            hardware_lock = self.runtime.hardware_lock
            if not hardware_lock.acquired or hardware_lock._handle is None:
                raise RuntimeError('trial display requires the acquired runtime hardware lock')
            fd = hardware_lock._handle.fileno()
            previous = os.environ.get('SL_HDMI_PROCESSED_CANVAS')
            self._configure_started = True
            try:
                os.environ['SL_HDMI_PROCESSED_CANVAS'] = '1'
                self._owner = RemoteHDMIOutput(self.manifest,
                    owner_log=self.output/'trial_hdmi_owner.log', startup_timeout_s=60.,
                    hardware_lock_fd=fd)
                self._owner.configure()
            except BaseException as exc:
                self._worker_unconfirmed = True
                self._error = type(exc).__name__+': '+str(exc)
                raise
            finally:
                if previous is None:
                    os.environ.pop('SL_HDMI_PROCESSED_CANVAS', None)
                else:
                    os.environ['SL_HDMI_PROCESSED_CANVAS'] = previous
            # PersistentCanvasOutput initializes and ACKs its stores on show;
            # the runner requires two real show ACKs before issuing ARM.
            self._configured = True
            return dict(configured=True, owner_pid=self._owner.owner_pid,
                        hdmi_acknowledged=False, prewarm_complete=True)

    def show(self, frame, centers_q12, accepted, reasons, stage):
        centers = np.asarray(centers_q12)
        mask = np.asarray(accepted)
        if (centers.shape != (480,) or not np.issubdtype(centers.dtype, np.integer)
                or mask.shape != (480,) or mask.dtype != np.bool_ or len(reasons) != 480):
            raise ValueError('trial display requires 480 integer Q12 centers, bool accepts and reasons')
        if len(frame.raw) != 752*480:
            raise ValueError('trial display requires the full native raw frame')
        if np.any(mask & ((centers < 0) | (centers >= 752*4096))):
            raise ValueError('accepted pixel center is outside the native camera width')
        try:
            roi = tuple(frame.compact_packet.configuration.roi)
        except AttributeError as exc:
            raise ValueError('trial pixel zoom requires the same-frame SLC1 configuration ROI') from exc
        if (len(roi) != 4 or any(type(value) is not int for value in roi)
                or not 0 <= roi[0] < roi[2] <= 752 or not 0 <= roi[1] < roi[3] <= 480):
            raise ValueError('invalid same-frame SLC1 configuration ROI')
        x0, y0, x1, y1 = roi
        rows = np.flatnonzero(mask)
        if (np.any((rows < y0) | (rows >= y1))
                or np.any((centers[rows] < x0*4096) | (centers[rows] >= x1*4096))):
            raise ValueError('accepted pixel sample is outside the same-frame SLC1 ROI')
        stage_text = str(stage).encode('ascii', errors='replace').decode('ascii')[:40]
        with self._lock:
            if not self._configured or self._closed or self._worker_unconfirmed:
                raise RuntimeError('trial display is not configured or owner is unconfirmed')
            if self._plot_roi != roi:
                self._base = self._background(roi)
                self._plot_roi = roi
            canvas = self._base.copy()
            raw = np.frombuffer(frame.raw, np.uint8).reshape(480, 752)
            x, y, width, height = CAMERA_IMAGE
            canvas[y:y+height, x:x+width] = raw[:, :, None]
            u = centers[rows].astype(float)/4096.
            px, py, pw, ph = PIXEL_PLOT
            xs = px+np.rint((u-x0)/(x1-x0)*pw).astype(int)
            ys = py+np.rint((rows-y0)/(y1-y0)*ph).astype(int)
            # Individual accepted samples only: no joins or interpolation over
            # rejected/missing rows, no assumed axes and no metric conversion.
            canvas[ys, xs] = (70, 235, 180)
            _text(canvas, f'REQUEST {frame.request_id} / EPOCH {frame.epoch} / STAGE {stage_text}'
                  f' / ACCEPTED {len(rows)}/480', (24, 78), .58, (80, 210, 255))
            rejection_counts = dict(Counter(str(reason) for reason, good in zip(reasons, mask)
                                           if not good and reason is not None))
            summary = ' / '.join(f'{key}:{value}' for key, value in sorted(rejection_counts.items()))
            _text(canvas, 'REJECTED: '+(summary or 'NONE')[:135], (24, 668), .44)
            receipt = dict(request_id=int(frame.request_id), camera_epoch=int(frame.epoch),
                stage=stage_text, accepted_rows=int(len(rows)), rejection_counts=rejection_counts,
                same_frame_overlay=True, reference_compared=False, rendered=True,
                hdmi_acknowledged=False, source='live_slc1_pixel_trial',
                title=TRIAL_BANNER, plot_roi=list(roi), metric_calibrated=False, physical_accuracy_verified=False)
            self._last_canvas, self._last_metadata = canvas, receipt
            acknowledged = self._owner.show_canvas(canvas)
            receipt['hdmi_acknowledged'] = bool(isinstance(acknowledged, dict)
                                               and acknowledged.get('hdmi_acknowledged') is True)
            receipt['sink_receipt'] = acknowledged
            return dict(receipt)

    def _save_last(self):
        if self._last_canvas is None:
            return None
        image_path = self.output/'LAST_TRIAL_PIXEL_CANVAS.png'
        if not cv2.imwrite(str(image_path), self._last_canvas):
            raise OSError('failed to save last trial pixel canvas')
        metadata = dict(self._last_metadata, file=image_path.name,
                        sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
                        saved_after_confirmed_owner_close=True)
        metadata_path = self.output/'LAST_TRIAL_PIXEL_CANVAS.json'
        metadata_path.write_text(json.dumps(metadata, indent=2)+'\n', encoding='utf-8')
        return dict(image=str(image_path), metadata=str(metadata_path), **metadata)

    def close(self, timeout_s=5.):
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s <= 0:
            raise ValueError('positive close timeout required')
        if not self._lock.acquire(timeout=timeout_s):
            return dict(closed=False, passed=False, worker_unconfirmed=True,
                        error='trial display operation is still active; retain owner and hardware lock')
        try:
            if self._closed:
                return dict(self._close_receipt)
            owner_receipt = None
            if self._owner is None and self._configure_started:
                return dict(closed=False, passed=False, worker_unconfirmed=True, error=self._error)
            if self._owner is not None:
                try:
                    owner_receipt = self._owner.close()
                    if not (isinstance(owner_receipt, dict)
                            and owner_receipt.get('closed_confirmed') is True
                            and owner_receipt.get('owner_exited') is True):
                        raise RuntimeError('trial HDMI owner did not confirm stop and process exit')
                except BaseException as exc:
                    self._worker_unconfirmed = True
                    self._error = type(exc).__name__+': '+str(exc)
                    return dict(closed=False, passed=False, worker_unconfirmed=True,
                                error=self._error, owner_receipt=owner_receipt)
            self._worker_unconfirmed = False
            self._closed = True
            self._close_receipt = dict(closed=True, passed=True, worker_unconfirmed=False,
                                       owner_receipt=owner_receipt, last_canvas=None)
            try:
                self._close_receipt['last_canvas'] = self._save_last()
            except BaseException as exc:
                self._close_receipt.update(passed=False, artifact_error=type(exc).__name__+': '+str(exc))
            return dict(self._close_receipt)
        finally:
            self._lock.release()


__all__ = ['TrialPixelDisplay', 'TRIAL_BANNER', 'CAMERA_IMAGE', 'PIXEL_PLOT']
