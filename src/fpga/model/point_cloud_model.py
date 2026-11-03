"""R40 PL cloud view: bit-exact reference of point_cloud_renderer.sv.

Integer arithmetic only. Every rounding, shift, clip and traversal order here is
the RTL contract; the RTL is verified against this file, not the other way round.

Pipeline
  1. points  : geometry records (axis XYZ Q16 + flags) of one PFO3 packet are staged;
               a packet that ends without error commits them, otherwise they are dropped.
  2. voxels  : committed points set bits in a GX*GY*GZ occupancy grid (32-bit words, x fastest).
  3. render  : back-to-front orthographic splat of set voxels into a PW*PH 4-bit panel.
  4. overlay : a WIN_W*WIN_H window of the 1280x720 BGRX video shows palette(panel) at 2x;
               colour 0 is transparent (the ARM canvas pixel passes through).
"""
import math

import numpy as np

GX, GY, GZ = 64, 64, 96
WORDS_PER_ROW = GX // 32
VOXEL_WORDS = GZ * GY * WORDS_PER_ROW          # 12288
PW, PH = 224, 240
PANEL_PIXELS = PW * PH                         # 53760
WIN_W, WIN_H = 2 * PW, 2 * PH
STAGE_DEPTH = 2048
MAGIC = 0x52343056                             # 'R40V'

DEFAULTS = dict(ctrl=0, inv=87381, ox=-24 * 65536, oy=-24 * 65536, oz=-2 * 65536,
                yaw_fixed=0, yaw_step=2, k=6144, kse=2401, kce=5656,
                sx0=112, sy0=120, zc=69, wx=808, wy=128, splat=3)


def sin_table():
    """1024-entry Q14 sine; the RTL ROM is generated from exactly this list."""
    return [int(round(16384 * math.sin(2 * math.pi * i / 1024))) for i in range(1024)]


SIN = sin_table()

# Height palette (index 0 transparent), the ARM point view's soft ramp: index i is
# ratio (i-1)/14 of RGB (105,150,210) -> (220,225,100), truncated like point_raster.c.
PALETTE = [0x000000,
           0x6996d2, 0x719bca, 0x79a0c2, 0x81a6ba, 0x89abb2, 0x92b0aa, 0x9ab6a2, 0xa2bb9b,
           0xaac093, 0xb2c68b, 0xbbcb83, 0xc3d07b, 0xcbd673, 0xd3db6b, 0xdce164]


def s32(v):
    v &= 0xffffffff
    return v - (1 << 32) if v & 0x80000000 else v


def quantize(xyz_q16, p):
    """Axis XYZ (Q16 mm, signed 32-bit) -> voxel address or None when outside the grid."""
    idx = []
    for value, origin, size in zip(xyz_q16, (p['ox'], p['oy'], p['oz']), (GX, GY, GZ)):
        d = s32(value) - s32(origin)               # 33-bit in RTL: no wrap
        if d < 0 or d >= (1 << 24):
            return None
        i = (d * p['inv']) >> 32
        if i >= size:
            return None
        idx.append(i)
    ix, iy, iz = idx
    return (iz << 12) | (iy << 6) | ix


class Grid:
    def __init__(self):
        self.words = np.zeros(VOXEL_WORDS, np.uint32)
        self.occupied = 0

    def set(self, vaddr):
        w, b = vaddr >> 5, vaddr & 31
        if not (int(self.words[w]) >> b) & 1:
            self.occupied += 1
        self.words[w] = np.uint32(int(self.words[w]) | (1 << b))


class Ingest:
    """Staging/commit semantics of the snoop path (FIFO of STAGE_DEPTH voxel addresses).

    The model assumes the drainer has emptied the FIFO before each packet starts,
    which the testbench enforces; capacity is then per packet.
    """

    def __init__(self, p):
        self.p = p
        self.grid = Grid()
        self.inserted = self.dropped = self.out_of_range = 0
        self.frames_committed = self.frames_discarded = 0
        self.staged = []

    def point(self, record_words):
        flags = record_words[7]
        mode_bit = 2 if self.p['ctrl'] & 16 else 1
        if not (self.p['ctrl'] & 2) or not (flags & 0x100) or not (flags & mode_bit):
            return
        v = quantize(record_words[4:7], self.p)
        if v is None:
            self.out_of_range += 1
        elif len(self.staged) >= STAGE_DEPTH:
            self.dropped += 1
        else:
            self.staged.append(v)

    def end_packet(self, ok):
        if ok:
            for v in self.staged:
                self.grid.set(v)
                self.inserted += 1
            self.frames_committed += 1
        elif self.staged:
            self.frames_discarded += 1             # only discards that dropped staged points
        self.staged = []


def coefficients(p, yaw):
    s, c = SIN[yaw & 1023], SIN[(yaw + 256) & 1023]
    a = (p['k'] * -s) >> 14
    b = (p['k'] * c) >> 14
    cc = (p['kse'] * -c) >> 14
    d = (p['kse'] * -s) >> 14
    e = p['kce']
    return a, b, cc, d, e, c >= 0, s >= 0, p['kse'] >= 0


def colour(iz):
    return 1 + ((iz * 5) >> 5)


def render(words, p, yaw):
    """Return the PH x PW uint8 panel exactly as the RTL writes the back buffer."""
    panel = np.zeros((PH, PW), np.uint8)
    a, b, c, d, e, xasc, yasc, zasc = coefficients(p, yaw)
    n = max(1, min(4, p['splat']))
    lo = -((n - 1) >> 1)
    zs = range(GZ) if zasc else range(GZ - 1, -1, -1)
    ys = range(GY) if yasc else range(GY - 1, -1, -1)
    hs = (0, 1) if xasc else (1, 0)
    for iz in zs:
        Z = 2 * iz - p['zc']
        col = colour(iz)
        for iy in ys:
            Y = 2 * iy - 63
            for h in hs:
                word = int(words[(iz << 7) | (iy << 1) | h])
                bits = range(32) if xasc else range(31, -1, -1)
                for bit in bits:
                    if not (word >> bit) & 1:
                        continue
                    X = 2 * (h * 32 + bit) - 63
                    sx = p['sx0'] + ((a * X + b * Y + 2048) >> 12)
                    sy = p['sy0'] - ((c * X + d * Y + e * Z + 2048) >> 12)
                    for dy in range(lo, lo + n):
                        py = sy + dy
                        if not 0 <= py < PH:
                            continue
                        for dx in range(lo, lo + n):
                            px = sx + dx
                            if 0 <= px < PW:
                                panel[py, px] = col
    return panel


def overlay(frame_words, panel, p, enabled):
    """frame_words: (H, W) uint32 BGRX beats. Returns the output beats."""
    out = frame_words.copy()
    if not enabled:
        return out
    h, w = frame_words.shape
    wx, wy = p['wx'], p['wy']
    for y in range(max(0, wy), min(h, wy + WIN_H)):
        for x in range(max(0, wx), min(w, wx + WIN_W)):
            idx = int(panel[(y - wy) >> 1, (x - wx) >> 1])
            if idx:
                out[y, x] = (int(frame_words[y, x]) & 0xff000000) | PALETTE[idx]
    return out


def tables_sv():
    """rtl/point_cloud_tables.sv: sine ROM and palette as plain modules (no include paths)."""
    lines = ['`timescale 1ns/1ps',
             '// Generated by model/point_cloud_model.py (tables_sv); do not edit by hand.',
             '// 1024-entry Q14 sine: v = round(16384*sin(2*pi*i/1024)).',
             'module sine_rom(input wire [9:0] i,output reg signed [15:0] v);',
             ' always @* begin', '  case(i)']
    for i, v in enumerate(SIN):
        lines.append(f"   10'd{i}:v=16'sd{v};" if v >= 0 else f"   10'd{i}:v=-16'sd{-v};")
    lines += ['   default:v=0;', '  endcase', ' end', 'endmodule', '',
              '// Height palette, 24-bit {R,G,B}; index 0 is transparent and never displayed.',
              'module colour_palette(input wire [3:0] i,output reg [23:0] rgb);',
              ' always @* begin', '  case(i)']
    lines += [f"   4'd{i}:rgb=24'h{v:06x};" for i, v in enumerate(PALETTE)]
    lines += ['   default:rgb=0;', '  endcase', ' end', 'endmodule', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    import sys
    from pathlib import Path
    Path(sys.argv[1]).write_text(tables_sv(), encoding='ascii')
