"""section_measure.sv against model/section_measure_model.py, register by register.

Cases: synthetic edge cases plus every saved frame of the board runs used by
section_model_compare.py. Each case is one Icarus run; results go to evidence/section_rtl_verify.json.
"""
import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'model')]
import section_measure_model as sm
import section_model_compare as cmp
from r33.profile_live import SectionBasis

IVER = Path('D:/Tools/structured_light_iverilog/bin')
OUT = ROOT / 'tmp/secsim'
PLANE = json.loads((ROOT / 'runtime/calibration_r41/laser_plane_r41.json').read_text())['plane_coefficients']
P_REGS = [0x50, 0x54, 0x58, 0x5C, 0x60, 0x64, 0x68, 0x6C, 0x70, 0x74, 0x78, 0x7C, 0x9C, 0xA0, 0xA4, 0xA8]


def u32(v):
    return int(v) & 0xffffffff


def records_from_sh(basis, sh, flags=0x103):
    xyz = basis.origin + np.outer(sh[:, 0], basis.es) + np.outer(sh[:, 1], basis.eh)
    q = np.rint(xyz * 65536).astype(np.int64)
    rec = np.zeros((len(sh), 8), np.int64)
    rec[:, 0] = np.arange(len(sh))
    rec[:, 1:4] = q
    rec[:, 7] = flags
    return rec & 0xffffffff


def synthetic():
    """(name, basis, [records per packet]) edge cases."""
    b = SectionBasis(PLANE)
    rng = np.random.default_rng(7)
    s = np.arange(-40, 30, .25)
    step = np.column_stack((s, .1 * s + np.where(s > 5, 2.5, 0.) + .02 * rng.standard_normal(len(s))))
    gap = step[(step[:, 0] < -5) | (step[:, 0] > -2)]
    groove = np.column_stack((s, np.where((s > -5) & (s < -2), -1.5, 0.) + .01 * rng.standard_normal(len(s))))
    flat = np.column_stack((np.arange(0, 12.5, .25), np.zeros(50)))
    zig = np.column_stack((np.arange(0, 100, .25), np.repeat(np.arange(20) % 2 * 3., 20)))
    long = np.column_stack((np.arange(0, 1100) * .05, .001 * np.arange(1100)))
    one = np.array([[1., 2.]])
    cases = [('empty', [np.zeros((0, 8), np.int64)]), ('one_point', [records_from_sh(b, one)]),
             ('flat', [records_from_sh(b, flat)]), ('step', [records_from_sh(b, step)]),
             ('step_gap', [records_from_sh(b, gap)]), ('groove', [records_from_sh(b, groove)]),
             ('reversed', [records_from_sh(b, step[::-1])]), ('over_16_segments', [records_from_sh(b, zig)]),
             ('over_1024_points', [records_from_sh(b, long)]),
             ('sequence', [records_from_sh(b, step), records_from_sh(b, gap), records_from_sh(b, flat)])]
    rec = records_from_sh(b, step)
    rec[::3, 7] = 0x101                     # no preview bit: skipped in preview mode
    rec[1::5, 7] = 0x003                    # not valid
    cases.append(('flags', [rec]))
    return [(name, b, packets, None) for name, packets in cases]


def real_runs():
    out = []
    for name in cmp.RUNS:
        folder = cmp.m.BOARD / name
        cal_path = folder / 'calibration_live.json'
        status = json.loads((folder / 'status.json').read_text(encoding='utf8'))
        binned = bool(status.get('pfo3_binning'))
        params = cmp.m.geo.parameters(cmp.m.load_calibration(cal_path, admit_measured=True), 0.)
        basis = SectionBasis(json.loads(cal_path.read_text())['laser_plane_camera'])
        packets, zmax = [], []
        for raw in sorted(folder.glob('raw_*.gray8')):
            g = cmp.geometry(np.fromfile(raw, np.uint8).reshape(480, 752), binned, params)
            show = (g[:, 7] & 0x102) == 0x102
            z = g[show, 3].astype(np.uint32).view(np.int32) / 65536.
            zmax.append(sm.I32_MAX if not len(z) else int(np.floor((np.percentile(z, 5) + 250) * 65536)))
            packets.append(g)
        out.append((name, basis, packets, zmax))
    return out


def script_and_expect(basis, packets, zmax):
    cfg = sm.config_from_basis(basis)
    model = sm.SectionMeasure(cfg)
    lines = ['W 04 00000003']
    for addr, v in zip(range(0x08, 0x2C, 4), cfg.origin + cfg.es + cfg.eh):
        lines.append(f'W {addr:02x} {u32(v):08x}')
    for addr, v in ((0x2C, cfg.z_min), (0x34, cfg.jump), (0x38, cfg.brk), (0x3C, cfg.level), (0x40, cfg.min_points)):
        lines.append(f'W {addr:02x} {u32(v):08x}')
    expect, dropped = [], 0
    for k, rec in enumerate(packets):
        cfg.z_max = sm.I32_MAX if zmax is None else zmax[k]
        lines.append(f'W 30 {u32(cfg.z_max):08x}')
        lines.append('D')
        lines += ['R ' + ' '.join(f'{u32(w):08x}' for w in r) for r in rec]
        lines.append('C')
        out = model.packet(rec)
        dropped += out['dropped']
        expect.append(encode(out, k + 1, dropped))
    return '\n'.join(lines) + '\n', expect


def encode(out, packet, dropped):
    segs = out['seg']
    status = ((out['step'] is not None) | (out['gap'] is not None) << 1 | (out['depth'] is not None) << 2
              | bool(segs) << 3 | (out['reference'] or 0) << 8 | (out['step_segment'] or 0) << 12
              | len(out['steps']) << 16)
    edges = out['gap_edges'] or (0, 0)
    regs = [packet, out['points'], out['overflow'] << 31 | len(segs), status, out['step'] or 0, out['gap'] or 0,
            out['depth'] or 0, out['noise'], out['level'], out['ref_rms'] or 0, out['ref_slope'] or 0,
            out['spacing'], dropped, edges[0], edges[1]]
    steps = [(u32(a), u32(o)) for a, o in out['steps']]
    seg = [(g.n, u32(g.s0), u32(g.s1), u32(out['offsets'][i]), u32(g.fit['a'])) for i, g in enumerate(segs)]
    return {'regs': [u32(v) for v in regs], 'steps': steps, 'seg': seg}


def simulate(name, script):
    folder = OUT / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'in.txt').write_text(script)
    vvp = folder / 'tb.vvp'
    subprocess.run([IVER / 'iverilog.exe', '-g2012', f'-DSCRIPT="{(folder / "in.txt").as_posix()}"',
                    f'-DRESULT="{(folder / "out.txt").as_posix()}"', '-s', 'tb_section_measure', '-o', vvp,
                    ROOT / 'rtl/section_measure.sv', ROOT / 'tests/tb_section_measure.sv'], check=True)
    log = subprocess.run([IVER / 'vvp.exe', '-n', vvp], check=True, capture_output=True, text=True).stdout
    got, cur = [], None
    for line in (folder / 'out.txt').read_text().split('\n'):
        f = line.split()
        if f and f[0] == 'P':
            cur = {'regs': [int(v, 16) if 'x' not in v else -1 for v in f[1:]], 'table': []}
            got.append(cur)
        elif f and f[0] == 'T':
            cur['table'].append([int(v, 16) if 'x' not in v else -1 for v in f[2:]])
    return got, log


def check(name, basis, packets, zmax):
    script, expect = script_and_expect(basis, packets, zmax)
    got, log = simulate(name, script)
    bad, cycles = [], []
    if len(got) != len(expect):
        return {'case': name, 'packets': len(expect), 'simulated': len(got), 'mismatches': ['packet count'], 'log': log[-500:]}
    for k, (g, e) in enumerate(zip(got, expect)):
        cycles.append(g['regs'][15])
        for i, (gv, ev) in enumerate(zip(g['regs'][:15], e['regs'])):
            if gv != ev:
                bad.append(f'packet {k} reg {P_REGS[i]:#x}: rtl {gv:#010x} model {ev:#010x}')
        for j, (a, o) in enumerate(e['steps']):
            if g['table'][j][:2] != [a, o]:
                bad.append(f'packet {k} step {j}: rtl {g["table"][j][:2]} model {[a, o]}')
        for j, row in enumerate(e['seg']):
            if g['table'][j][2:] != list(row):
                bad.append(f'packet {k} seg {j}: rtl {g["table"][j][2:]} model {list(row)}')
    return {'case': name, 'packets': len(expect), 'simulated': len(got), 'mismatches': bad[:20], 'mismatch_count': len(bad),
                'max_solve_cycles': max(cycles) if cycles else 0}


def main():
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--synthetic', action='store_true')
    parser.add_argument('--work-dir', type=Path, default=OUT)
    parser.add_argument('--report', type=Path, default=ROOT / 'evidence/section_rtl_verify.json')
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    OUT = args.work_dir.resolve()
    if OUT.exists() or args.report.exists():
        parser.error('work directory and report must be new paths; historical evidence is not overwritten')
    OUT.mkdir(parents=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    cases = synthetic() + ([] if args.synthetic else real_runs())
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda c: check(*c), cases))
    ok = all(r['mismatch_count'] == 0 and r['packets'] == r['simulated'] for r in results if 'mismatch_count' in r) \
        and all('mismatch_count' in r for r in results)
    report = {'passed': ok, 'cases': results, 'packets': sum(r['packets'] for r in results)}
    args.report.write_text(json.dumps(report, indent=1))
    for r in results:
        print(r['case'], r['packets'], 'mismatches', r.get('mismatch_count', r.get('mismatches')),
              'max cycles', r.get('max_solve_cycles'), *(r['mismatches'][:3]))
    print('PASS' if ok else 'FAIL', report['packets'], 'packets')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
