"""Calibrate the reconstruction scale and the turntable axis from T02 (three coaxial cylinders).

T02 model (mm, large end down): diameter 40 / 30 / 20, each section 16 high, total 48.
Input: camera-frame PL points of T02 (one static laser profile or more) made with the current
calibration. Unknowns: s (scales every point about the camera centre, which equals scaling the
laser-plane offset), the axis direction (2) and point (2), and the T02 base height h0.
Residuals: distance to the axis minus the section radius, and the section height extents.

Using T02 to calibrate means T02 itself can no longer show the accuracy: check another object.
"""
import argparse
import json
from pathlib import Path

import numpy as np

RADII = np.array([20.0, 15.0, 10.0])          # bottom (large) to top (small)
SECTION = 16.0


def frame(d):
    d = d / np.linalg.norm(d)
    e1 = np.cross(d, [1.0, 0.0, 0.0])
    if np.linalg.norm(e1) < 1e-6:
        e1 = np.cross(d, [0.0, 0.0, 1.0])
    e1 /= np.linalg.norm(e1)
    return d, e1, np.cross(d, e1)


def geometry(P, x, p0, d0):
    s, a, b, c, e, _ = x
    _, e1, e2 = frame(d0)
    d = d0 + a * e1 + b * e2
    d /= np.linalg.norm(d)
    p = p0 + c * e1 + e * e2
    v = s * P - p
    h = v @ d
    r = np.linalg.norm(v - np.outer(h, d), axis=1)
    return r, h, p, d


def residuals(P, section, x, p0, d0, w_height):
    r, h, _, _ = geometry(P, x, p0, d0)
    res = [r - RADII[section]]
    h0 = x[5]
    for k in range(3):
        hk = h[section == k]
        lo, hi = np.percentile(hk, [1, 99])
        res.append(w_height * np.array([lo - (h0 + SECTION * k), hi - (h0 + SECTION * (k + 1))]))
    return np.concatenate(res)


def solve(P, section, p0, d0, w_height=30.0, iters=30):
    r, h, _, _ = geometry(P, np.array([1.0, 0, 0, 0, 0, 0]), p0, d0)
    x = np.array([1.0, 0, 0, 0, 0, np.percentile(h[section == 0], 1)])
    for _ in range(iters):
        f = residuals(P, section, x, p0, d0, w_height)
        J = np.empty((len(f), len(x)))
        for j in range(len(x)):
            dx = np.zeros_like(x)
            dx[j] = 1e-6 if j == 0 else 1e-4
            J[:, j] = (residuals(P, section, x + dx, p0, d0, w_height) - f) / dx[j]
        step = np.linalg.lstsq(J, -f, rcond=None)[0]
        x = x + step
        if np.abs(step).max() < 1e-7:
            break
    return x


def report(P, section, x, p0, d0, label):
    r, h, p, d = geometry(P, x, p0, d0)
    rows = []
    for k in range(3):
        m = section == k
        lo, hi = np.percentile(h[m], [1, 99])
        rows.append(dict(section=k, nominal_d=2 * RADII[k], measured_d=round(2 * float(np.median(r[m])), 3),
                         radius_rms=round(float(np.sqrt(np.mean((r[m] - RADII[k]) ** 2))), 3),
                         height=round(float(hi - lo), 2), points=int(m.sum())))
    print(label, json.dumps(rows))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('points', type=Path, help='camera-frame T02 points (N x 3 or more columns), .npy')
    ap.add_argument('axis', type=Path, help='current turntable_axis.json')
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    P = np.load(a.points)[:, :3].astype(float)
    axis = json.loads(a.axis.read_text())
    p0, d0 = np.array(axis['axis_point_camera_mm']), np.array(axis['axis_direction_camera'])
    r, h, _, _ = geometry(P, np.array([1.0, 0, 0, 0, 0, 0]), p0, d0)
    # sections by the current radius clusters: large (bottom), middle, small (top)
    centres = np.sort(np.percentile(r, [10, 50, 90]))[::-1]
    for _ in range(10):
        section = np.argmin(np.abs(r[:, None] - centres[None, :]), axis=1)
        centres = np.array([np.median(r[section == k]) for k in range(3)])
    keep = np.abs(r - centres[section]) < 1.5
    P, section = P[keep], section[keep]
    print('points', len(P), 'current radii', centres.round(2))
    before = report(P, section, np.array([1.0, 0, 0, 0, 0, 0]), p0, d0, 'BEFORE')
    x = solve(P, section, p0, d0)
    after = report(P, section, x, p0, d0, 'AFTER')
    _, _, p, d = geometry(P, x, p0, d0)
    out = dict(schema='t02_artifact_fit_v1', scale=float(x[0]), plane_offset_factor=float(x[0]),
               axis_point_camera_mm=p.tolist(), axis_direction_camera=d.tolist(), base_height_mm=float(x[5]),
               axis_shift_mm=float(np.linalg.norm(np.cross(p - p0, d))), axis_tilt_change_deg=float(
                   np.degrees(np.arccos(min(1.0, abs(float(d @ (d0 / np.linalg.norm(d0)))))))),
               before=before, after=after, points=str(a.points), source_axis=str(a.axis),
               note='calibrated on T02: T02 errors after this fit are not an accuracy result')
    a.out.write_text(json.dumps(out, indent=2))
    print('FIT', json.dumps({k: out[k] for k in ('scale', 'axis_shift_mm', 'axis_tilt_change_deg', 'base_height_mm')}))


if __name__ == '__main__':
    main()
