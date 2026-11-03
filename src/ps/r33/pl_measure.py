"""R41 PL live measurement control: pfo3_frontend registers 0x500-0x5ff only.

The PL (rtl/profile_statistics.sv) accumulates per-packet statistics over the PL geometry records of
every committed PFO3 packet; this module configures it and converts the last packet's results to
millimetres. Camera-frame values (distance Z, X/Y extent) need only the camera/laser calibration;
axis-frame values (height range, per-band radius) are meaningful only with a calibrated turntable
axis and are reported with that caveat by the caller.
"""
import math

BASE = 0x500
WINDOW = 0x100
MAGIC = 0x5234314D
Q16 = 65536.0


def _s32(v):
    v &= 0xffffffff
    return v - (1 << 32) if v & 0x80000000 else v


class PLMeasure:
    def __init__(self, mmio_factory, phys_addr):
        self._mmio = mmio_factory(int(phys_addr), 4096)
        if self._read(0x00) != MAGIC:
            raise RuntimeError('R41 PL measurement block absent (magic mismatch)')
        self.cfg = None

    def _write(self, offset, value):
        if type(offset) is not int or not 0 <= offset < WINDOW or offset & 3:
            raise ValueError('PL measure offset outside its 0x500-0x5ff window')
        self._mmio.write(BASE + offset, int(value) & 0xffffffff)

    def _read(self, offset):
        if type(offset) is not int or not 0 <= offset < WINDOW or offset & 3:
            raise ValueError('PL measure offset outside its 0x500-0x5ff window')
        return int(self._mmio.read(BASE + offset)) & 0xffffffff

    def configure(self, *, preview=True, z_origin_mm=0., z_shift=18, h_origin_mm=-64., h_shift=17):
        """Z histogram: 256 bins of 2^z_shift Q16 units from z_origin; 32 height bands likewise."""
        for name, v in (('z_shift', z_shift), ('h_shift', h_shift)):
            if type(v) is not int or not 10 <= v <= 24:
                raise ValueError(name + ' must be 10..24 (Q16 units per bin = 2^shift)')
        self._write(0x04, 0)
        self._write(0x08, int(round(z_origin_mm * Q16)))
        self._write(0x0C, z_shift)
        self._write(0x10, int(round(h_origin_mm * Q16)))
        self._write(0x14, h_shift)
        self._write(0x04, 1 | (2 if preview else 0))
        self.cfg = dict(preview=preview, z_origin_mm=z_origin_mm, z_bin_mm=(1 << z_shift) / Q16,
                        h_origin_mm=h_origin_mm, h_bin_mm=(1 << h_shift) / Q16)
        return self.cfg

    def disable(self):
        self._write(0x04, 0)

    def frame(self, *, bands=False):
        """Results of the last committed packet, in millimetres."""
        r = {o: self._read(o) for o in range(0x20, 0x64, 4)}
        n = r[0x24]
        out = dict(packets=r[0x20], points=n, dropped_busy=r[0x60])
        if n:
            zsum = (_s32(r[0x34]) << 32) | r[0x30]
            out.update(z_min_mm=_s32(r[0x28]) / Q16, z_max_mm=_s32(r[0x2C]) / Q16, z_mean_mm=zsum / n / Q16,
                       x_min_mm=_s32(r[0x48]) / Q16, x_max_mm=_s32(r[0x4C]) / Q16,
                       y_min_mm=_s32(r[0x50]) / Q16, y_max_mm=_s32(r[0x54]) / Q16,
                       axis_z_min_mm=_s32(r[0x58]) / Q16, axis_z_max_mm=_s32(r[0x5C]) / Q16,
                       z_in_range=r[0x3C], z_below=r[0x40], z_above=r[0x44])
            if r[0x38] >> 31:
                c = self.cfg
                out['z_median_mm'] = c['z_origin_mm'] + ((r[0x38] & 0xff) + .5) * c['z_bin_mm']
                out['z_median_bin_mm'] = c['z_bin_mm']
        if bands:
            rows = []
            for b in range(32):
                self._write(0x18, b)
                cnt = self._read(0x64)
                if not cnt:
                    continue
                mx = self._read(0x68) | self._read(0x6C) << 32
                mn = self._read(0x70) | self._read(0x74) << 32
                sm = self._read(0x78) | self._read(0x7C) << 32
                c = self.cfg
                rows.append(dict(band=b, height_mm=c['h_origin_mm'] + (b + .5) * c['h_bin_mm'], points=cnt,
                                 r_max_mm=math.sqrt(mx) / Q16, r_min_mm=math.sqrt(mn) / Q16,
                                 r_rms_mm=math.sqrt(sm / cnt) / Q16))
            out['bands'] = rows
        return out
