"""Fixed-point section model vs the float measure_frame on saved board frames.

Geometry records come from the reference detector in PL emission (patch) order, so both
sides see exactly the points the PL would produce. Writes evidence/section_model_compare.json.
"""
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'model')]
import render_ui_mockups as m  # noqa: E402
import section_measure_model as sm  # noqa: E402
from r33.profile_live import ProfileConfig, SectionBasis, measure_frame  # noqa: E402

RUNS = ['r37_r41b_live01', 'r37_r41b_live05', 'r37_r41b_live06', 'r37_r41b_live07', 'r37_r41c_live01',
        'r37_r41f_live02', 'r37_r41f_live03', 'r37_r41g_live01', 'r37_r41g_live02']
Q = 65536.0


def geometry(image, binned, params):
    recs, patches, _ = m.detect(image, binned)
    diag = {r[0]: r for r in recs}
    g = []
    for p in patches:
        modes = (p[13] >> 25 & 1) | ((p[13] >> 26 & 1) << 1)
        if modes:
            r = diag[p[0]]
            g.append(m.geo.reconstruct(params, r[4], r[5], r[0], modes))
    return np.asarray(g, np.int64).reshape(-1, 8) & 0xffffffff


def mm(v):
    return None if v is None else v / Q


def run(name):
    folder = m.BOARD / name
    cal_path = folder / 'calibration_live.json'
    status = json.loads((folder / 'status.json').read_text(encoding='utf8'))
    binned = bool(status.get('pfo3_binning'))
    params = m.geo.parameters(m.load_calibration(cal_path, admit_measured=True), 0.)
    basis, cfg = SectionBasis(json.loads(cal_path.read_text())['laser_plane_camera']), ProfileConfig()
    pl = sm.SectionMeasure(sm.config_from_basis(basis))
    rows = []
    for raw in sorted(folder.glob('raw_*.gray8')):
        g = geometry(np.fromfile(raw, np.uint8).reshape(480, 752), binned, params)
        show = (g[:, 7] & 0x102) == 0x102
        xyz = g[show, 1:4].astype(np.uint32).view(np.int32).astype(float) / Q
        f = measure_frame(xyz, basis, cfg)
        zmax = f['window']['z_max_auto_mm']
        pl.cfg.z_max = sm.I32_MAX if zmax is None else int(np.floor(zmax * Q))
        p = pl.packet(g)
        rows.append(dict(run=name, frame=raw.stem, points=(f['points'], p['points']),
                         segments=(f['segments'], p['segments']), noise=(f['noise_mm'], mm(p['noise'])),
                         step=(f['step_mm'], mm(p['step'])), gap=(f['gap_mm'], mm(p['gap'])),
                         steps=(len(f['steps']), len(p['steps']))))
    return rows


def stats(v):
    v = np.abs(np.asarray(v, float))
    return dict(n=len(v), median=float(np.median(v)), p95=float(np.percentile(v, 95)), max=float(v.max())) \
        if len(v) else dict(n=0)


def main():
    with ProcessPoolExecutor(4) as pool:
        rows = [r for part in pool.map(run, RUNS) for r in part]
    both = [r for r in rows if None not in r['step']]
    step_diff = [r['step'][1] - r['step'][0] for r in both]
    half_noise = [abs(r['step'][1] - r['step'][0]) <= .5 * max(r['noise'][0], 1e-9) for r in both]
    gaps = [r for r in rows if None not in r['gap']]
    result = dict(
        frames=len(rows), points_equal=all(r['points'][0] == r['points'][1] for r in rows),
        step_valid_agree=sum((r['step'][0] is None) == (r['step'][1] is None) for r in rows),
        step_both_valid=len(both), step_abs_diff_mm=stats(step_diff),
        step_within_half_noise=sum(half_noise),
        step_rel_diff=stats([(r['step'][1] - r['step'][0]) / r['step'][0] for r in both]),
        gap_valid_agree=sum((r['gap'][0] is None) == (r['gap'][1] is None) for r in rows),
        gap_both_valid=len(gaps), gap_abs_diff_mm=stats([r['gap'][1] - r['gap'][0] for r in gaps]),
        segments_equal=sum(r['segments'][0] == r['segments'][1] for r in rows),
        noise_ratio=stats([r['noise'][1] / r['noise'][0] for r in rows if r['noise'][0]]),
        rows=rows)
    out = ROOT / 'evidence/section_model_compare.json'
    out.write_text(json.dumps(result, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != 'rows'}, indent=1))


if __name__ == '__main__':
    main()
