"""SECM v1 driver and Page B adapter; accesses only the declared 0x700 window.

The hardware packet counter changes after its tables and scalars are published. The
live loop allows one packet at a time and reads the snapshot before submitting another.
All fitting and scalar measurements below come from PL; NumPy projection is for drawing.
"""
import math
import time
from dataclasses import replace

import numpy as np

BASE, END, MAGIC, VERSION = 0x700, 0x7ff, 0x5345434d, 1
Q16, Q24 = 65536., 16777216.
I32_MIN, I32_MAX = -(1 << 31), (1 << 31) - 1


def declared(manifest):
    """Check declaration without constructing or touching MMIO."""
    entry = manifest.get('section_measure') or {}
    if not entry.get('present'):
        return False
    if (entry.get('register_window') != [BASE, END] or entry.get('magic') != MAGIC
            or entry.get('version', VERSION) != VERSION):
        raise ValueError('unsupported section_measure manifest window/magic/version')
    return True


def backend_for(manifest, requested):
    if requested == 'arm':
        return 'arm'
    present = declared(manifest)
    if requested == 'pl' and not present:
        raise RuntimeError('PL section not declared; use --section-backend arm explicitly')
    return 'pl' if present else 'arm'


def signed(word):
    word = int(word) & 0xffffffff
    return word - (1 << 32) if word & 0x80000000 else word


def quantize(value, scale):
    if not math.isfinite(value):
        raise ValueError('non-finite section configuration')
    q = round(value * scale)
    if not I32_MIN <= q <= I32_MAX:
        raise ValueError('section configuration exceeds signed 32-bit range')
    return q


class PLSection:
    def __init__(self, manifest, mmio_factory, phys_addr):
        if not declared(manifest):
            raise RuntimeError('section_measure not declared; no MMIO created')
        self._mmio = mmio_factory(int(phys_addr), 4096)
        if self._read(0) != MAGIC:
            raise RuntimeError('PL section magic mismatch')
        self.packet = self._read(0x50)
        self.dropped = self._read(0x9c)
        self.cfg = None

    def _offset(self, offset):
        if type(offset) is not int or not 0 <= offset <= 0xfc or offset & 3:
            raise ValueError('section access outside 0x700-0x7ff')
        return BASE + offset

    def _read(self, offset):
        return int(self._mmio.read(self._offset(offset))) & 0xffffffff

    def _write(self, offset, value):
        self._mmio.write(self._offset(offset), int(value) & 0xffffffff)

    def configure(self, basis, cfg, *, preview=True):
        if cfg.rows is not None:
            raise ValueError('PL section has no image-row ROI; use --section-backend arm')
        if not 3 <= cfg.min_points <= 1024:
            raise ValueError('PL section min_points must be 3..1024')
        values = [quantize(float(v), Q16) for v in basis.origin]
        values += [max(-16777216, min(16777215, quantize(float(v), Q24)))
                   for v in np.r_[basis.es, basis.eh]]
        zmin = I32_MIN if cfg.z_min_mm is None else quantize(cfg.z_min_mm, Q16)
        zmax = I32_MAX if cfg.z_max_mm is None else quantize(cfg.z_max_mm, Q16)
        thresholds = [quantize(v, Q16) for v in (cfg.jump_mm, cfg.break_mm, cfg.level_mm)]
        # Validate everything before changing the block.
        self._write(4, 0)
        for offset, value in zip(range(8, 0x2c, 4), values):
            self._write(offset, value)
        for offset, value in zip((0x2c, 0x30, 0x34, 0x38, 0x3c, 0x40),
                                 [zmin, zmax, *thresholds, cfg.min_points]):
            self._write(offset, value)
        self._write(4, 3 if preview else 1)
        self.zmin, self.zmax, self.profile_cfg = zmin, zmax, cfg
        self.cfg = {'version': VERSION, 'preview': bool(preview), 'origin_q16': values[:3],
                        'es_q24': values[3:6], 'eh_q24': values[6:9], 'z_min_q16': zmin, 'z_max_q16': zmax,
                        'jump_q16': thresholds[0], 'break_q16': thresholds[1], 'level_q16': thresholds[2],
                        'min_points': cfg.min_points, 'auto_depth_source': 'previous_frame_in_window_z_p05'}
        return self.cfg

    def disable(self):
        self._write(4, 0)

    def effective_config(self):
        """ARM comparison uses exactly this frame's quantized hardware Z bounds."""
        return replace(self.profile_cfg, z_min_mm=self.zmin / Q16, z_max_mm=self.zmax / Q16,
                       auto_depth_mm=0.)

    def next_window(self, xyz):
        """Called only after reading a completed frame, before the next submission."""
        cfg = self.profile_cfg
        if cfg.z_min_mm is not None or cfg.z_max_mm is not None or not cfg.auto_depth_mm:
            return None
        xyz = np.asarray(xyz).reshape(-1, 3)
        z = xyz[:, 2]
        z = z[np.isfinite(z) & (z >= self.zmin / Q16) & (z <= self.zmax / Q16)]
        if not len(z):
            return None  # keep prior bounds; no fabricated nearest surface
        new = min(I32_MAX, max(I32_MIN, math.floor((np.percentile(z, 5) + cfg.auto_depth_mm) * Q16)))
        self._write(0x30, new)
        self.zmax = new
        return new / Q16

    def frame(self, *, timeout_s=2., live=False, target=None, monotonic=time.monotonic, sleep=time.sleep):
        """Wait for exactly one new solver result; never reuse a previous packet.

        live: the PL solves every camera frame; wait for `target` (the packet ARM took) when
        given, else take any newer result. A result published during the read is re-read.
        """
        expected = (self.packet + 1) & 0xffffffff
        deadline = monotonic() + timeout_s
        while True:
            packet = self._read(0x50)
            ahead = (packet - target) & 0xffffffff if target is not None else 0
            if packet == expected or (live and packet != self.packet and (target is None or ahead < 1 << 31)):
                break
            if packet != self.packet:
                raise RuntimeError('section packet identity advanced unexpectedly')
            if monotonic() >= deadline:
                raise TimeoutError('PL section solver did not publish current packet')
            sleep(.0005)
        r = {o: self._read(o) for o in (*range(0x54, 0x80, 4), 0x9c, 0xa0, 0xa4, 0xa8)}
        nseg, status = r[0x58] & 31, r[0x5c]
        nsteps = (status >> 16) & 15
        if nseg > 16 or r[0x54] > 1024:
            raise RuntimeError('invalid PL section result counts')
        out = {'backend': 'pl', 'version': VERSION, 'packets': packet, 'points': r[0x54],
                   'overflow': bool(r[0x58] >> 31), 'dropped': r[0x9c], 'cycles': r[0xa8],
                   'reference': (status >> 8) & 15 if status & 8 else None,
                   'step_segment': (status >> 12) & 15 if status & 1 else None,
                   'step_mm': signed(r[0x60]) / Q16 if status & 1 else None,
                   'gap_mm': signed(r[0x64]) / Q16 if status & 2 else None,
                   'gap_depth_mm': signed(r[0x68]) / Q16 if status & 4 else None,
                   'noise_mm': signed(r[0x6c]) / Q16, 'level_mm': signed(r[0x70]) / Q16,
                   'reference_rms_mm': signed(r[0x74]) / Q16 if status & 8 else None,
                   'reference_slope': signed(r[0x78]) / Q24 if status & 8 else None,
                   'spacing_mm': signed(r[0x7c]) / Q16,
                   'gap_edges': (signed(r[0xa0]) / Q16, signed(r[0xa4]) / Q16) if status & 2 else None,
                   'steps': [], 'segment_list': []}
        for i in range(max(nseg, nsteps)):
            self._write(0x44, (i << 8) | min(i, 14))
            if i < nsteps:
                out['steps'].append({'at_s_mm': signed(self._read(0x80)) / Q16,
                                         'step_mm': signed(self._read(0x84)) / Q16})
            if i < nseg:
                out['segment_list'].append({'n': self._read(0x88), 's0': signed(self._read(0x8c)) / Q16,
                    's1': signed(self._read(0x90)) / Q16, 'offset_mm': signed(self._read(0x94)) / Q16,
                    'a': signed(self._read(0x98)) / Q24})
        if self._read(0x50) != packet:
            if live and timeout_s > 0:
                return self.frame(timeout_s=timeout_s - .05, live=True, monotonic=monotonic, sleep=sleep)
            raise RuntimeError('section snapshot changed during read')
        if any(index is not None and index >= nseg for index in (out['reference'], out['step_segment'])):
            raise RuntimeError('invalid PL section segment index')
        out['dropped_delta'] = (out['dropped'] - self.dropped) & 0xffffffff
        out['valid'] = not out['overflow'] and not out['dropped_delta']
        self.packet, self.dropped = packet, out['dropped']
        return out


class SectionWindowMismatch(RuntimeError):
    """The PL section result counted a different window than this frame's points select.

    In live mode a packet already in flight keeps the Z window it started with, so right after
    the window moves (a new object) one result can belong to the previous window."""


def profile_frame(result, xyz, basis, cfg, point_rows=None, rows=None):
    """Adapt a PL result to the existing tracker. Only plotting is projected on ARM."""
    from r33.profile_live import row_fields, select_window
    keep, window = select_window(xyz, point_rows, cfg)
    if result['points'] != min(int(keep.sum()), 1024):
        raise SectionWindowMismatch('PL section points do not match this frame and Z window')
    sh = basis.project(np.asarray(xyz)[keep])
    order = np.argsort(sh[:, 0], kind='stable')
    sh = sh[order]
    out = dict(result, hardware_result=result, segments=len(result['segment_list']), profile=sh, window=window,
               grade='PL preview grade' if result.get('preview', True) else 'PL measurement grade',
               step_angle_deg=None, gap_unresolved=False)
    out['segment_list'] = [dict(g) for g in result['segment_list']]
    for g in out['segment_list']:
        # Q16 output bounds can round across the float display projection by a few units.
        g['i0'] = int(np.searchsorted(sh[:, 0], g['s0'] - .002))
        g['i1'] = int(np.searchsorted(sh[:, 0], g['s1'] + .002, side='right'))
        shown = sh[g['i0']:g['i1']]
        g['b'] = float(np.median(shown[:, 1] - g['a'] * shown[:, 0])) if len(shown) else 0.
        g['rms'] = (result['reference_rms_mm']
                    if out['reference'] is not None and g is out['segment_list'][out['reference']] else None)
    if out['gap_mm'] is not None and point_rows is not None and rows is not None and len(sh):
        prow = np.asarray(point_rows)[keep][order]
        left, right = (int(np.argmin(abs(sh[:, 0] - edge))) for edge in out['gap_edges'])
        r0, r1 = sorted((int(prow[left]), int(prow[right])))
        fields = row_fields(rows)
        rejected = ((np.asarray(fields['qualified_count'])[r0 + 1:r1] > 0)
                    & (np.asarray(fields['preview_count'])[r0 + 1:r1] == 0))
        out['gap_unresolved'] = bool(len(rejected) and rejected.mean() > .5)
        if out['gap_unresolved']:
            out.update(gap_mm=None, gap_depth_mm=None, gap_edges=None)
    if not out['valid']:
        out.update(step_mm=None, gap_mm=None, gap_depth_mm=None, steps=[])
    return out


def comparison(frame_number, pl, arm, *, next_z_max_mm):
    """JSON-safe per-frame evidence; does not change PL values or tolerances."""
    keys = ('points', 'segments', 'step_mm', 'gap_mm', 'gap_depth_mm', 'noise_mm', 'level_mm',
            'spacing_mm', 'reference', 'step_segment', 'gap_unresolved')
    values = {}
    for key in keys:
        p, a = pl.get(key), arm.get(key)
        values[key] = {'pl': p, 'arm': a, 'delta': None if p is None or a is None else float(p) - float(a)}
    return {'frame': frame_number, 'packet': pl['packets'], 'version': VERSION, 'backend': 'pl',
                'valid': pl['valid'], 'overflow': pl['overflow'], 'dropped_delta': pl['dropped_delta'],
                'window': pl['window'], 'next_z_max_mm': next_z_max_mm, 'values': values,
                'pl_steps': pl['steps'], 'arm_steps': arm['steps'], 'pl_segments': pl['segment_list'],
                'arm_segments': arm['segment_list'], 'reference_rms_mm': pl['reference_rms_mm'],
                'reference_slope': pl['reference_slope'], 'solver_cycles': pl['cycles'],
                'hardware_result': pl['hardware_result']}
