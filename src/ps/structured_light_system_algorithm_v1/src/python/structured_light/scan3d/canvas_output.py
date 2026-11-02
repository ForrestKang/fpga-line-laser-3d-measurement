"""Persistent HDMI presentation of an already rendered BGR canvas.

No camera extraction or geometry is performed here. Allocation ownership and
halt-before-free are inherited from PreviewHDMIOutput.
"""
from __future__ import annotations

import time
import cv2
import numpy as np

from ..p2.hdmi_output import WIDTH, HEIGHT
from ..p2.preview_output import PreviewHDMIOutput


class PersistentCanvasOutput(PreviewHDMIOutput):
    def _trace_stage(self, stage, **values):
        observer = getattr(self, '_diagnostic_stage', None)
        if observer is not None:
            try:
                observer(stage, **values)
            except Exception:
                pass

    def show_canvas(self, canvas: np.ndarray) -> dict:
        if (not isinstance(canvas, np.ndarray) or canvas.shape != (HEIGHT, WIDTH, 3)
                or canvas.dtype != np.uint8 or not canvas.flags.c_contiguous):
            raise ValueError('canvas must be contiguous 1280x720 BGR uint8')
        if self.quarantined:
            raise RuntimeError('canvas buffers quarantined')
        started = time.monotonic()
        if self.geometry is None:
            self._initialize(752, 480, 0, False)
        initialized = time.monotonic()
        self._trace_stage('canvas_status_begin')
        self._status()
        park = int(self.dma.read(0x28))
        if ((park >> 16) & 31) != self.current or (park & 31) != self.current:
            raise RuntimeError('unexpected HDMI active-store ownership')
        target = (self.current + 1) % 3
        frame = self.buffers[target]
        # Fill G,B,R and the zero pad in one native pass over the inactive
        # store. A negative source channel explicitly writes zero padding.
        self._trace_stage('canvas_fill_begin', target_store=target)
        cv2.mixChannels([canvas], [np.asarray(frame)], [0, 1, 1, 0, 2, 2, -1, 3])
        self._trace_stage('canvas_fill_complete', target_store=target)
        self._trace_stage('canvas_flush_begin', target_store=target)
        frame.flush()
        self._trace_stage('canvas_flush_complete', target_store=target)
        filled = time.monotonic()
        self._trace_stage('canvas_switch_begin', target_store=target)
        self.dma.write(4, 0x1000)
        self.dma.write(0x28, (int(self.dma.read(0x28)) & ~31) | target)
        self._trace_stage('canvas_wait_begin', target_store=target)
        self._wait_store(target)
        self._trace_stage('canvas_wait_complete', target_store=target)
        self.current = target
        self.updates += 1
        self._trace_stage('canvas_receipt_begin', target_store=target)
        result = dict(active_store=target, update_count=self.updates, persistent_stores=3,
                      initialization_s=initialized-started, fill_s=filled-initialized,
                      switch_s=time.monotonic()-filled, vdma_status=self._status(),
                      hdmi_memory_lane_order='G,B,R,X', hdmi_acknowledged=True,
                      acknowledgement_basis='VDMA frame event and active park store; not optical proof')
        self.snapshots.append(result)
        del self.snapshots[:-8]
        return result

    def show(self, canvas):
        return self.show_canvas(canvas)


def create_canvas_output(overlay, manifest_path):
    from ..p2.hdmi_output import create_output
    base = create_output(overlay, manifest_path)
    return PersistentCanvasOutput(base.clock, base.vtc, base.dma, base.allocator,
                                  timeout_s=base.timeout_s)


__all__ = ['PersistentCanvasOutput', 'create_canvas_output']
