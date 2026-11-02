"""R41 fixed-point live profile: step height, gap, tolerance judgement, profile change and
acquisition quality, one PFO3 frame at a time (PL geometry XYZ + PL row summaries).

The section is expressed in the laser plane itself (s along the laser line, h toward the
camera), so its millimetres depend only on the camera/laser calibration, not on the turntable
axis. Points are PL preview grade until the measurement gate is re-qualified for live frames;
every result carries that grade.

Automatic re-acquisition in fixed-point mode: a frame whose quality is BAD, or whose value is
an outlier against the running window, is not used; the tracker keeps the last good result
and says how many frames it dropped, so the judgement always comes from good frames only.
"""
from collections import deque
from dataclasses import dataclass
import math
import numpy as np

GRADE = 'PL preview grade'
# PFO3 row summary / candidate record columns (decoder order); the live R33 path hands both over
# as plain 2-D uint32 arrays, the fixture decoder as structured arrays.
ROW_FIELDS = ('row raw_peak sat250_count sat255_count raw_candidate_count qualified_count '
              'measurement_count preview_count measurement_max_width preview_max_width measurement_max_peak '
              'preview_max_peak measurement_ambiguous preview_ambiguous reserved0 reserved1').split()


def row_fields(rows):
    a = np.asarray(rows)
    if a.dtype.names:
        return a
    if a.ndim != 2 or a.shape[1] != len(ROW_FIELDS):
        raise ValueError('row summaries must be 16 columns')
    return {name: a[:, j] for j, name in enumerate(ROW_FIELDS)}


def point_rows_from(records, ids):
    """Image row of each geometry record (word 0 = candidate id) from the candidate records."""
    a = np.asarray(records)
    cid, row = (a['candidate_id'], a['row']) if a.dtype.names else (a[:, 0], a[:, 1])
    cid, ids = np.asarray(cid, np.int64), np.asarray(ids, np.int64)
    if not len(cid):
        return None if len(ids) else np.zeros(0, np.int64)
    pos = np.clip(np.searchsorted(cid, ids), 0, len(cid) - 1)
    return np.asarray(row, np.int64)[pos] if np.array_equal(cid[pos], ids) else None


class SectionBasis:
    """Laser-plane section coordinates: s along the laser line (image rows, downward),
    h perpendicular to it inside the plane, increasing toward the camera."""

    def __init__(self, plane):
        p = np.asarray(plane, np.float64).reshape(4)
        k = np.linalg.norm(p[:3])
        if not np.isfinite(k) or k == 0:
            raise ValueError('invalid laser plane')
        self.n, self.d = p[:3] / k, p[3] / k
        y = np.array([0., 1., 0.])
        es = y - (y @ self.n) * self.n
        if np.linalg.norm(es) < .5:
            raise ValueError('laser plane does not contain the image vertical well enough')
        self.es = es / np.linalg.norm(es)
        eh = np.cross(self.n, self.es)
        self.eh = -eh if eh[2] > 0 else eh       # toward the camera = smaller depth
        self.origin = -self.d * self.n

    def project(self, xyz):
        q = np.asarray(xyz, np.float64).reshape(-1, 3) - self.origin
        return np.column_stack((q @ self.es, q @ self.eh))


@dataclass(frozen=True)
class ProfileConfig:
    jump_mm: float = .8             # |dh| between neighbours that starts a new segment
    break_mm: float = 1.0           # minimum along-line hole that starts a new segment / gap
    min_points: int = 6             # smallest usable segment
    level_mm: float = .5            # segments within this of the reference line are "same level"
    window: int = 9                 # frames in the running median
    min_valid: int = 5              # good frames needed before a judgement
    step_nominal_mm: float | None = None
    step_tol_mm: float = .2
    gap_nominal_mm: float | None = None
    gap_tol_mm: float = .2
    change_tol_mm: float = .5
    change_bin_mm: float = .5
    reference_frames: int = 10      # good frames averaged into the change reference (0: off)
    z_min_mm: float | None = None   # measuring window on camera distance Z
    z_max_mm: float | None = None
    auto_depth_mm: float = 250.     # no Z window: keep Z <= nearest (5th pct) + this (0: all)
    rows: tuple | None = None       # measuring window on image rows (first, last)

    def __post_init__(self):
        for name in ('jump_mm', 'break_mm', 'level_mm', 'step_tol_mm', 'gap_tol_mm', 'change_tol_mm',
                     'change_bin_mm'):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
                raise ValueError(name + ' must be a positive number')
        for name in ('step_nominal_mm', 'gap_nominal_mm'):
            v = getattr(self, name)
            if v is not None and not (isinstance(v, (int, float)) and math.isfinite(v)):
                raise ValueError(name + ' must be finite')
        if self.gap_nominal_mm is not None and self.gap_nominal_mm <= 0:
            raise ValueError('gap_nominal_mm must be positive')
        if self.rows is not None and not (len(self.rows) == 2 and 0 <= self.rows[0] <= self.rows[1] < 480):
            raise ValueError('rows window must be (first, last) inside 0..479')
        if self.auto_depth_mm < 0:
            raise ValueError('auto_depth_mm must be >= 0')
        if not (3 <= self.min_points and 1 <= self.min_valid <= self.window and self.reference_frames >= 0):
            raise ValueError('invalid point/frame counts')


@dataclass(frozen=True)
class QualityLimits:
    min_points: int = 30
    min_peak: int = 60              # median laser peak DN
    max_width_px: int = 24          # median candidate width
    warn_sat_pixels: int = 200      # PFO3 measurement gate rejects above 360
    bad_sat_pixels: int = 360
    max_sat_row_fraction: float = .05
    warn_completeness: float = .8
    bad_completeness: float = .5
    max_ambiguous_fraction: float = .05


# code -> (level, Chinese, English); {} placeholders filled from the metrics.
HINTS = {
    'no_stripe': ('BAD', '未检测到激光条纹', 'no laser stripe'),
    'few_points': ('BAD', '有效点过少 {points}', 'too few points {points}'),
    'saturated': ('BAD', '过曝：饱和像素 {sat_pixels}，降低曝光', 'overexposed: {sat_pixels} saturated px'),
    'saturated_warn': ('WARN', '接近过曝：饱和像素 {sat_pixels}', 'near overexposure: {sat_pixels} px'),
    'saturated_rows': ('WARN', '饱和行 {sat_row_pct:.0f}%', 'saturated rows {sat_row_pct:.0f}%'),
    'dark': ('WARN', '条纹偏暗：峰值 {peak}，增加曝光', 'stripe dark: peak {peak}'),
    'wide': ('WARN', '条纹过宽 {width} px：离焦或多次反射', 'stripe wide {width} px'),
    'incomplete': ('WARN', '轮廓不完整：有效行 {completeness_pct:.0f}%', 'profile incomplete {completeness_pct:.0f}%'),
    'broken': ('WARN', '轮廓严重缺失：有效行 {completeness_pct:.0f}%', 'profile broken {completeness_pct:.0f}%'),
    'noisy': ('WARN', '轮廓噪声 ±{noise:.2f} mm 大于公差：物体靠近相机', 'profile noise ±{noise:.2f} mm > tol'),
    'ambiguous': ('WARN', '多峰行 {ambiguous_pct:.0f}%：杂散光或反射', 'ambiguous rows {ambiguous_pct:.0f}%'),
}


def assess_quality(rows, points, limits=QualityLimits(), row_window=None, width_scale=1):
    """Stripe intensity, width, saturation and completeness from the PL row summaries.

    Completeness counts only rows where the detector saw stripe light (a qualified candidate):
    rows without any (occlusion, beyond the object) are geometry, not an acquisition failure.
    """
    rows = row_fields(rows)
    n = len(rows['row'])
    win = np.zeros(n, bool)
    lo, hi = (0, n - 1) if row_window is None else row_window
    win[lo:hi + 1] = True
    valid = (np.asarray(rows['preview_count']) > 0) & win
    seen = (np.asarray(rows['qualified_count']) > 0) & win
    nv = int(valid.sum())
    sat = np.asarray(rows['sat250_count'], np.int64) * win
    m = dict(points=int(points), valid_rows=nv, stripe_rows=int(seen.sum()), sat_pixels=int(sat.sum()),
             row_window=[int(lo), int(hi)])
    codes = []
    if nv == 0:
        codes.append('no_stripe')
    else:
        idx = np.flatnonzero(valid)
        m.update(first_row=int(idx[0]), last_row=int(idx[-1]),
                 completeness=nv / max(nv, int(seen.sum())),
                 peak=int(np.median(np.asarray(rows['preview_max_peak'])[valid])),
                 width=int(np.median(np.asarray(rows['preview_max_width'])[valid])) * width_scale,
                 sat_row_fraction=float((sat[valid] > 0).mean()),
                 ambiguous_fraction=float((np.asarray(rows['preview_ambiguous'])[seen] > 0).mean())
                 if seen.any() else 0.)
        m.update(completeness_pct=100 * m['completeness'], sat_row_pct=100 * m['sat_row_fraction'],
                 ambiguous_pct=100 * m['ambiguous_fraction'])
        if points < limits.min_points:
            codes.append('few_points')
        if m['sat_pixels'] > limits.bad_sat_pixels:
            codes.append('saturated')
        elif m['sat_pixels'] > limits.warn_sat_pixels:
            codes.append('saturated_warn')
        if m['sat_row_fraction'] > limits.max_sat_row_fraction and 'saturated' not in codes:
            codes.append('saturated_rows')
        if m['peak'] < limits.min_peak:
            codes.append('dark')
        if m['width'] > limits.max_width_px:
            codes.append('wide')
        if m['completeness'] < limits.bad_completeness:
            codes.append('broken')
        elif m['completeness'] < limits.warn_completeness:
            codes.append('incomplete')
        if m['ambiguous_fraction'] > limits.max_ambiguous_fraction:
            codes.append('ambiguous')
    levels = [HINTS[c][0] for c in codes]
    level = 'BAD' if 'BAD' in levels else 'WARN' if levels else 'GOOD'
    hints = [dict(code=c, level=HINTS[c][0], zh=HINTS[c][1].format(**m), en=HINTS[c][2].format(**m)) for c in codes]
    return dict(level=level, metrics=m, hints=hints)


def _median3(h):
    if len(h) < 3:
        return h.copy()
    out = h.copy()
    out[1:-1] = np.median(np.column_stack((h[:-2], h[1:-1], h[2:])), axis=1)
    return out


def _fit(s, h):
    a, b = np.polyfit(s, h, 1)
    r = h - (a * s + b)
    rms = float(np.sqrt(np.mean(r * r)))
    keep = np.abs(r) <= 3 * max(rms, .02)
    if 3 <= keep.sum() < len(s):
        a, b = np.polyfit(s[keep], h[keep], 1)
        r = h - (a * s + b)
        rms = float(np.sqrt(np.mean(r[keep] ** 2)))
    return float(a), float(b), rms


def segment_profile(sh, cfg):
    """Sort along the line and split at height jumps or holes; fit a line to each piece."""
    if len(sh) == 0:
        return np.zeros((0, 2)), [], cfg.break_mm, 0.
    sh = sh[np.argsort(sh[:, 0], kind='stable')]
    s, h = sh[:, 0], sh[:, 1]
    ds = np.diff(s)
    spacing = float(np.median(ds)) if len(ds) else 0.
    hole = max(cfg.break_mm, 4 * spacing)
    # Height noise from second differences (a straight sloped surface gives zero), robust to steps.
    noise = float(1.4826 * np.median(np.abs(np.diff(h, 2))) / math.sqrt(6)) if len(h) > 2 else 0.
    jump = max(cfg.jump_mm, 5 * noise)
    cut = np.flatnonzero((ds > hole) | (np.abs(np.diff(_median3(h))) > jump)) + 1
    segs = []
    for idx in np.split(np.arange(len(s)), cut):
        if len(idx) >= cfg.min_points:
            a, b, rms = _fit(s[idx], h[idx])
            segs.append(dict(i0=int(idx[0]), i1=int(idx[-1]) + 1, s0=float(s[idx[0]]), s1=float(s[idx[-1]]),
                             a=a, b=b, rms=rms, n=len(idx)))
    return sh, segs, hole, noise


def _offset(seg_line, s, h):
    a, b = seg_line['a'], seg_line['b']
    return (h - (a * s + b)) / math.sqrt(1 + a * a)


def select_window(xyz, point_rows, cfg):
    """Measuring window: explicit Z / row limits, else the nearest object (auto depth)."""
    xyz = np.asarray(xyz, np.float64).reshape(-1, 3)
    keep = np.ones(len(xyz), bool)
    z = xyz[:, 2]
    if cfg.z_min_mm is not None:
        keep &= z >= cfg.z_min_mm
    if cfg.z_max_mm is not None:
        keep &= z <= cfg.z_max_mm
    if cfg.rows is not None and point_rows is not None:
        r = np.asarray(point_rows)
        keep &= (r >= cfg.rows[0]) & (r <= cfg.rows[1])
    auto = None
    if cfg.z_min_mm is None and cfg.z_max_mm is None and cfg.auto_depth_mm and keep.any():
        auto = float(np.percentile(z[keep], 5)) + cfg.auto_depth_mm
        keep &= z <= auto
    win = dict(z_max_auto_mm=auto, points_in=int(keep.sum()), points_out=int((~keep).sum()))
    if point_rows is not None and keep.any():
        r = np.asarray(point_rows)[keep]
        win['rows'] = (int(r.min()), int(r.max()))
    elif cfg.rows is not None:
        win['rows'] = tuple(cfg.rows)
    return keep, win


def measure_frame(xyz, basis, cfg, point_rows=None, rows=None):
    """Single-frame step height and gap in the laser plane, inside the measuring window.

    Primary step: the two most-populated segments, left -> right along the line, as the
    perpendicular offset of the right one from the left one's fitted line (positive = toward
    the camera). All adjacent-segment steps are listed as well. A gap is reported only where
    the hole between two same-level surfaces is not mostly rows in which the detector saw
    stripe light but accepted no centre (that is an acquisition hole, not a gap)."""
    keep, win = select_window(xyz, point_rows, cfg)
    sh = basis.project(np.asarray(xyz, np.float64).reshape(-1, 3)[keep])
    order = np.argsort(sh[:, 0], kind='stable')
    sh = sh[order]
    prow = None if point_rows is None else np.asarray(point_rows)[keep][order]
    sh, segs, hole, noise = segment_profile(sh, cfg)
    level_mm = max(cfg.level_mm, 3 * noise)
    out = dict(points=int(len(sh)), segments=len(segs), spacing_mm=None, step_mm=None, gap_mm=None, window=win,
               gap_depth_mm=None, step_angle_deg=None, reference=None, step_segment=None, gap_edges=None,
               profile=sh, segment_list=segs, grade=GRADE, noise_mm=noise, level_mm=level_mm, steps=[],
               gap_unresolved=False)
    if len(sh) > 1:
        out['spacing_mm'] = float(np.median(np.diff(sh[:, 0])))
    if not segs:
        return out
    s, h = sh[:, 0], sh[:, 1]
    for g0, g1 in zip(segs, segs[1:]):
        off = float(np.median(_offset(g0, s[g1['i0']:g1['i1']], h[g1['i0']:g1['i1']])))
        if abs(off) > level_mm:
            out['steps'].append(dict(at_s_mm=.5 * (g0['s1'] + g1['s0']), step_mm=off))
    ref = max(segs, key=lambda g: (g['n'], g['s1'] - g['s0']))
    out['reference'] = segs.index(ref)
    for g in segs:
        g['offset_mm'] = float(np.median(_offset(ref, s[g['i0']:g['i1']], h[g['i0']:g['i1']])))
    others = [g for g in segs if g is not ref]
    if others:
        second = max(others, key=lambda g: (g['n'], g['s1'] - g['s0']))
        left, right = sorted((ref, second), key=lambda g: g['s0'])
        off = float(np.median(_offset(left, s[right['i0']:right['i1']], h[right['i0']:right['i1']])))
        if abs(off) > level_mm:
            out['step_mm'] = off
            out['reference'], out['step_segment'] = segs.index(left), segs.index(right)
            out['step_angle_deg'] = math.degrees(abs(math.atan(right['a']) - math.atan(left['a'])))
    level = [g for g in segs if abs(g['offset_mm']) <= level_mm]
    if len(level) >= 2:
        idx = np.concatenate([np.arange(g['i0'], g['i1']) for g in level])
        idx.sort()
        ds = np.diff(s[idx])
        j = int(np.argmax(ds))
        if ds[j] > hole and prow is not None and rows is not None:
            rows = row_fields(rows)
            r0, r1 = sorted((int(prow[idx[j]]), int(prow[idx[j + 1]])))
            between = slice(r0 + 1, r1)
            seen = np.asarray(rows['qualified_count'])[between] > 0
            rejected = seen & (np.asarray(rows['preview_count'])[between] == 0)
            out['gap_unresolved'] = bool(len(rejected) and rejected.mean() > .5)
        if ds[j] > hole and not out['gap_unresolved']:
            left, right = idx[j], idx[j + 1]
            out['gap_mm'] = float(math.hypot(s[right] - s[left], h[right] - h[left]))
            out['gap_edges'] = (float(s[left]), float(s[right]))
            inside = (s > s[left]) & (s < s[right])
            if inside.any():
                out['gap_depth_mm'] = float(-_offset(ref, s[inside], h[inside]).min())
    return out


def _judge(values, nominal, tol, min_valid):
    if len(values) < min_valid:
        return dict(status='WAIT', value=None, spread=None, n=len(values))
    v = np.asarray(values)
    value, spread = float(np.median(v)), float(np.percentile(v, 90) - np.percentile(v, 10))
    if spread > tol:
        status = 'UNSTABLE'
    elif nominal is None:
        status = 'MEASURED'
    else:
        status = 'PASS' if abs(value - nominal) <= tol + 1e-9 else 'FAIL'
    return dict(status=status, value=value, spread=spread, n=len(v),
                deviation=None if nominal is None else value - nominal, nominal=nominal, tol=tol)


class ProfileTracker:
    """Running judgement over good frames, outlier rejection and profile-change detection."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.values = dict(step=deque(maxlen=cfg.window), gap=deque(maxlen=cfg.window))
        self.frames = self.used = self.rejected_quality = 0
        self.outliers = dict(step=0, gap=0)
        self.ref_stack, self.reference = [], None
        self.last = None

    def reset_reference(self):
        self.ref_stack, self.reference = [], None

    def _binned(self, sh, b):
        k = np.floor(sh[:, 0] / b).astype(np.int64)
        if not len(k):
            return {}
        out = {}
        order = np.argsort(k, kind='stable')
        k, hs = k[order], sh[order, 1]
        starts = np.flatnonzero(np.r_[True, np.diff(k) != 0])
        for a, z in zip(starts, np.r_[starts[1:], len(k)]):
            out[int(k[a])] = float(np.median(hs[a:z]))
        return out

    def _change(self, frame):
        """Reference = per-bin median of the first good frames (bin >= 2x point spacing); its
        frame-to-frame scatter sets the threshold, so the acquisition noise never reads as change.
        CHANGED needs >= 3 consecutive compared bins beyond the threshold."""
        c = self.cfg
        if self.reference is None:
            if not c.reference_frames:
                return dict(status='OFF')
            self.ref_stack.append((frame['profile'], frame['spacing_mm'] or 0.))
            if len(self.ref_stack) >= c.reference_frames:
                b = max(c.change_bin_mm, 2 * float(np.median([sp for _, sp in self.ref_stack])))
                binned = [self._binned(p, b) for p, _ in self.ref_stack]
                bins, scatter = {}, []
                for key in set().union(*binned):
                    v = np.array([f[key] for f in binned if key in f])
                    if len(v) * 2 >= len(binned):
                        bins[key] = float(np.median(v))
                        if len(v) >= 3:
                            scatter.append(1.4826 * float(np.median(np.abs(v - bins[key]))))
                noise = float(np.median(scatter)) if scatter else 0.
                self.reference = dict(bins=bins, bin_mm=b, noise_mm=noise, threshold_mm=max(c.change_tol_mm, 3 * noise))
                self.ref_stack = []
            return dict(status='LEARNING', frames=len(self.ref_stack), needed=c.reference_frames)
        ref = self.reference
        cur = self._binned(frame['profile'], ref['bin_mm'])
        keys = sorted(k for k in cur if k in ref['bins'])
        if not keys:
            return dict(status='NO_OVERLAP', threshold_mm=ref['threshold_mm'])
        dev = np.array([cur[k] - ref['bins'][k] for k in keys])
        over = np.abs(dev) > ref['threshold_mm']
        runs, start = [], None
        for j in range(len(keys) + 1):
            linked = j < len(keys) and over[j] and start is not None and keys[j] - keys[j - 1] <= 2
            if start is not None and not linked:
                runs.append((start, j))
                start = None
            if j < len(keys) and over[j] and start is None:
                start = j
        runs = [r for r in runs if r[1] - r[0] >= 3]
        out = dict(status='CHANGED' if runs else 'SAME', threshold_mm=ref['threshold_mm'],
                   typical_dev_mm=float(np.median(np.abs(dev))), changed_bins=int(over.sum()),
                   compared_bins=len(keys), coverage=len(keys) / max(1, len(ref['bins'])))
        if runs:
            a, z = max(runs, key=lambda r: abs(float(np.median(dev[r[0]:r[1]]))))
            out.update(max_dev_mm=float(np.median(dev[a:z])),
                       at_s_mm=(.5 * (keys[a] + keys[z - 1]) + .5) * ref['bin_mm'],
                       length_mm=(keys[z - 1] - keys[a] + 1) * ref['bin_mm'])
        return out

    def update(self, frame, quality):
        self.frames += 1
        tol = min(t for t, nom in ((self.cfg.step_tol_mm, self.cfg.step_nominal_mm),
                                   (self.cfg.gap_tol_mm, self.cfg.gap_nominal_mm), (1e9, 0)) if nom is not None)
        if frame.get('noise_mm') and 2 * frame['noise_mm'] > tol and quality['level'] != 'BAD':
            level, zh, en = HINTS['noisy']
            quality = dict(quality, level='WARN', hints=quality['hints'] + [dict(
                code='noisy', level=level, zh=zh.format(noise=frame['noise_mm']), en=en.format(noise=frame['noise_mm']))])
        out = dict(frame=frame, quality=quality, dropped=False)
        if quality['level'] == 'BAD':
            self.rejected_quality += 1
            out['dropped'] = 'quality'
        else:
            self.used += 1
            for key in ('step', 'gap'):
                v = frame[key + '_mm']
                if v is None:
                    continue
                q = self.values[key]
                if len(q) >= self.cfg.min_valid:
                    med = float(np.median(q))
                    mad = 1.4826 * float(np.median(np.abs(np.asarray(q) - med)))
                    tol = self.cfg.step_tol_mm if key == 'step' else self.cfg.gap_tol_mm
                    if abs(v - med) > max(4 * mad, 2 * tol, .3):
                        self.outliers[key] += 1
                        out.setdefault('outlier', []).append(key)
                        continue
                q.append(v)
            out['change'] = self._change(frame)
        c = self.cfg
        out['step'] = _judge(self.values['step'], c.step_nominal_mm, c.step_tol_mm, c.min_valid)
        out['gap'] = _judge(self.values['gap'], c.gap_nominal_mm, c.gap_tol_mm, c.min_valid)
        if 'change' not in out:
            out['change'] = (self.last or {}).get('change', dict(status='WAIT'))
        out['counts'] = dict(frames=self.frames, used=self.used, rejected_quality=self.rejected_quality,
                             outliers=dict(self.outliers))
        self.last = out
        return out


def log_view(summary):
    """JSON-friendly per-frame record (no point arrays)."""
    f = summary['frame']
    return dict(points=f['points'], segments=f['segments'], step_mm=f['step_mm'], gap_mm=f['gap_mm'],
                window=f['window'],
                gap_depth_mm=f['gap_depth_mm'], step_angle_deg=f['step_angle_deg'], spacing_mm=f['spacing_mm'],
                noise_mm=f['noise_mm'], steps=f['steps'], gap_unresolved=f['gap_unresolved'],
                quality=summary['quality']['level'], hints=[h['code'] for h in summary['quality']['hints']],
                dropped=summary['dropped'], outlier=summary.get('outlier'),
                step=summary['step'], gap=summary['gap'], change=summary['change'], counts=summary['counts'])
