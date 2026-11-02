"""R40 PL point-cloud view control: pfo3_frontend registers 0x400-0x4ff only.

Display-only. The view snoops the PL geometry records of every PFO3 packet; it never
changes PFO3 output, DMA or the frozen replay scope (r33/scope.py). This module has
its own guard: it can touch nothing outside the 256-byte view window.
Register contract and bit-exact behaviour: model/r40_model.py in the R40 workspace.
"""
import math
import time

import numpy as np

BASE = 0x400
WINDOW = 0x100
MAGIC = 0x52343056
VOXEL_WORDS = 12288
PANEL_PIXELS = 53760
CTRL_OVERLAY, CTRL_INSERT, CTRL_AUTO, CTRL_CLEAR, CTRL_PREVIEW, CTRL_FREEZE = 1, 2, 4, 8, 16, 32


def default_params(*, voxel_mm=0.6, radius_mm=19.2, z0_mm=-1.0, height_mm=48.0,
                   elevation_deg=23.0, yaw_deg=-55.0, yaw_step=2, splat=3, window=(808, 128)):
    """Grid 64x64x96 centred on the turntable axis; view scale fits radius x height.

    Screen units: one 'doubled voxel' (voxel_mm/2). k is pixels per unit in Q12 on the
    224x240 half-resolution panel (shown 2x in the 448x480 HDMI window).
    """
    unit = voxel_mm / 2
    r_units, h_units = radius_mm / unit, height_mm / unit
    e = math.radians(elevation_deg)
    k_h = 200.0 / (2 * r_units)
    k_v = 220.0 / (math.cos(e) * h_units + 2 * math.sin(e) * r_units)
    k = min(k_h, k_v)
    iz_centre = (height_mm / 2 - z0_mm) / voxel_mm
    return dict(inv=int(round(65536 / voxel_mm)), ox=int(round(-radius_mm * 65536)),
                oy=int(round(-radius_mm * 65536)), oz=int(round(z0_mm * 65536)),
                yaw_fixed=int(round(yaw_deg / 360 * 1024)) % 1024, yaw_step=int(yaw_step),
                k=int(round(k * 4096)), kse=int(round(k * 4096 * math.sin(e))),
                kce=int(round(k * 4096 * math.cos(e))), sx0=112, sy0=120,
                zc=int(round(2 * iz_centre)), wx=int(window[0]), wy=int(window[1]), splat=int(splat),
                voxel_mm=voxel_mm, elevation_deg=elevation_deg)


class PLCloudView:
    def __init__(self, mmio_factory, phys_addr):
        self._mmio = mmio_factory(int(phys_addr), 4096)
        if self._read(0x68) != MAGIC:
            raise RuntimeError('R40 PL cloud view absent (magic mismatch)')

    def _write(self, offset, value):
        if type(offset) is not int or not 0 <= offset < WINDOW or offset & 3:
            raise ValueError('PL view offset outside its 0x400-0x4ff window')
        self._mmio.write(BASE + offset, int(value) & 0xffffffff)

    def _read(self, offset):
        if type(offset) is not int or not 0 <= offset < WINDOW or offset & 3:
            raise ValueError('PL view offset outside its 0x400-0x4ff window')
        return int(self._mmio.read(BASE + offset)) & 0xffffffff

    def configure(self, p):
        for offset, value in ((0x04, p['inv']), (0x08, p['ox']), (0x0c, p['oy']), (0x10, p['oz']),
                              (0x14, (p['yaw_step'] << 16) | p['yaw_fixed']), (0x18, p['k']),
                              (0x1c, p['kse']), (0x20, p['kce']),
                              (0x24, ((p['sy0'] & 0x7ff) << 16) | (p['sx0'] & 0x7ff)), (0x28, p['zc']),
                              (0x2c, (p['wy'] << 16) | p['wx']), (0x30, p['splat'])):
            self._write(offset, value)
        readback = [self._read(o) for o in (0x04, 0x08, 0x0c, 0x10)]
        if readback != [p['inv'] & 0x3ffff] + [v & 0xffffffff for v in (p['ox'], p['oy'], p['oz'])]:
            raise RuntimeError('PL view configuration readback mismatch')

    def start(self, *, auto_rotate=True, include_preview=False, timeout_s=1.0):
        """Clear the grid and counters, then enable ingest + overlay (preview-grade points too if asked)."""
        ctrl = (CTRL_OVERLAY | CTRL_INSERT | (CTRL_AUTO if auto_rotate else 0)
                | (CTRL_PREVIEW if include_preview else 0))
        self._write(0x00, ctrl | CTRL_CLEAR)
        deadline = time.monotonic() + timeout_s
        while self._read(0x64) & 1:
            if time.monotonic() > deadline:
                raise TimeoutError('PL view clear did not finish')
            time.sleep(.0005)
        self._ctrl = ctrl

    def status(self):
        names = ('inserted', 'dropped', 'frames_committed', 'frames_discarded', 'occupied', 'out_of_range',
                 'render_count', 'last_render_cycles', 'swap_count', 'status', 'magic', 'last_yaw',
                 'republish_count')
        values = dict(zip(names, (self._read(o) for o in range(0x40, 0x74, 4))))
        values['frozen'] = bool(values['status'] & 2)
        values['last_pub_buf'] = (values['status'] >> 2) & 1
        return values

    def _readback(self, address):
        self._write(0x34, address)
        for _ in range(10000):
            if self._read(0x64) & 8:
                return self._read(0x38)
        raise TimeoutError('PL view readback timeout')

    def snapshot(self, *, settle_renders=2, timeout_s=2.0):
        """Freeze after `settle_renders` complete renders, read grid + panel, then resume.

        Called after the last packet: renders counted here started after every commit
        drained, so the panel is a pure function of the final grid and last_yaw.
        """
        time.sleep(.05)
        deadline = time.monotonic() + timeout_s
        first = self._read(0x58)
        while (self._read(0x58) - first) & 0xffffffff < settle_renders:
            if time.monotonic() > deadline:
                raise TimeoutError('PL view renders did not advance (no video frames?)')
            time.sleep(.002)
        self._write(0x00, self._ctrl | CTRL_FREEZE)
        while not self._read(0x64) & 2:
            if time.monotonic() > deadline:
                raise TimeoutError('PL view freeze not reached')
            time.sleep(.0005)
        state = self.status()
        started = time.perf_counter()
        voxels = np.array([self._readback(i) for i in range(VOXEL_WORDS)], np.uint32)
        base = state['last_pub_buf'] * PANEL_PIXELS
        panel = np.array([self._readback(0x80000000 | (base + i)) for i in range(PANEL_PIXELS)], np.uint8)
        readback_s = time.perf_counter() - started
        self._write(0x00, self._ctrl)
        return state, voxels, panel.reshape(240, 224), readback_s

    def set_window(self, x, y):
        """Move the 448x480 window; the video side takes it with the next published render."""
        self._write(0x2c, (int(y) << 16) | int(x))

    def set_overlay(self, enabled):
        """Change only OVERLAY. Never pulse CLEAR or change ingest/preview/rotation."""
        ctrl = self._read(0x00) & ~CTRL_CLEAR
        ctrl = (ctrl | CTRL_OVERLAY) if enabled else (ctrl & ~CTRL_OVERLAY)
        self._write(0x00, ctrl)
        self._ctrl = ctrl

    def set_insert(self, enabled):
        """Change only INSERT (points of packets ingested from now on)."""
        ctrl = self._read(0x00) & ~CTRL_CLEAR
        ctrl = (ctrl | CTRL_INSERT) if enabled else (ctrl & ~CTRL_INSERT)
        self._write(0x00, ctrl)
        self._ctrl = ctrl

    def clear(self, timeout_s=1.0):
        """Empty the grid and counters; other control bits stay as they are."""
        ctrl = self._read(0x00) & ~CTRL_CLEAR
        self._write(0x00, ctrl | CTRL_CLEAR)
        deadline = time.monotonic() + timeout_s
        while self._read(0x64) & 1:
            if time.monotonic() > deadline:
                raise TimeoutError('PL view clear did not finish')
            time.sleep(.0005)
        self._ctrl = ctrl

    def stop_overlay(self):
        self.set_overlay(False)
