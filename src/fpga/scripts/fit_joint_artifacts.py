"""Joint calibration of the reconstruction scale and the turntable axis from two printed objects.

T02 (three coaxial cylinders 40/30/20 mm, 16 mm each) and the C1 vase (its STL gives the mean
radius of every 1 mm layer). Each rotation scan supplies object-frame points made with the axis
it was scanned with, plus per-view motor steps and point counts; undoing that gives camera
points. Shared unknowns: scale s about the camera centre (= laser-plane offset factor), axis
point (2) and direction (2). Per object: centre offset on the turntable (2) and base height (1).
A third object that took no part here is needed to state accuracy.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_vase_stl import circle, load_stl, slice_points

T02_RADII = np.array([10.0, 15.0, 20.0])


def basis(d):
    d = d / np.linalg.norm(d)
    e1 = np.cross(d, [0.0, 0.0, 1.0])
    e1 /= np.linalg.norm(e1)
    return d, e1, np.cross(d, e1)


def camera_points(cloud, views, axis, steps_per_turn):
    p0, d0 = np.array(axis['axis_point_camera_mm'], float), np.array(axis['axis_direction_camera'], float)
    d0 /= np.linalg.norm(d0)
    theta = np.repeat(views[:, 0] * 2 * np.pi / steps_per_turn, views[:, 1])
    v = cloud[:, :3] - p0
    ct, st = np.cos(theta)[:, None], np.sin(theta)[:, None]
    return v * ct + np.cross(d0, v) * st + np.outer(v @ d0, d0) * (1 - ct) + p0, theta


def turn_back(P, theta, shared, p0, d0):
    """Object-frame (u, w, up) for camera points under the shared parameters."""
    s, a, b, c, e = shared
    _, e1, e2 = basis(d0)
    d = d0 + a * e1 + b * e2
    d /= np.linalg.norm(d)
    p = p0 + c * e1 + e * e2
    v = s * P - p
    ct, st = np.cos(-theta)[:, None], np.sin(-theta)[:, None]
    q = v * ct + np.cross(d, v) * st + np.outer(v @ d, d) * (1 - ct)
    _, f1, f2 = basis(d)
    return q @ f1, q @ f2, -(q @ d), p, d          # camera +y is down: up = -d


class T02:
    def __init__(self, P, theta, section, height_weight=10.0):
        self.P, self.theta, self.section, self.height_weight = P, theta, section, height_weight

    def residuals(self, shared, own, p0, d0):
        u, w, up, _, _ = turn_back(self.P, self.theta, shared, p0, d0)
        r = np.hypot(u - own[0], w - own[1])
        res = [r - T02_RADII[self.section]]
        for k in range(3):            # section k from the top: small, middle, large
            lo, hi = np.percentile(up[self.section == k], [2, 98])
            top = own[2] + 16.0 * (3 - k)
            res.append(self.height_weight * np.array([hi - top, lo - (top - 16.0)]))
        return np.concatenate(res)


class Vase:
    def __init__(self, P, theta, z_model, r_model):
        self.P, self.theta, self.zm, self.rm = P, theta, z_model, r_model

    def residuals(self, shared, own, p0, d0):
        u, w, up, _, _ = turn_back(self.P, self.theta, shared, p0, d0)
        r = np.hypot(u - own[0], w - own[1])
        z = up - own[2]                                 # model height of each point
        inside = (z > 3) & (z < 47)
        return np.where(inside, r - np.interp(z, self.zm, self.rm), 0.0)


def solve(objects, p0, d0, x, fixed=()):
    def f(x):
        shared = x[:5]
        return np.concatenate([o.residuals(shared, x[5 + 3 * i:8 + 3 * i], p0, d0) for i, o in enumerate(objects)])
    lam, cost = 1e-2, np.sum(f(x) ** 2)
    for _ in range(80):
        r0 = f(x)
        J = np.empty((len(r0), len(x)))
        for j in range(len(x)):
            dx = np.zeros_like(x)
            dx[j] = 1e-6 if j == 0 else 1e-4
            J[:, j] = (f(x + dx) - r0) / dx[j]
        J[:, list(fixed)] = 0.0                       # held parameters do not move
        A, g = J.T @ J, J.T @ r0
        step = np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-9), -g)
        trial = np.sum(f(x + step) ** 2)
        if trial < cost:
            x, cost, lam = x + step, trial, max(lam / 3, 1e-6)
            if np.abs(step).max() < 1e-7:
                break
        else:
            lam *= 4
            if lam > 1e6:
                break
    return x


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--t02', type=Path, nargs=3, action='append', required=True, metavar=('CLOUD', 'VIEWS', 'AXIS'),
                    help='one T02 rotation scan; repeat for more placements')
    ap.add_argument('--vase', type=Path, nargs=4, metavar=('CLOUD', 'VIEWS', 'AXIS', 'STL'),
                    help='vase scan with its STL; omitted: T02 scans only')
    ap.add_argument('--reference-axis', type=Path, required=True, help='axis the shared parameters start from')
    ap.add_argument('--scale-only', action='store_true', help='hold the axis, fit the scale (and per-object offsets)')
    ap.add_argument('--height-weight', type=float, default=10.0,
                    help='weight of the T02 section extents; 0 fits the diameters only')
    ap.add_argument('--steps-per-turn', type=float, default=3547.0)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    ref = json.loads(a.reference_axis.read_text())
    p0, d0 = np.array(ref['axis_point_camera_mm'], float), np.array(ref['axis_direction_camera'], float)
    d0 /= np.linalg.norm(d0)
    rng = np.random.default_rng(0)

    objects, x0 = [], [1.0, 0, 0, 0, 0]
    for cloud_path, views_path, axis_path in a.t02:
        cloud, views = np.load(cloud_path), np.load(views_path)
        P, th = camera_points(cloud, views, json.loads(axis_path.read_text()), a.steps_per_turn)
        u, w, up, _, _ = turn_back(P, th, np.array([1.0, 0, 0, 0, 0]), p0, d0)
        r = np.hypot(u, w)
        keep = (cloud[:, 3] > 0.5) & (r < 30)
        top = np.percentile(up[keep], 99.8)
        depth = top - up                                # from the top face down
        section = np.clip((depth // 16).astype(int), 0, 2)
        clear = np.abs(depth - 16 * np.round(depth / 16)) > 1.0
        keep &= clear & (depth > 1) & (depth < 47)
        for k in range(3):
            m = keep & (section == k)
            keep &= ~((section == k) & (np.abs(r - np.median(r[m])) > 3.0))
        idx = rng.choice(np.flatnonzero(keep), min(keep.sum(), 8000), replace=False)
        objects.append(T02(P[idx], th[idx], section[idx], a.height_weight))
        x0 += [0.0, 0.0, top - 48.0]
    if a.vase:
        cloud_v, views_v = np.load(a.vase[0]), np.load(a.vase[1])
        Pv, thv = camera_points(cloud_v, views_v, json.loads(a.vase[2].read_text()), a.steps_per_turn)
        tri = load_stl(a.vase[3])
        zs = np.arange(0.5, tri[:, :, 2].max(), 1.0)
        rm = np.array([circle(slice_points(tri, z))[2] for z in zs])
        u, w, up, _, _ = turn_back(Pv, thv, np.array([1.0, 0, 0, 0, 0]), p0, d0)
        keep_v = (cloud_v[:, 3] > 0.5) & (np.hypot(u, w) < 30)
        shift = json.loads(Path('tmp/vase/vase_vs_stl.json').read_text())['summary']['base_shift']
        keep_v &= (up - shift > 3) & (up - shift < 47)
        idx = rng.choice(np.flatnonzero(keep_v), min(keep_v.sum(), 8000), replace=False)
        objects.append(Vase(Pv[idx], thv[idx], zs, rm))
        x0 += [0.0, 0.0, shift]
    x0 = np.array(x0)
    before = [float(np.sqrt(np.mean(o.residuals(x0[:5], x0[5 + 3 * i:8 + 3 * i], p0, d0) ** 2))) for i, o in enumerate(objects)]
    x = solve(objects, p0, d0, x0, fixed=(1, 2, 3, 4) if a.scale_only else ())
    after = [float(np.sqrt(np.mean(o.residuals(x[:5], x[5 + 3 * i:8 + 3 * i], p0, d0) ** 2))) for i, o in enumerate(objects)]
    _, _, _, p, d = turn_back(objects[0].P[:1], objects[0].theta[:1], x[:5], p0, d0)
    out = dict(schema='joint_artifact_fit_v2', scale=float(x[0]), axis_point_camera_mm=p.tolist(),
               axis_direction_camera=d.tolist(), axis_shift_mm=float(np.linalg.norm(np.cross(p - p0, d))),
               axis_tilt_change_deg=float(np.degrees(np.arccos(min(1.0, abs(float(d @ d0)))))),
               objects=[dict(kind=type(o).__name__, centre_offset_mm=x[5 + 3 * i:7 + 3 * i].tolist(),
                             base_up_mm=float(x[7 + 3 * i]), rms_before=before[i], rms_after=after[i])
                        for i, o in enumerate(objects)],
               reference_axis=str(a.reference_axis),
               note='calibrated on the listed objects: state accuracy only on an object not listed')
    a.out.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    main()
