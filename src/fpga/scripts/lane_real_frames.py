"""Direction-lane configurations on real camera frames (full-frame frontend RTL simulation).

Every configuration must produce identical records and geometry; only cycles may differ.
"""
import argparse
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
IVER = Path('D:/Tools/structured_light_iverilog/bin')
BOARD = Path('D:/Workspace/CodexWork/scan3d_r37_cdc_threshold_20261026_01/stageA13/boardprep21_session_board31/board')
SOURCES = ['rtl/shared_window_filter.sv', 'baseline_rtl/ridge_center_solver.sv', 'rtl/direction_refiner.sv', 'rtl/stripe_extractor.sv',
           'rtl/pixel_frontend.sv', 'vendor/lane_sweep/rtl/pfo3_tdp_ram.sv', 'vendor/lane_sweep/rtl/pfo3_banked_ram.sv',
           'tests/tb_frontend_lanes.sv']
# name: (lanes, dispatch, banked requests, banked results, balanced dispatch)
CONFIGS = {'2_static': (2, 0, 0, 0, 0), '4_balanced': (4, 1, 1, 1, 1), '8_static': (8, 0, 1, 1, 0)}
FRAMES = {  # wide/speckled stripe at ~0.35-0.43 m, then focused stripes at 0.65-0.7 m
    'near_a': 'r37_r41f_live03/raw_000240.gray8',
    'near_b': 'r37_r41g_live01/raw_000135.gray8',
    'near_c': 'r37_r41g_live02/raw_000150.gray8',
    'far_a': 'r37_r41f_live02/raw_000180.gray8',
    'far_b': 'r37_r41b_live07/raw_000300.gray8',
    'far_c': 'r37_r41c_live01/raw_000300.gray8',
}


def run(cmd, log):
    with open(log, 'w') as f:
        subprocess.run([str(c) for c in cmd], stdout=f, stderr=subprocess.STDOUT, check=True, timeout=3600)


def simulate(out, config, frame, binned):
    tag = f'{frame}_bin{binned}_{config}'
    log = out / f'{tag}.log'
    run([IVER / 'vvp.exe', out / f'{config}.vvp', f'+INPUT={out / (frame + ".hex")}', f'+BIN={binned}',
         f'+OUTPUT={out / (tag + ".records")}', f'+GEOMETRY={out / (tag + ".geometry")}'], log)
    text = log.read_text()
    if 'TEST_PASS' not in text:
        raise RuntimeError(f'{tag}: simulation failed')
    result = {k: int(v) for k, v in re.findall(r'(\w+)=(\d+)', re.search(r'RESULT (.+)', text).group(1))}
    return dict(frame=frame, binned=binned, config=config, **result)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('output', type=Path)
    ap.add_argument('--jobs', type=int, default=3)
    args = ap.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    for name, rel in FRAMES.items():
        raw = np.fromfile(BOARD / rel, np.uint8)
        (out / f'{name}.hex').write_text('\n'.join(f'{v:02x}' for v in raw) + '\n')
    for config, (lanes, dispatch, banked, results, balanced) in CONFIGS.items():
        params = [f'-Ptb_frontend_lanes.{k}={v}' for k, v in
                  (('LANES', lanes), ('DISPATCH', dispatch), ('BANKED_REQUESTS', banked),
                   ('BANKED_RESULTS', results), ('BALANCED_DISPATCH', balanced))]
        run([IVER / 'iverilog.exe', '-g2012', '-s', 'tb_frontend_lanes', *params, '-o', out / f'{config}.vvp',
             *[ROOT / s for s in SOURCES]], out / f'compile_{config}.log')
    jobs = [(c, f, b) for f in FRAMES for b in (0, 1) for c in CONFIGS]
    start = time.time()
    with ThreadPoolExecutor(args.jobs) as pool:
        rows = list(pool.map(lambda j: simulate(out, *j), jobs))
    for frame in FRAMES:
        for binned in (0, 1):
            ref = out / f'{frame}_bin{binned}_2_static'
            for config in CONFIGS:
                tag = out / f'{frame}_bin{binned}_{config}'
                for ext in ('.records', '.geometry'):
                    if Path(str(tag) + ext).read_bytes() != Path(str(ref) + ext).read_bytes():
                        raise RuntimeError(f'{tag.name}{ext} differs from 2_static')
    summary = {}
    for config in CONFIGS:
        cyc = {f'{r["frame"]}_bin{r["binned"]}': r['processing_cycles'] for r in rows if r['config'] == config}
        summary[config] = cyc
    gain = {c: float(np.median([summary['2_static'][k] / v for k, v in summary[c].items()])) for c in CONFIGS}
    report = dict(outputs_identical=True, wall_s=round(time.time() - start), cycles=summary,
                  median_speedup_vs_2_static=gain, rows=rows)
    (out / 'RESULT.json').write_text(json.dumps(report, indent=1), encoding='utf8')
    print(json.dumps(dict(cycles=summary, median_speedup_vs_2_static=gain), indent=1))


if __name__ == '__main__':
    main()
