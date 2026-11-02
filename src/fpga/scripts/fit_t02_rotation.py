"""Calibrate the turntable axis (and refine the scale) from a T02 rotation scan.

T02: three coaxial cylinders, diameters 40 / 30 / 20 mm, 16 mm each. The scan gives, per view,
object-frame points made with the current axis (p0, d0) and the motor angle. Undoing that gives
the camera points; then the axis (point 2, direction 2), a scale s about the camera centre, T02's
centre offset from the axis on the turntable (2) and its base height are fitted so every point
lies on its section's cylinder. Even views fit, odd views check (same object: not an accuracy test).
"""
import argparse
import json
from pathlib import Path

import numpy as np

RADII = np.array([10.0, 15.0, 20.0])
SECTION = 16.0


def rot(axis, angle):
    x, y, z = axis
    k = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    return np.eye(3) + np.sin(angle) * k + (1 - np.cos(angle)) * (k @ k)


def basis(d):
    d = d / np.linalg.norm(d)
    e1 = np.cross(d, [0.0, 0.0, 1.0])
    e1 /= np.linalg.norm(e1)
    return d, e1, np.cross(d, e1)


def object_coords(P, theta, x, p0, d0):
    s, a, b, c, e = x[:5]
    _, e1, e2 = basis(d0)
    d = d0 + a * e1 + b * e2
    d /= np.linalg.norm(d)
    p = p0 + c * e1 + e * e2
    v = s * P - p
    # undo the turntable rotation of each point: rot(d, -theta) v (Rodrigues, vectorised)
    ct, st = np.cos(-theta)[:, None], np.sin(-theta)[:, None]
    q = v * ct + np.cross(d, v) * st + np.outer(v @ d, d) * (1 - ct)
    h = q @ d
    _, f1, f2 = basis(d)
    return q @ f1, q @ f2, h, p, d


def residuals(P, theta, section, x, p0, d0, w_height):
    u, w, h, _, _ = object_coords(P, theta, x, p0, d0)
    r = np.hypot(u - x[5], w - x[6])
    res = [r - RADII[section]]
    for k in range(3):
        lo, hi = np.percentile(h[section == k], [2, 98])
        res.append(w_height * np.array([lo - (x[7] + SECTION * k), hi - (x[7] + SECTION * (k + 1))]))
    return np.concatenate(res)


def evaluate(P, theta, section, x, p0, d0):
    u, w, h, _, _ = object_coords(P, theta, x, p0, d0)
    out = []
    for k in range(3):
        m = section == k
        uu, ww = u[m], w[m]
        A = np.column_stack((uu, ww, np.ones_like(uu)))
        c = np.linalg.lstsq(A, uu ** 2 + ww ** 2, rcond=None)[0]
        cx, cy = c[0] / 2, c[1] / 2
        R = np.sqrt(c[2] + cx ** 2 + cy ** 2)
        res = np.hypot(uu - cx, ww - cy) - R
        lo, hi = np.percentile(h[m], [2, 98])
        out.append(dict(nominal_d=2 * RADII[k], measured_d=round(2 * float(R), 3),
                        error_d=round(2 * float(R) - 2 * RADII[k], 3),
                        roundness_rms=round(float(np.sqrt(np.mean(res ** 2))), 3),
                        height_2_98=round(float(hi - lo), 2), points=int(m.sum())))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('cloud', type=Path, help='object-frame points (x, y, z, measurement flag), .npy')
    ap.add_argument('views', type=Path, help='per accepted view: motor steps, point count, .npy')
    ap.add_argument('axis', type=Path, help='turntable_axis.json used by the scan')
    ap.add_argument('--steps-per-turn', type=float, default=3547.0)
    ap.add_argument('--sections', type=float, nargs=4, default=(10.0, 26.0, 42.0, 60.0),
                    help='T02 section bounds along the current axis (small, middle, large); the rest is dropped')
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    cloud, views = np.load(a.cloud), np.load(a.views)
    axis = json.loads(a.axis.read_text())
    p0, d0 = np.array(axis['axis_point_camera_mm'], float), np.array(axis['axis_direction_camera'], float)
    d0 /= np.linalg.norm(d0)
    theta = np.repeat(views[:, 0] * 2 * np.pi / a.steps_per_turn, views[:, 1])
    view_index = np.repeat(np.arange(len(views)), views[:, 1])
    q = cloud[:, :3]
    # camera points: rot(d0, theta) (q - p0) + p0, per point
    v = q - p0
    ct, st = np.cos(theta)[:, None], np.sin(theta)[:, None]
    P = v * ct + np.cross(d0, v) * st + np.outer(v @ d0, d0) * (1 - ct) + p0
    keep = cloud[:, 3] > 0.5
    x0 = np.array([1.0, 0, 0, 0, 0, 0, 0, 0.0])
    u, w, h, _, _ = object_coords(P, theta, x0, p0, d0)
    r = np.hypot(u, w)
    # sections by height along the current axis (small, middle, large), 1 mm kept clear of each step
    b = np.asarray(a.sections, float)
    section = np.searchsorted(b[1:-1], h)
    clear = np.min(np.abs(h[:, None] - b[None, :]), axis=1) > 1.0
    keep &= (r < 40) & (h > b[0]) & (h < b[-1]) & clear
    for k in range(3):                         # drop stray points far from the section's radius
        m = keep & (section == k)
        keep &= ~((section == k) & (np.abs(r - np.median(r[m])) > 2.0))
    x0[7] = b[0]
    fit_set = keep & (view_index % 2 == 0)
    check_set = keep & (view_index % 2 == 1)
    rng = np.random.default_rng(0)
    idx = np.flatnonzero(fit_set)
    idx = rng.choice(idx, min(len(idx), 12000), replace=False)
    Pf, tf, sf = P[idx], theta[idx], section[idx]
    x, lam = x0.copy(), 1e-2
    cost = np.sum(residuals(Pf, tf, sf, x, p0, d0, 10.0) ** 2)
    for _ in range(100):                      # Levenberg-Marquardt
        f = residuals(Pf, tf, sf, x, p0, d0, 10.0)
        J = np.empty((len(f), len(x)))
        for j in range(len(x)):
            dx = np.zeros_like(x)
            dx[j] = 1e-6 if j == 0 else 1e-4
            J[:, j] = (residuals(Pf, tf, sf, x + dx, p0, d0, 10.0) - f) / dx[j]
        A, g = J.T @ J, J.T @ f
        step = np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-9), -g)
        trial = np.sum(residuals(Pf, tf, sf, x + step, p0, d0, 10.0) ** 2)
        if trial < cost:
            x, cost, lam = x + step, trial, max(lam / 3, 1e-6)
            if np.abs(step).max() < 1e-7:
                break
        else:
            lam *= 4
            if lam > 1e6:
                break
    before = evaluate(P[check_set], theta[check_set], section[check_set], x0, p0, d0)
    after_fit = evaluate(P[fit_set], theta[fit_set], section[fit_set], x, p0, d0)
    after_check = evaluate(P[check_set], theta[check_set], section[check_set], x, p0, d0)
    _, _, _, p, d = object_coords(P[:1], theta[:1], x, p0, d0)
    out = dict(schema='t02_rotation_fit_v1', scale=float(x[0]), axis_point_camera_mm=p.tolist(),
               axis_direction_camera=d.tolist(), t02_centre_offset_mm=[float(x[5]), float(x[6])],
               base_height_mm=float(x[7]), axis_shift_mm=float(np.linalg.norm(np.cross(p - p0, d))),
               axis_tilt_change_deg=float(np.degrees(np.arccos(min(1.0, abs(float(d @ d0)))))),
               before_odd_views=before, after_even_views=after_fit, after_odd_views=after_check,
               note='calibrated on T02 (even views), checked on odd views of the same T02: not an accuracy test')
    a.out.write_text(json.dumps(out, indent=2))
    for key in ('scale', 'axis_shift_mm', 'axis_tilt_change_deg', 't02_centre_offset_mm', 'base_height_mm'):
        print(key, out[key])
    for key in ('before_odd_views', 'after_even_views', 'after_odd_views'):
        print(key, json.dumps(out[key]))


if __name__ == '__main__':
    main()
