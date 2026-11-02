"""Compare a turntable scan with its STL for objects whose sections are star-shaped around the axis.

Both become radius maps r(z, theta) (1 mm x 2 deg cells; cell value = median radius). The model
axis is its z axis (STL base at z = 0). Alignment searches the scan's base height, the object's
offset from the turntable axis and its turn about the axis; only these rigid parameters change,
never the scan's shape or size. Reports the per-layer mean radius and the radial error per cell.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_vase_stl import load_stl

NT = 180


def model_map(tri, zs):
    """Radius of the outer surface per (z, angle): ray from the axis, farthest crossing."""
    out = np.full((len(zs), NT), np.nan)
    ang = (np.arange(NT) + 0.5) * 2 * np.pi / NT
    dirs = np.column_stack((np.cos(ang), np.sin(ang)))
    for i, z in enumerate(zs):
        segs = []
        for a_, b_ in ((0, 1), (1, 2), (2, 0)):
            pass
        za = tri[:, :, 2]
        cross = ((za.min(1) < z) & (za.max(1) > z))
        t = tri[cross]
        pts = []
        for j in range(3):
            a, b = t[:, j], t[:, (j + 1) % 3]
            m = (a[:, 2] - z) * (b[:, 2] - z) < 0
            s = (z - a[m, 2]) / (b[m, 2] - a[m, 2])
            pts.append((a[m] + s[:, None] * (b[m] - a[m]))[:, :2])
        # rebuild segments: each crossing triangle gives exactly two crossing points
        p = np.full((len(t), 2, 2), np.nan)
        cnt = np.zeros(len(t), int)
        for j in range(3):
            a, b = t[:, j], t[:, (j + 1) % 3]
            m = (a[:, 2] - z) * (b[:, 2] - z) < 0
            s = (z - a[m, 2]) / (b[m, 2] - a[m, 2])
            q = (a[m] + s[:, None] * (b[m] - a[m]))[:, :2]
            idx = np.flatnonzero(m)
            p[idx, np.minimum(cnt[idx], 1)] = q
            cnt[idx] += 1
        ok = cnt == 2
        A, B = p[ok, 0], p[ok, 1]
        if not len(A):
            continue
        # ray (0,0) + r*dir hits segment A + s(B-A): solve per direction
        D = B - A
        for k, dv in enumerate(dirs):
            den = dv[0] * D[:, 1] - dv[1] * D[:, 0]
            good = np.abs(den) > 1e-12
            r = (A[:, 0] * D[:, 1] - A[:, 1] * D[:, 0]) / np.where(good, den, 1)
            s = (A[:, 0] * dv[1] - A[:, 1] * dv[0]) / np.where(good, den, 1)
            hit = good & (r > 0) & (s >= 0) & (s <= 1)
            if hit.any():
                out[i, k] = r[hit].max()
    return out


def scan_map(u, w, up, base, cx, cy, zs):
    z = up - base
    th = np.mod(np.arctan2(w - cy, u - cx), 2 * np.pi)
    r = np.hypot(u - cx, w - cy)
    zi = np.floor(z - zs[0] + 0.5).astype(int)
    ti = np.floor(th / (2 * np.pi) * NT).astype(int) % NT
    ok = (zi >= 0) & (zi < len(zs))
    out = np.full((len(zs), NT), np.nan)
    key = zi[ok] * NT + ti[ok]
    rr = r[ok]
    order = np.argsort(key)
    key, rr = key[order], rr[order]
    starts = np.r_[0, np.flatnonzero(np.diff(key)) + 1]
    for s, e in zip(starts, np.r_[starts[1:], len(key)]):
        if e - s >= 2:
            out[key[s] // NT, key[s] % NT] = np.median(rr[s:e])
    return out


def score(sm, mm, shift):
    m = np.roll(mm, shift, axis=1)
    d = sm - m
    ok = np.isfinite(d)
    return np.median(np.abs(d[ok])) if ok.sum() > 500 else np.inf, d


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('cloud', type=Path)
    ap.add_argument('axis', type=Path)
    ap.add_argument('stl', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--zmin', type=float, default=1.5, help='skip the base flange below this model height')
    a = ap.parse_args()
    tri = load_stl(a.stl)
    zmax = tri[:, :, 2].max()
    zs = np.arange(a.zmin + 0.5, zmax - 0.5, 1.0)
    mm = model_map(tri, zs)
    cloud = np.load(a.cloud)
    ax = json.loads(a.axis.read_text())
    p = np.array(ax['axis_point_camera_mm'], float)
    d = np.array(ax['axis_direction_camera'], float)
    d /= np.linalg.norm(d)
    e1 = np.cross(d, [0.0, 0.0, 1.0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(d, e1)
    v = cloud[:, :3] - p
    u, w, up = v @ e1, v @ e2, -(v @ d)
    keep = (cloud[:, 3] > 0.5) & (np.hypot(u, w) < 32)
    u, w, up = u[keep], w[keep], up[keep]
    top = np.percentile(up, 99.9)
    best = None
    for base in np.arange(top - zmax - 3, top - zmax + 3.01, 0.5):        # coarse search
        for cx in np.arange(-3, 3.01, 1.0):
            for cy in np.arange(-3, 3.01, 1.0):
                sm = scan_map(u, w, up, base, cx, cy, zs)
                for sh in range(0, NT, 3):
                    sc, _ = score(sm, mm, sh)
                    if best is None or sc < best[0]:
                        best = (sc, base, cx, cy, sh)
    _, base, cx, cy, sh = best
    for step in (0.25, 0.1):                                            # refine
        improved = True
        while improved:
            improved = False
            for db, dx, dy, ds in ((step, 0, 0, 0), (-step, 0, 0, 0), (0, step, 0, 0), (0, -step, 0, 0),
                                   (0, 0, step, 0), (0, 0, -step, 0), (0, 0, 0, 1), (0, 0, 0, -1)):
                sm = scan_map(u, w, up, base + db, cx + dx, cy + dy, zs)
                sc, _ = score(sm, mm, (sh + ds) % NT)
                if sc < best[0]:
                    best = (sc, base + db, cx + dx, cy + dy, (sh + ds) % NT)
                    _, base, cx, cy, sh = best
                    improved = True
    sm = scan_map(u, w, up, base, cx, cy, zs)
    _, diff = score(sm, mm, sh)
    ok = np.isfinite(diff)
    layer = []
    mrolled = np.roll(mm, sh, axis=1)
    for i, z in enumerate(zs):
        both = np.isfinite(sm[i]) & np.isfinite(mrolled[i])
        if both.sum() >= NT * 0.5:
            layer.append(dict(z=float(z), model_mean_r=round(float(np.mean(mrolled[i][both])), 3),
                              scan_mean_r=round(float(np.mean(sm[i][both])), 3),
                              error_mean_d=round(2 * float(np.mean(sm[i][both] - mrolled[i][both])), 3),
                              coverage=round(float(both.mean()), 3)))
    model_cells = np.isfinite(mm).sum()
    summary = dict(stl=str(a.stl), model_height=round(float(zmax), 2), base_up=float(base), offset_mm=[float(cx), float(cy)],
                   turn_deg=float(sh * 360 / NT), cells_compared=int(ok.sum()),
                   coverage=round(float(ok.sum() / model_cells), 3),
                   radial_error_median=round(float(np.median(diff[ok])), 3),
                   radial_error_abs_median=round(float(np.median(np.abs(diff[ok]))), 3),
                   radial_error_abs_p90=round(float(np.percentile(np.abs(diff[ok]), 90)), 3),
                   radial_error_abs_p95=round(float(np.percentile(np.abs(diff[ok]), 95)), 3),
                   layer_mean_d_error_median=round(float(np.median([l['error_mean_d'] for l in layer])), 3),
                   scan_height=round(float(np.percentile(up, 99.9) - base), 2))
    a.out.write_text(json.dumps(dict(summary=summary, layers=layer), indent=2))
    np.save(a.out.with_suffix('.diff.npy'), diff)
    print(json.dumps(summary, indent=1))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 2, figsize=(14, 6), dpi=110)
    im = ax[0].imshow(diff, origin='lower', aspect='auto', cmap='coolwarm', vmin=-2, vmax=2,
                      extent=(0, 360, zs[0] - 0.5, zs[-1] + 0.5))
    fig.colorbar(im, ax=ax[0], label='scan - model radius, mm')
    ax[0].set_xlabel('angle about the axis, deg'); ax[0].set_ylabel('height, mm')
    ax[0].set_title('radial error map (white: not seen)')
    ax[1].plot([l['model_mean_r'] * 2 for l in layer], [l['z'] for l in layer], lw=2, label='STL mean diameter')
    ax[1].plot([l['scan_mean_r'] * 2 for l in layer], [l['z'] for l in layer], '.', label='scan mean diameter')
    ax[1].set_xlabel('mean diameter over angle, mm'); ax[1].legend(); ax[1].grid(alpha=.3)
    ax[1].set_title('layer mean diameter')
    fig.tight_layout()
    fig.savefig(a.out.with_suffix('.png'))


if __name__ == '__main__':
    main()
