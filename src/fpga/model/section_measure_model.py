"""Fixed-point model of rtl/section_measure.sv; integer arithmetic, bit-exact with the RTL.

Formats: coordinates and lengths Q16 mm, unit vectors and slopes Q24, squares Q32.
Shifts are arithmetic (floor). Divisions truncate toward zero and saturate.

Per committed packet: the window points are stored (at most MAX_POINTS), stably sorted by s,
then noise and spacing come from exact medians, the profile is split, each segment gets a
least-squares line refitted once without 3-sigma outliers, and segment offsets are medians of
the per-point residuals, as in profile_live.
"""
from dataclasses import dataclass, field

MAX_POINTS = 1024
MAX_SEGMENTS = 16
MAX_STEPS = 15
NOISE_K = 39667                 # 1.4826 / sqrt(6) in Q16: median |d2| -> noise sigma
RMS_FLOOR = 1311                # 0.02 mm
I32_MAX = (1 << 31) - 1
U64_MAX = (1 << 64) - 1


def sat(v, bits=32):
    lim = 1 << (bits - 1)
    return max(-lim, min(lim - 1, v))


def udiv(num, den):
    """Unsigned quotient, saturated to 64 bits (the RTL divider)."""
    return U64_MAX if (num >> 64) >= den else num // den


def sdiv(num, den):
    """Signed / positive, truncated toward zero, saturated to 32 bits."""
    q = min(udiv(abs(num), den), I32_MAX)
    return -q if num < 0 else q


def isqrt(v):
    """Floor square root of a 64-bit value (the RTL restoring square root)."""
    r = 0
    for bit in range(31, -1, -1):
        t = r | (1 << bit)
        if t * t <= v:
            r = t
    return r


def median(values):
    """Exact median; an even count averages the two middle values (floor), like numpy.

    The RTL finds each order statistic by a 32-step bisection over the stored points.
    """
    if not values:
        return 0
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) >> 1


@dataclass
class Config:
    origin: tuple                   # Q16 mm
    es: tuple                       # Q24
    eh: tuple                       # Q24
    z_min: int = -(1 << 31)
    z_max: int = I32_MAX
    jump: int = 52429               # 0.8 mm
    brk: int = 65536                # 1.0 mm
    level: int = 32768              # 0.5 mm
    min_points: int = 6
    preview: bool = True


def config_from_basis(basis, **kw):
    """Config from a profile_live.SectionBasis (float) by rounding to Q16 / Q24."""
    def q24(v):
        return tuple(min((1 << 24) - 1, max(-(1 << 24), int(round(c * (1 << 24))))) for c in v)
    origin = tuple(int(round(c * 65536)) for c in basis.origin)
    return Config(origin=origin, es=q24(basis.es), eh=q24(basis.eh), **kw)


@dataclass
class Segment:
    i0: int
    i1: int
    s0: int
    h0: int
    s1: int
    h1: int
    fit: dict = field(default_factory=dict)

    @property
    def n(self):
        return self.i1 - self.i0


def moments(seg, s, h, keep=None):
    """Sums relative to the segment's first point; ds, dh limited to 28 bits."""
    acc = [0] * 6
    for i in range(seg.i0, seg.i1):
        if keep is not None and not keep[i - seg.i0]:
            continue
        ds, dh = sat(s[i] - seg.s0, 28), sat(h[i] - seg.h0, 28)
        for k, v in enumerate((1, ds, dh, ds * ds, ds * dh, dh * dh)):
            acc[k] += v
    return acc


def line(acc):
    """Least squares h = h0 + b + a (s - s0) and the rms of its residuals (Q24, Q16, Q16)."""
    n, sx, sy, sxx, sxy, syy = acc
    d = n * sxx - sx * sx
    num = n * sxy - sx * sy
    a = 0
    if d > 0:
        a = min(udiv(abs(num) << 24, d), I32_MAX)
        a = -a if num < 0 else a
    b = sdiv(sy - ((a * sx) >> 24), n)
    ss = max(0, syy - ((a * sxy) >> 24) - b * sy)
    return a, b, isqrt(min(udiv(ss, n), 1 << 62))


def fit(g, s, h):
    """Line, refitted once without points beyond 3 rms (profile_live._fit), and 1/sqrt(1+a^2)."""
    acc = moments(g, s, h)
    a, b, rms = line(acc)
    lim = 3 * max(rms, RMS_FLOOR)
    c = sat(g.h0 + b)
    keep = [abs(residual(a, c, g.s0, s[i], h[i])) <= lim for i in range(g.i0, g.i1)]
    if 3 <= sum(keep) < g.n:
        acc = moments(g, s, h, keep)
        a, b, rms = line(acc)
    root = isqrt((1 << 48) + a * a)
    g.fit = dict(a=a, c=sat(g.h0 + b), rms=rms, inv=udiv(1 << 48, root))


def residual(a, c, s0, s, h):
    """Vertical residual of (s, h) from the line h = c + a (s - s0); the RTL residual unit."""
    return sat(h - c - ((a * (s - s0)) >> 24))


def offset(line_seg, s, h):
    """Perpendicular offset (Q16) of point (s, h) from the fitted line of `line_seg`."""
    f = line_seg.fit
    return sat((residual(f['a'], f['c'], line_seg.s0, s, h) * f['inv']) >> 24)


def seg_offset(g, line_seg, s, h):
    """Median vertical residual of segment g against the line of `line_seg`, made perpendicular."""
    f = line_seg.fit
    r = median([residual(f['a'], f['c'], line_seg.s0, s[i], h[i]) for i in range(g.i0, g.i1)])
    return sat((r * f['inv']) >> 24)


class SectionMeasure:
    """One instance per run; `packet` returns the results of one committed PFO3 packet."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.packets = 0

    def select(self, records):
        c = self.cfg
        pts, dropped = [], 0
        for r in records:
            flags = int(r[7])
            grade = flags & 2 if c.preview else flags & 1
            if not (flags & 0x100 and grade):
                continue
            x, y, z = (int(v) - (1 << 32) if int(v) >> 31 else int(v) for v in r[1:4])
            if not c.z_min <= z <= c.z_max:
                continue
            if len(pts) < MAX_POINTS:
                pts.append(project(c, x, y, z))
            else:
                dropped += 1
        return pts, dropped

    def packet(self, records):
        c = self.cfg
        pts, dropped = self.select(records)
        pts = sorted(pts, key=lambda p: p[0])            # stable, as the RTL insertion sort
        n = len(pts)
        s = [p[0] for p in pts]
        h = [p[1] for p in pts]
        spacing = median([sat(s[i] - s[i - 1]) for i in range(1, n)])
        noise = median([min(abs(h[i + 1] - 2 * h[i] + h[i - 1]), I32_MAX) for i in range(1, n - 1)])
        noise = (noise * NOISE_K) >> 16
        hole = max(c.brk, sat(4 * spacing))
        jump = max(c.jump, sat(5 * noise))
        level = max(c.level, sat(3 * noise))
        m = list(h)
        for i in range(1, n - 1):
            m[i] = sorted(h[i - 1:i + 2])[1]

        segs, overflow, i0 = [], False, 0
        for i in range(1, n + 1):
            if i == n or s[i] - s[i - 1] > hole or abs(m[i] - m[i - 1]) > jump:
                if i - i0 >= c.min_points:
                    if len(segs) < MAX_SEGMENTS:
                        segs.append(Segment(i0, i, s[i0], h[i0], s[i - 1], h[i - 1]))
                    else:
                        overflow = True
                i0 = i
        for g in segs:
            fit(g, s, h)

        out = dict(points=n, dropped=dropped, segments=len(segs), overflow=overflow, noise=noise, spacing=spacing,
                   level=level, step=None, step_segment=None, reference=None, gap=None, gap_edges=None, depth=None,
                   steps=[], ref_rms=None, ref_slope=None, seg=segs, s=s, h=h)
        for g0, g1 in zip(segs, segs[1:]):
            off = seg_offset(g1, g0, s, h)
            if abs(off) > level and len(out['steps']) < MAX_STEPS:
                out['steps'].append(((g0.s1 + g1.s0) >> 1, off))
        if not segs:
            self.packets += 1
            return out

        def key(i):
            return segs[i].n, segs[i].s1 - segs[i].s0
        ref = 0
        for i in range(1, len(segs)):
            if key(i) > key(ref):
                ref = i
        offsets = [seg_offset(g, segs[ref], s, h) for g in segs]
        out.update(reference=ref, offsets=offsets)
        if len(segs) > 1:
            sec = None
            for i in range(len(segs)):
                if i != ref and (sec is None or key(i) > key(sec)):
                    sec = i
            left, right = (sec, ref) if segs[sec].s0 < segs[ref].s0 else (ref, sec)
            off = seg_offset(segs[right], segs[left], s, h)
            if abs(off) > level:
                out.update(step=off, reference=left, step_segment=right)
        out['ref_rms'] = segs[out['reference']].fit['rms']
        out['ref_slope'] = segs[out['reference']].fit['a']

        # largest hole between consecutive same-level segments
        lv = [i for i in range(len(segs)) if abs(offsets[i]) <= level]
        best = None
        for a, b in zip(lv, lv[1:]):
            d = segs[b].s0 - segs[a].s1
            if d > hole and (best is None or d > best[0]):
                best = (d, a, b)
        if best is not None:
            _, a, b = best
            dsg, dhg = sat(segs[b].s0 - segs[a].s1), sat(segs[b].h0 - segs[a].h1)
            out['gap'] = isqrt(dsg * dsg + dhg * dhg)
            out['gap_edges'] = (segs[a].s1, segs[b].s0)
            if segs[a].i1 < segs[b].i0:       # offset is monotonic in the residual: one minimum
                f = segs[ref].fit
                r = min(residual(f['a'], f['c'], segs[ref].s0, s[i], h[i]) for i in range(segs[a].i1, segs[b].i0))
                out['depth'] = sat(-sat((r * f['inv']) >> 24))
        self.packets += 1
        return out


def project(cfg, x, y, z):
    q = (x - cfg.origin[0], y - cfg.origin[1], z - cfg.origin[2])
    s = sum(a * b for a, b in zip(q, cfg.es)) >> 24
    h = sum(a * b for a, b in zip(q, cfg.eh)) >> 24
    return sat(s), sat(h)
