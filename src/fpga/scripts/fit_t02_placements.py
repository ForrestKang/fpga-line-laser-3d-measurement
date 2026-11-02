"""Joint calibration of the laser plane and the turntable axis from T02 scans in several placements.

T02: three coaxial cylinders 40 / 30 / 20 mm, 16 mm each, standing either large end down ('down')
or small end down ('up'). One placement cannot pin errors that grow with height (the earlier
T02-only fit read 44.8 mm for the 40 mm section once T02 was turned over); two placements put
each radius at two heights.

Every scan is undone to camera points with the axis and plane it was scanned with, and scaled
into the installed plane (all planes share the normal). Shared unknowns: scale s and laser-plane
normal tilt (2) - each camera point is moved along its ray onto the plane n'.X = s.D0 - plus the
axis direction (2) and point (2). Per scan: T02 centre offset (2) and base height (1).
Sections are labelled by radius clustering, so the placement is read from the data; points within
1.2 mm of a step and 1 mm of the ends are dropped. Even views fit, odd views check.
"""
import argparse
import json
from pathlib import Path

import numpy as np

STEPS = 3547.0
SECTION = 16.0
CLEAR_STEP, CLEAR_END = 1.2, 1.0


def basis(d):
    d = d / np.linalg.norm(d)
    e1 = np.cross(d, [0.0, 0.0, 1.0])
    e1 /= np.linalg.norm(e1)
    return d, e1, np.cross(d, e1)


def rotate(v, d, theta):
    ct, st = np.cos(theta)[:, None], np.sin(theta)[:, None]
    return v * ct + np.cross(d, v) * st + np.outer(v @ d, d) * (1 - ct)


def camera_points(cloud, views, axis, plane_d, base_d):
    """Camera points of a scan, moved into the base plane (same normal, offset base_d)."""
    p0, d0 = np.array(axis['axis_point_camera_mm'], float), np.array(axis['axis_direction_camera'], float)
    d0 /= np.linalg.norm(d0)
    theta = np.repeat(views[:, 0] * 2 * np.pi / STEPS, views[:, 1])
    P = rotate(cloud[:, :3] - p0, d0, theta) + p0
    return P * (base_d / plane_d), theta, np.repeat(np.arange(len(views)), views[:, 1])


class Model:
    """Shared x[:7] = s, axis dir a, b, axis point c, e, plane tilt g1, g2 (about the base frame)."""

    def __init__(self, p0, d0, n0, D0):
        self.p0, self.d0, self.n0, self.D0 = p0, d0, n0, D0
        _, self.e1, self.e2 = basis(d0)
        _, self.f1, self.f2 = basis(n0)

    def frame(self, x):
        d = self.d0 + x[1] * self.e1 + x[2] * self.e2
        d /= np.linalg.norm(d)
        n = self.n0 + x[5] * self.f1 + x[6] * self.f2
        n /= np.linalg.norm(n)
        return self.p0 + x[3] * self.e1 + x[4] * self.e2, d, n

    def corrected(self, P, x):
        _, _, n = self.frame(x)
        # move each point along its camera ray from plane n0 onto n' (scaled by s); relative form,
        # exact identity at x0 although the stored points sit on the plane only to ~0.1 mm
        return P * (x[0] * (P @ self.n0) / (P @ n))[:, None]

    def object(self, P, theta, x):
        p, d, _ = self.frame(x)
        q = rotate(self.corrected(P, x) - p, d, -theta)
        _, f1, f2 = basis(d)
        return q @ f1, q @ f2, -(q @ d)                      # camera +y is down: up = -d


def circle(u, w):
    c = np.linalg.lstsq(np.column_stack((u, w, np.ones_like(u))), u ** 2 + w ** 2, rcond=None)[0]
    cx, cy = c[0] / 2, c[1] / 2
    R = np.sqrt(c[2] + cx ** 2 + cy ** 2)
    return cx, cy, R, np.hypot(u - cx, w - cy) - R


class Scan:
    def __init__(self, name, P, theta, view, flag, model, orientation=None):
        self.name = name
        u, w, up = model.object(P, theta, np.zeros(7) + np.r_[1.0, np.zeros(6)])
        keep = (flag > 0.5) & (np.hypot(u, w) < 35)
        top = np.percentile(up[keep], 99.8)
        keep &= up > top - 52                               # T02 is 48 mm; the turntable hub lies below
        band = keep & (up < top - 2) & (up > top - 12)
        cx, cy, _, _ = circle(u[band], w[band])
        r = np.hypot(u - cx, w - cy)
        if orientation is None:
            # radius clusters (1-D k-means, 3 levels) -> small/middle/large. Only valid near the
            # true axis: about an axis far off the measured radii need not even keep their order.
            centres = np.percentile(r[keep], [10, 50, 90])
            for _ in range(30):
                lab = np.argmin(np.abs(r[:, None] - centres[None, :]), axis=1)
                centres = np.array([np.median(r[keep & (lab == k)]) for k in range(3)])
            order = np.argsort(centres)
            size = np.empty(3, int)
            size[order] = np.arange(3)                      # 0 small (R10), 1 middle, 2 large (R20)
            sec = size[lab]
            keep &= np.abs(r - centres[lab]) < 2.5
            mid = [np.median(up[keep & (sec == k)]) for k in range(3)]
            orientation = 'down' if mid[2] < mid[0] else 'up'
            stack = [2, 1, 0] if orientation == 'down' else [0, 1, 2]
        else:
            # placement known: the two steps are the two largest radius jumps along the height,
            # whatever the radii read about the current axis
            stack = [2, 1, 0] if orientation == 'down' else [0, 1, 2]     # sections bottom to top
            zs = np.arange(top - 52, top, 0.5)
            med = np.array([np.median(r[keep & (up >= z) & (up < z + 0.5)]) if (keep & (up >= z) & (up < z + 0.5)).sum() > 15
                            else np.nan for z in zs])
            jump = np.abs(med[2:] - med[:-2])
            jump[~np.isfinite(jump)] = 0
            first = int(np.argmax(jump))
            jump[max(0, first - 16):first + 17] = 0         # the other step is >= 8 mm away
            second = int(np.argmax(jump))
            steps = sorted(zs[[first + 1, second + 1]])
            band = np.searchsorted(steps, up)               # 0 bottom, 1 middle, 2 top
            sec = np.array(stack)[band]
            for k in range(3):
                m = keep & (sec == k)
                keep &= ~((sec == k) & (np.abs(r - np.median(r[m])) > 2.5))
        self.orientation = orientation
        bottom = np.percentile(up[keep], 0.2)
        bounds = [bottom]
        for lo, hi in zip(stack, stack[1:]):
            bounds.append(0.5 * (np.percentile(up[keep & (sec == lo)], 98) + np.percentile(up[keep & (sec == hi)], 2)))
        bounds.append(top)
        clear = np.ones(len(up), bool)
        for b in bounds[1:-1]:
            clear &= np.abs(up - b) > CLEAR_STEP
        keep &= clear & (up > bottom + CLEAR_END) & (up < top - CLEAR_END)
        self.P, self.theta, self.view, self.sec, self.keep, self.flag = P, theta, view, sec, keep, flag > 0.5
        self.stack, self.x0 = stack, np.array([cx, cy, bottom])
        self.radius = np.array([10.0, 15.0, 20.0])

    def expected_band(self, k):
        """Kept height band of size-k section relative to the base, and its 2/98 % targets."""
        i = self.stack.index(k)
        lo = SECTION * i + (CLEAR_END if i == 0 else CLEAR_STEP)
        hi = SECTION * (i + 1) - (CLEAR_END if i == 2 else CLEAR_STEP)
        return lo + 0.02 * (hi - lo), hi - 0.02 * (hi - lo)

    def residuals(self, model, x, own, idx, w_height):
        u, w, up = model.object(self.P[idx], self.theta[idx], x)
        sec = self.sec[idx]
        # the print's own taper: radius slope x[7 + k] per mm of height from T02's large end,
        # about the section centre; shared by all placements (it turns over with T02)
        h = up - own[2]
        z_obj = h if self.orientation == 'down' else 3 * SECTION - h
        centre = np.array([5 * SECTION / 2, 3 * SECTION / 2, SECTION / 2])[sec]
        radius = self.radius[sec] + np.asarray(x[7:10])[sec] * (z_obj - centre)
        res = [np.hypot(u - own[0], w - own[1]) - radius]
        for k in range(3):
            lo, hi = np.percentile(up[sec == k], [2, 98])
            elo, ehi = self.expected_band(k)
            res.append(w_height * np.array([lo - own[2] - elo, hi - own[2] - ehi]))
        return np.concatenate(res)

    def evaluate(self, model, x, subset):
        u, w, up = model.object(self.P, self.theta, x)
        m = self.keep & subset
        out = dict(orientation=self.orientation, sections=[])
        for k in self.stack:                                # bottom to top
            s = m & (self.sec == k)
            cx, cy, R, res = circle(u[s], w[s])
            z = up[s]
            lo_s, hi_s = z < np.percentile(z, 15), z > np.percentile(z, 85)
            d_lo = 2 * circle(u[s][lo_s], w[s][lo_s])[2]
            d_hi = 2 * circle(u[s][hi_s], w[s][hi_s])[2]
            out['sections'].append(dict(nominal_d=2 * self.radius[k], d=round(2 * R, 3), error=round(2 * R - 2 * self.radius[k], 3),
                                        taper_bottom_to_top=round(d_hi - d_lo, 3), roundness_rms=round(float(res.std()), 3),
                                        points=int(s.sum())))
        # step heights from the radius jumps on all measurement points of this subset
        top = np.percentile(up[self.keep], 99.9)
        allm = subset & self.flag & (np.hypot(u, w) < 35) & (up > top - 52)
        cx, cy = np.mean([circle(u[m & (self.sec == k)], w[m & (self.sec == k)])[:2] for k in range(3)], axis=0)
        r = np.hypot(u - cx, w - cy)
        zs = np.arange(np.percentile(up[allm], 0.2), np.percentile(up[allm], 99.8), 0.25)
        med = np.array([np.median(r[allm & (up >= z) & (up < z + 0.25)]) if (allm & (up >= z) & (up < z + 0.25)).sum() > 15
                        else np.nan for z in zs])
        steps = []
        for c in (12.5, 17.5):                              # radius between 10/15 and 15/20
            above = med > c
            idx = np.flatnonzero(np.diff(above.astype(int)) != 0)
            steps.append(float(zs[idx[0] + 1]) if len(idx) else None)
        b0, b3 = float(np.percentile(up[allm], 0.2)), float(np.percentile(up[allm], 99.8))
        edges = [b0] + sorted(s for s in steps if s is not None) + [b3]
        out['section_heights'] = [round(b - a, 2) for a, b in zip(edges, edges[1:])]
        out['total_height'] = round(b3 - b0, 2)
        return out


def solve(scans, model, x, owns, idxs, w_height, fixed=()):
    def f(xx, oo):
        return np.concatenate([s.residuals(model, xx, oo[i], idxs[i], w_height) for i, s in enumerate(scans)])
    flat = np.concatenate([x] + owns)
    n = len(x)

    def F(v):
        return f(v[:n], [v[n + 3 * i:n + 3 * i + 3] for i in range(len(scans))])
    lam, cost = 1e-2, np.sum(F(flat) ** 2)
    for _ in range(120):
        r0 = F(flat)
        J = np.empty((len(r0), len(flat)))
        for j in range(len(flat)):
            dv = np.zeros_like(flat)
            dv[j] = 1e-6 if j == 0 else 1e-4
            J[:, j] = (F(flat + dv) - r0) / dv[j]
        J[:, list(fixed)] = 0.0
        A, g = J.T @ J, J.T @ r0
        step = np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-9), -g)
        trial = np.sum(F(flat + step) ** 2)
        if trial < cost:
            flat, cost, lam = flat + step, trial, max(lam / 3, 1e-7)
            if np.abs(step).max() < 1e-8:
                break
        else:
            lam *= 4
            if lam > 1e7:
                break
    return flat[:n], [flat[n + 3 * i:n + 3 * i + 3] for i in range(len(scans))], float(np.sqrt(cost / len(r0)))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--scan', nargs=5, action='append', required=True,
                    metavar=('NAME', 'CLOUD', 'VIEWS', 'SCAN_AXIS', 'SCAN_PLANE'), help='a T02 scan used in the fit')
    ap.add_argument('--check', nargs=5, action='append', default=[],
                    metavar=('NAME', 'CLOUD', 'VIEWS', 'SCAN_AXIS', 'SCAN_PLANE'), help='a T02 scan only evaluated')
    ap.add_argument('--base-axis', type=Path, required=True)
    ap.add_argument('--base-plane', type=Path, required=True)
    ap.add_argument('--no-plane-tilt', action='store_true')
    ap.add_argument('--no-taper', action='store_true', help='T02 sections are exact cylinders (no print taper)')
    ap.add_argument('--fix-scale', action='store_true', help='keep the installed plane offset (only the axis, tilt, taper move)')
    ap.add_argument('--orientation', choices=('down', 'up'), help='known placement of every scan (default: from radii)')
    ap.add_argument('--height-weight', type=float, default=10.0)
    ap.add_argument('--out', type=Path, required=True, help='new folder: fit JSON, laser_plane, turntable_axis')
    a = ap.parse_args()
    if a.out.exists():
        raise FileExistsError(a.out)
    axis0 = json.loads(a.base_axis.read_text())
    plane0 = json.loads(a.base_plane.read_text())
    coef = np.array(plane0['plane_coefficients'], float)
    n0, D0 = coef[:3] / np.linalg.norm(coef[:3]), -coef[3] / np.linalg.norm(coef[:3])
    p0, d0 = np.array(axis0['axis_point_camera_mm'], float), np.array(axis0['axis_direction_camera'], float)
    d0 /= np.linalg.norm(d0)
    model = Model(p0, d0, n0, D0)

    def load(spec):
        name, cloud, views, axis, plane = spec
        cl, vw = np.load(cloud), np.load(views)
        pc = np.array(json.loads(Path(plane).read_text())['plane_coefficients'], float)
        assert np.allclose(pc[:3] / np.linalg.norm(pc[:3]), n0, atol=1e-9), 'scan plane normal differs'
        P, th, view = camera_points(cl, vw, json.loads(Path(axis).read_text()), -pc[3] / np.linalg.norm(pc[:3]), D0)
        return Scan(name, P, th, view, cl[:, 3], model, a.orientation)
    scans = [load(s) for s in a.scan]
    checks = [load(s) for s in a.check]
    rng = np.random.default_rng(0)
    idxs = [rng.choice(np.flatnonzero(s.keep & (s.view % 2 == 0)), 8000, replace=False) for s in scans]
    x0 = np.r_[1.0, np.zeros(9)]
    fixed = ((0,) if a.fix_scale else ()) + ((5, 6) if a.no_plane_tilt else ()) + ((7, 8, 9) if a.no_taper else ())
    x, owns, rms = solve(scans, model, x0.copy(), [s.x0.copy() for s in scans], idxs, a.height_weight, fixed)
    p, d, n = model.frame(x)
    report = dict(schema='t02_placements_fit_v1', scale=float(x[0]), plane_tilt=bool(not a.no_plane_tilt),
                  plane_normal_change_deg=float(np.degrees(np.arccos(min(1.0, float(n @ n0))))),
                  axis_point_camera_mm=p.tolist(), axis_direction_camera=d.tolist(),
                  axis_shift_mm=float(np.linalg.norm(np.cross(p - p0, d))),
                  axis_tilt_change_deg=float(np.degrees(np.arccos(min(1.0, abs(float(d @ d0)))))),
                  laser_plane=(np.r_[n, -x[0] * D0]).tolist(), fit_rms=rms,
                  print_taper_d_per_16mm={'small': 32 * float(x[7]), 'middle': 32 * float(x[8]), 'large': 32 * float(x[9])}, height_weight=a.height_weight,
                  base_axis=str(a.base_axis), base_plane=str(a.base_plane), fitted=[], checked=[])
    for s, own in zip(scans, owns):
        report['fitted'].append(dict(name=s.name, centre_offset_mm=own[:2].tolist(), base_up_mm=float(own[2]),
                                     before_odd=s.evaluate(model, x0, s.view % 2 == 1),
                                     after_odd=s.evaluate(model, x, s.view % 2 == 1)))
    for s in checks:
        report['checked'].append(dict(name=s.name, before=s.evaluate(model, x0, np.ones(len(s.view), bool)),
                                      after=s.evaluate(model, x, np.ones(len(s.view), bool))))
    a.out.mkdir(parents=True)
    (a.out / 'FIT.json').write_text(json.dumps(report, indent=2))
    plane = dict(plane0, plane_coefficients=report['laser_plane'], previous_plane_coefficients=coef.tolist(),
                 adjusted_by='scripts/fit_t02_placements.py ' + ', '.join(s.name for s in scans))
    (a.out / 'laser_plane_r41.json').write_text(json.dumps(plane, indent=2))
    axis = dict(axis0, axis_point_camera_mm=p.tolist(), axis_direction_camera=d.tolist(),
                previous_axis_point_camera_mm=p0.tolist(), previous_axis_direction_camera=d0.tolist(),
                adjusted_by='scripts/fit_t02_placements.py ' + ', '.join(s.name for s in scans))
    (a.out / 'turntable_axis.json').write_text(json.dumps(axis, indent=2))
    print(json.dumps({k: report[k] for k in ('scale', 'plane_normal_change_deg', 'axis_shift_mm',
                                             'axis_tilt_change_deg', 'fit_rms', 'print_taper_d_per_16mm')}))
    for group in ('fitted', 'checked'):
        for item in report[group]:
            for key in ('before_odd', 'after_odd', 'before', 'after'):
                if key in item:
                    e = item[key]
                    print(group, item['name'], key, e['orientation'],
                          [(sec['nominal_d'], sec['d'], sec['taper_bottom_to_top'], sec['roundness_rms']) for sec in e['sections']],
                          'heights', e['section_heights'], 'total', e['total_height'])


if __name__ == '__main__':
    main()
