"""R41 laser AE unit verification: rtl/r41_laser_ae.sv (Icarus) against an independent model.

For every scenario the model renders the same frames from the exposure the testbench reports
(sensor_rows), recomputes the saturated count and peak, and replays the control rule, settle
window, clamps, SCCB failures and disable/load behaviour. Pass = every per-frame line equal.
"""
import json
import subprocess
import sys
from pathlib import Path

R40 = Path(__file__).resolve().parents[1]
IVER = Path('D:/Tools/structured_light_iverilog/bin')
BEATS = 376

SCENARIOS = {
    'bright_start_480': dict(ROWS0=480, LASERK=256, BGK=20, LASERPX=150),
    'dim_start_16': dict(ROWS0=16, LASERK=256, BGK=20, LASERPX=150),
    'dark_scene_clamps_max': dict(ROWS0=100, LASERK=40, BGK=5, LASERPX=150, MAXR=300),
    'glare_clamps_min': dict(ROWS0=60, LASERK=4000, BGK=900, LASERPX=150, MINR=24),
    'sccb_failures': dict(ROWS0=480, LASERK=256, BGK=20, LASERPX=150, FAILMASK=0b101),
    'settle_0': dict(ROWS0=480, LASERK=256, BGK=20, LASERPX=150, SETTLE=0),
    'disable_midway': dict(ROWS0=480, LASERK=256, BGK=20, LASERPX=150, DISABLE_AT=3),
}
DEFAULTS = dict(FRAMES=40, ROWS0=480, MINR=16, MAXR=480, SATLIM=200, TLOW=200, SETTLE=2,
                LASERK=256, BGK=20, LASERPX=150, FAILMASK=0, DISABLE_AT=1 << 30)


def pix(r, idx, c):
    col = idx % 752
    if col < c['LASERPX']:
        v = (c['LASERK'] * r) >> 8
    elif col < c['LASERPX'] + 8:
        v = (c['LASERK'] * r) >> 9
    else:
        v = (c['BGK'] * r) >> 8
    return min(v, 255)


def stats(r, c):
    vals = [pix(r, i, c) for i in range(BEATS * 4)]
    return sum(v >= 250 for v in vals), max(vals)


def model(c, sensor_rows_seq):
    rows, settle, updates, errors, ordinal = c['ROWS0'], 0, 0, 0, 0
    out = []
    for f, srows in enumerate(sensor_rows_seq):
        enabled = f < c['DISABLE_AT']
        sat, peak = stats(srows, c)
        if settle:
            settle -= 1
        elif enabled:
            if sat > c['SATLIM']:
                step = max(1, rows >> 2)
                nr = max(c['MINR'], rows - step if rows > step else 1)
            elif peak < c['TLOW']:
                nr = min(c['MAXR'], rows + max(1, rows >> 3))
            else:
                nr = rows
            if nr != rows:
                if (c['FAILMASK'] >> ordinal) & 1:
                    errors += 1
                else:
                    rows, updates, settle = nr, updates + 1, c['SETTLE']
                ordinal += 1
        out.append((f, srows, sat, peak, rows, updates, errors))
    return out


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    vvp = out / 'sim.vvp'
    subprocess.run([str(IVER / 'iverilog.exe'), '-g2012', '-o', str(vvp), str(R40 / 'rtl/r41_laser_ae.sv'),
                    str(R40 / 'tests/tb_r41_laser_ae.sv')], check=True)
    results, ok = {}, True
    for name, over in SCENARIOS.items():
        c = dict(DEFAULTS, **over)
        sim = subprocess.run([str(IVER / 'vvp.exe'), '-n', str(vvp)] + ['+%s=%d' % kv for kv in c.items()],
                             capture_output=True, text=True)
        (out / (name + '.log')).write_text(sim.stdout + sim.stderr, encoding='utf8')
        lines = [tuple(map(int, l.split()[1:])) for l in sim.stdout.splitlines() if l.startswith('F ')]
        load = next((l.split()[1:] for l in sim.stdout.splitlines() if l.startswith('L ')), None)
        expect = model(c, [l[1] for l in lines])
        first_diff = next((i for i, (a, b) in enumerate(zip(lines, expect)) if a != b), None)
        disabled = c['DISABLE_AT'] < c['FRAMES']
        load_ok = load is not None and (int(load[1]) == 123) == disabled
        passed = ('TEST_DONE' in sim.stdout and 'ERROR' not in sim.stdout and len(lines) == c['FRAMES']
                  and first_diff is None and load_ok)
        ok &= passed
        final = lines[-1] if lines else None
        results[name] = dict(passed=passed, frames=len(lines), first_diff=first_diff,
                             final_sensor_rows=final[1] if final else None, final_sat=final[2] if final else None,
                             final_peak=final[3] if final else None, updates=final[5] if final else None,
                             errors=final[6] if final else None, load_after=load,
                             trajectory=[l[1] for l in lines[:16]])
    report = dict(schema='R41_AE_UNIT_V1', passed=ok, scenarios=results)
    (out / 'RESULT.json').write_text(json.dumps(report, indent=1), encoding='utf8')
    print(json.dumps({k: {kk: v[kk] for kk in ('passed', 'final_sensor_rows', 'final_sat', 'final_peak', 'updates',
                                                'errors', 'trajectory')} for k, v in results.items()}, indent=1))
    print('R41_AE_PASS' if ok else 'R41_AE_FAIL')


if __name__ == '__main__':
    main()
