"""T02 (40/30/20 mm, 3 x 16 mm, large end down) from a stitched scan: diameter, roundness and
height of every section, step heights from the radius changes, and T02's offset from the axis.
Model values are only used to name the sections and to score."""
import argparse
import json
from pathlib import Path

import numpy as np

NOMINAL_D = (40.0, 30.0, 20.0)                    # bottom to top


def circle(xy):
    x, y = xy[:, 0], xy[:, 1]
    c = np.linalg.lstsq(np.column_stack((x, y, np.ones_like(x))), x ** 2 + y ** 2, rcond=None)[0]
    cx, cy = c[0] / 2, c[1] / 2
    r = np.sqrt(c[2] + cx ** 2 + cy ** 2)
    return cx, cy, r, np.hypot(x - cx, y - cy) - r


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('cloud', type=Path, help='object-frame points from the scan (x, y, z, flag)')
    ap.add_argument('axis', type=Path, help='turntable_axis.json the scan was stitched with')
    ap.add_argument('--out', type=Path)
    a = ap.parse_args()
    cloud = np.load(a.cloud)
    axis = json.loads(a.axis.read_text())
    p = np.array(axis['axis_point_camera_mm'], float)
    d = np.array(axis['axis_direction_camera'], float)
    d /= np.linalg.norm(d)
    e1 = np.cross(d, [0.0, 0.0, 1.0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(d, e1)
    v = cloud[:, :3] - p
    u, w, up = v @ e1, v @ e2, -(v @ d)
    keep = (cloud[:, 3] > 0.5) & (np.hypot(u, w) < 30)
    u, w, up = u[keep], w[keep], up[keep]
    top = np.percentile(up, 99.8)              # T02 top face; the turntable hub lies below T02
    near = up > top - 50
    u, w, up = u[near], w[near], up[near]
    # 1 mm layers, then the step heights where the layer radius jumps
    layers = []
    for z in np.arange(np.floor(up.min()), np.ceil(up.max())):
        m = (up >= z) & (up < z + 1)
        if m.sum() >= 150:
            cx, cy, r, res = circle(np.column_stack((u[m], w[m])))
            good = np.abs(res) < 2
            cx, cy, r, res = circle(np.column_stack((u[m][good], w[m][good])))
            layers.append((z + 0.5, 2 * r, cx, cy, float(np.sqrt(np.mean(res ** 2)))))
    L = np.array(layers)
    jumps = np.flatnonzero(np.abs(np.diff(L[:, 1])) > 4)
    bounds = [L[0, 0] - 0.5] + [float((L[j, 0] + L[j + 1, 0]) / 2) for j in jumps] + [L[-1, 0] + 0.5]
    sections = []
    for k in range(len(bounds) - 1):
        lo, hi = bounds[k], bounds[k + 1]
        m = (up > lo + 1) & (up < hi - 1)
        cx, cy, r, res = circle(np.column_stack((u[m], w[m])))
        good = np.abs(res) < 2
        cx, cy, r, res = circle(np.column_stack((u[m][good], w[m][good])))
        sections.append(dict(bottom=round(lo, 2), top=round(hi, 2), height=round(hi - lo, 2), diameter=round(2 * r, 3),
                             roundness_rms=round(float(np.sqrt(np.mean(res ** 2))), 3),
                             centre_offset=[round(float(cx), 2), round(float(cy), 2)], points=int(m.sum())))
    order = sorted(range(len(sections)), key=lambda i: sections[i]['bottom'])
    rows = []
    for i, k in enumerate(order[:3]):
        s = sections[k]
        rows.append(dict(s, nominal_d=NOMINAL_D[i], error_d=round(s['diameter'] - NOMINAL_D[i], 3),
                         error_h=round(s['height'] - 16.0, 2)))
    out = dict(cloud=str(a.cloud), axis=str(a.axis), sections=rows, layers=len(L))
    if a.out:
        a.out.write_text(json.dumps(out, indent=2))
    for r in rows:
        print(json.dumps(r))


if __name__ == '__main__':
    main()
