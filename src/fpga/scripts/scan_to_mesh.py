"""Closed mesh (binary STL) from a turntable scan of a star-shaped object.

Points in the object frame (u, w, up) around the object's own axis become a radius map over
angle x height: median per cell, gaps filled around the circle and along the height, a light
smoothing, then side, bottom and top caps. Valid where every horizontal section is star-shaped
around the axis (a ray from the axis meets the surface once); not a general surface reconstructor.
"""
import argparse
import json
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fit_joint_artifacts import camera_points, turn_back
from compare_vase_stl import circle


def radius_map(u, w, z, n_theta, dz):
    theta = np.mod(np.arctan2(w, u), 2 * np.pi)
    r = np.hypot(u, w)
    zi = np.floor((z - z.min()) / dz).astype(int)
    ti = np.floor(theta / (2 * np.pi) * n_theta).astype(int) % n_theta
    nz = zi.max() + 1
    grid = np.full((nz, n_theta), np.nan)
    order = np.lexsort((ti, zi))
    keys = zi[order] * n_theta + ti[order]
    starts = np.r_[0, np.flatnonzero(np.diff(keys)) + 1]
    for s, e in zip(starts, np.r_[starts[1:], len(keys)]):
        k = keys[s]
        grid[k // n_theta, k % n_theta] = np.median(r[order[s:e]])
    return grid, z.min()


def fill(grid):
    g = grid.copy()
    nz, nt = g.shape
    for i in range(nz):                        # around the circle
        row = g[i]
        ok = np.isfinite(row)
        if ok.sum() >= 3:
            idx = np.flatnonzero(ok)
            g[i] = np.interp(np.arange(nt), np.r_[idx - nt, idx, idx + nt], np.r_[row[idx], row[idx], row[idx]])
    for j in range(nt):                        # along the height
        col = g[:, j]
        ok = np.isfinite(col)
        if ok.sum() >= 2:
            g[:, j] = np.interp(np.arange(nz), np.flatnonzero(ok), col[ok])
    return g


def smooth(g, passes=2):
    for _ in range(passes):
        g = (np.roll(g, 1, 1) + g * 2 + np.roll(g, -1, 1)) / 4
        g[1:-1] = (g[:-2] + g[1:-1] * 2 + g[2:]) / 4
    return g


def write_stl(path, tris):
    with open(path, 'wb') as out:
        out.write(b'scan3d turntable mesh'.ljust(80, b' '))
        out.write(struct.pack('<I', len(tris)))
        for t in tris:
            n = np.cross(t[1] - t[0], t[2] - t[0])
            n = n / (np.linalg.norm(n) or 1)
            out.write(struct.pack('<12fH', *n, *t[0], *t[1], *t[2], 0))


def mesh(g, z0, dz, n_theta):
    nz = g.shape[0]
    ang = np.arange(n_theta) * 2 * np.pi / n_theta
    V = np.stack([g * np.cos(ang)[None], g * np.sin(ang)[None], (z0 + (np.arange(nz) + .5) * dz)[:, None] * np.ones((1, n_theta))], -1)
    tris = []
    for i in range(nz - 1):
        for j in range(n_theta):
            a, b = V[i, j], V[i, (j + 1) % n_theta]
            c, d = V[i + 1, j], V[i + 1, (j + 1) % n_theta]
            tris += [(a, b, d), (a, d, c)]
    for row, flip in ((0, True), (nz - 1, False)):       # caps
        centre = np.array([0.0, 0.0, V[row, 0, 2]])
        for j in range(n_theta):
            a, b = V[row, j], V[row, (j + 1) % n_theta]
            tris.append((centre, b, a) if flip else (centre, a, b))
    return np.array(tris)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('cloud', type=Path)
    ap.add_argument('views', type=Path)
    ap.add_argument('scan_axis', type=Path, help='axis the scan was stitched with')
    ap.add_argument('--shared', type=float, nargs=5, default=(1.0, 0, 0, 0, 0),
                    help='scale and axis correction relative to --reference-axis (fit output)')
    ap.add_argument('--reference-axis', type=Path)
    ap.add_argument('--zmin', type=float, required=True, help='lowest up value kept (turntable surface)')
    ap.add_argument('--zmax', type=float, required=True)
    ap.add_argument('--theta', type=int, default=180)
    ap.add_argument('--dz', type=float, default=0.5)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    cloud, views = np.load(a.cloud), np.load(a.views)
    scan_axis = json.loads(a.scan_axis.read_text())
    ref = json.loads((a.reference_axis or a.scan_axis).read_text())
    p0, d0 = np.array(ref['axis_point_camera_mm'], float), np.array(ref['axis_direction_camera'], float)
    P, th = camera_points(cloud, views, scan_axis, 3547.0)
    u, w, up, _, _ = turn_back(P, th, np.array(a.shared), p0, d0)
    keep = (cloud[:, 3] > 0.5) & (np.hypot(u, w) < 40) & (up > a.zmin) & (up < a.zmax)
    u, w, up = u[keep], w[keep], up[keep]
    cx, cy, _, _ = circle(np.column_stack((u, w)))           # the object's own axis
    u, w = u - cx, w - cy
    grid, z0 = radius_map(u, w, up - a.zmin, a.theta, a.dz)
    covered = float(np.isfinite(grid).mean())
    g = smooth(fill(grid))
    tris = mesh(g, z0, a.dz, a.theta)
    write_stl(a.out, tris)
    print(json.dumps(dict(out=str(a.out), triangles=len(tris), cells_covered=round(covered, 3),
                          height=round(float(g.shape[0] * a.dz), 1), points=int(len(u)))))


if __name__ == '__main__':
    main()
