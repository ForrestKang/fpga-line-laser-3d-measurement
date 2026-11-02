"""Compare a turntable scan with the STL it was printed from, layer by layer.

Both are cut into 1 mm height layers; each layer gets a circle fit (diameter, roundness). The
STL (z up, base at z=0) is only used to find the scan's base height (one vertical shift) and to
score: no scan coordinate is changed. Writes a JSON table and a PNG.
"""
import argparse
import json
import struct
from pathlib import Path

import numpy as np


def load_stl(path):
    data = Path(path).read_bytes()
    n = struct.unpack_from('<I', data, 80)[0]
    if len(data) == 84 + 50 * n:
        tri = np.frombuffer(data, np.dtype([('n', '<3f4'), ('v', '<9f4'), ('a', '<u2')]), n, 84)['v']
        return tri.reshape(-1, 3, 3).astype(float)
    verts = [list(map(float, line.split()[1:4])) for line in data.decode().splitlines() if line.strip().startswith('vertex')]
    return np.array(verts, float).reshape(-1, 3, 3)


def slice_points(tri, z):
    """Points where the triangle edges cross the plane z."""
    out = []
    for i, j in ((0, 1), (1, 2), (2, 0)):
        a, b = tri[:, i], tri[:, j]
        m = (a[:, 2] - z) * (b[:, 2] - z) < 0
        t = (z - a[m, 2]) / (b[m, 2] - a[m, 2])
        out.append(a[m, :2] + t[:, None] * (b[m, :2] - a[m, :2]))
    return np.concatenate(out)


def circle(xy):
    x, y = xy[:, 0], xy[:, 1]
    c = np.linalg.lstsq(np.column_stack((x, y, np.ones_like(x))), x ** 2 + y ** 2, rcond=None)[0]
    cx, cy = c[0] / 2, c[1] / 2
    r = np.sqrt(c[2] + cx ** 2 + cy ** 2)
    return cx, cy, r, np.hypot(x - cx, y - cy) - r


def scan_layers(cloud, axis, max_radius, min_points):
    P, meas = cloud[:, :3], cloud[:, 3] > 0.5
    p = np.array(axis['axis_point_camera_mm'], float)
    d = np.array(axis['axis_direction_camera'], float)
    d /= np.linalg.norm(d)
    e1 = np.cross(d, [0.0, 0.0, 1.0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(d, e1)
    v = P - p
    h = -(v @ d)                       # camera +y points down: height is up
    xy = np.column_stack((v @ e1, v @ e2))
    keep = meas & (np.hypot(xy[:, 0], xy[:, 1]) < max_radius)
    layers = {}
    for z in np.arange(np.floor(h[keep].min()), np.ceil(h[keep].max())):
        m = keep & (h >= z) & (h < z + 1)
        if m.sum() < min_points:
            continue
        cx, cy, r, res = circle(xy[m])
        good = np.abs(res) < 3
        cx, cy, r, res = circle(xy[m][good])
        layers[float(z + 0.5)] = (2 * r, float(np.sqrt(np.mean(res ** 2))), int(m.sum()))
    return layers


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('cloud', type=Path)
    ap.add_argument('axis', type=Path)
    ap.add_argument('stl', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--max-radius', type=float, default=40.0)
    a = ap.parse_args()
    tri = load_stl(a.stl)
    zmax = tri[:, :, 2].max()
    model = {}
    for z in np.arange(0.5, zmax, 1.0):
        pts = slice_points(tri, z)
        if len(pts) >= 12:
            _, _, r, res = circle(pts)
            model[float(z)] = (2 * r, float(np.sqrt(np.mean(res ** 2))))
    scan = scan_layers(np.load(a.cloud), json.loads(a.axis.read_text()), a.max_radius, 200)
    hs = np.array(sorted(scan))
    zs = np.array(sorted(model))
    md = np.array([model[z][0] for z in zs])
    best = None
    for shift in np.arange(hs.min() - 20, hs.max(), 0.25):       # scan height of the model's z = 0
        z = hs - shift
        inside = (z > 2) & (z < zmax - 1)
        if inside.sum() < 0.5 * len(zs):
            continue
        err = np.array([scan[h][0] for h in hs[inside]]) - np.interp(z[inside], zs, md)
        score = np.median(np.abs(err))
        if best is None or score < best[0]:
            best = (score, shift)
    shift = best[1]
    rows = []
    for h in hs:
        z = h - shift
        if 0 <= z <= zmax:
            dm = float(np.interp(z, zs, md))
            rows.append(dict(z_model=round(float(z), 1), model_d=round(dm, 2), scan_d=round(scan[h][0], 2),
                             error=round(scan[h][0] - dm, 2), scan_roundness=round(scan[h][1], 2),
                             model_roundness=round(float(np.interp(z, zs, [model[k][1] for k in zs])), 2),
                             points=scan[h][2]))
    err = np.array([r['error'] for r in rows if 2 < r['z_model'] < zmax - 1])
    summary = dict(stl=str(a.stl), model_height=round(float(zmax), 2), base_shift=float(shift), layers=len(rows),
                   error_median=round(float(np.median(err)), 3), error_mean=round(float(np.mean(err)), 3),
                   error_abs_median=round(float(np.median(np.abs(err))), 3),
                   error_p95_abs=round(float(np.percentile(np.abs(err), 95)), 3),
                   scan_height_span=round(float(max(r['z_model'] for r in rows) - min(r['z_model'] for r in rows)), 1))
    a.out.write_text(json.dumps(dict(summary=summary, rows=rows), indent=2))
    print(json.dumps(summary))
    for r in rows[::3]:
        print(r)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 2, figsize=(12, 6), dpi=110)
    ax[0].plot(md, zs, label='STL model', lw=2)
    ax[0].plot([r['scan_d'] for r in rows], [r['z_model'] for r in rows], '.', label='scan (circle fit per 1 mm)')
    ax[0].set_xlabel('diameter mm'); ax[0].set_ylabel('height mm'); ax[0].legend(); ax[0].grid(alpha=.3)
    ax[1].plot([r['error'] for r in rows], [r['z_model'] for r in rows], '.-')
    ax[1].axvline(0, color='k', lw=.8); ax[1].set_xlabel('scan - model diameter, mm'); ax[1].grid(alpha=.3)
    ax[0].set_title('vase: scan vs STL'); ax[1].set_title('diameter error by height')
    fig.tight_layout()
    fig.savefig(a.out.with_suffix('.png'))


if __name__ == '__main__':
    main()
