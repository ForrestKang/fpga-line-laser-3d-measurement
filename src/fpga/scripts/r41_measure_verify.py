"""R41 measurement unit verification: rtl/r41_measure.sv (Icarus) against an independent model.

Random geometry records (valid/invalid, measurement/preview flags, points below/inside/above the
Z histogram and outside the height bands) over many packets with commits, discards, an empty
packet and configuration changes between packets. After every commit all result registers and the
32 band entries must equal the model (band max/min only where the band count is non-zero).
"""
import json
import random
import subprocess
import sys
from pathlib import Path

R40 = Path(__file__).resolve().parents[1]
IVER = Path('D:/Tools/structured_light_iverilog/bin')
M32 = 0xffffffff


def s32(v):
    v &= M32
    return v - (1 << 32) if v & 0x80000000 else v


class Model:
    def __init__(self):
        self.enable, self.preview = 0, 0
        self.z_origin, self.z_shift, self.h_origin, self.h_shift = 0, 18, 0, 18
        self.seq = 0
        self.points = []
        self.bands = [dict(n=0, mx=0, mn=0, sm=0) for _ in range(32)]
        self.result = None

    def write(self, a, d):
        if a == 0x04:
            self.enable, self.preview = d & 1, (d >> 1) & 1
        elif a == 0x08:
            self.z_origin = s32(d)
        elif a == 0x0C:
            self.z_shift = d & 31
        elif a == 0x10:
            self.h_origin = s32(d)
        elif a == 0x14:
            self.h_shift = d & 31

    def point(self, w):
        f = w[7]
        if self.enable and f & 0x100 and (f & (2 if self.preview else 1)):
            self.points.append((s32(w[1]), s32(w[2]), s32(w[3]), s32(w[4]), s32(w[5]), s32(w[6]),
                                self.z_origin, self.z_shift, self.h_origin, self.h_shift))

    def commit(self):
        n = len(self.points)
        below = above = hout = 0
        hist = [0] * 256
        bands = [dict(n=0, mx=None, mn=None, sm=0) for _ in range(32)]
        for cx, cy, cz, ax, ay, az, zo, zs, ho, hs in self.points:
            dz, dh = cz - zo, az - ho
            if dz < 0:
                below += 1
            elif dz >> zs > 255:
                above += 1
            else:
                hist[dz >> zs] += 1
            r2 = (ax * ax + ay * ay) & ((1 << 64) - 1)
            if dh < 0 or dh >> hs > 31:
                hout += 1
            else:
                b = bands[dh >> hs]
                b['n'] += 1
                b['sm'] = (b['sm'] + r2) & ((1 << 64) - 1)
                b['mx'] = r2 if b['mx'] is None else max(b['mx'], r2)
                b['mn'] = r2 if b['mn'] is None else min(b['mn'], r2)
        m = (n - below - above) & 0xffff
        median, valid = 0, 0
        if m:
            target, cum = ((m - 1) & 0xffff) >> 1, 0
            for i, c in enumerate(hist):
                if cum + c > target:
                    median, valid = i, 1
                    break
                cum += c
        ext = lambda k, f, empty: f(p[k] for p in self.points) if self.points else empty
        zsum = sum(p[2] for p in self.points) & ((1 << 48) - 1)
        zsum_hi = zsum >> 32
        zsum_hi = zsum_hi | 0xffff0000 if zsum_hi & 0x8000 else zsum_hi
        self.seq += 1
        regs = [self.seq, n, ext(2, min, 0x7fffffff) & M32, ext(2, max, -0x80000000) & M32,
                zsum & M32, zsum_hi, (valid << 31) | median, m, below, above,
                ext(0, min, 0x7fffffff) & M32, ext(0, max, -0x80000000) & M32,
                ext(1, min, 0x7fffffff) & M32, ext(1, max, -0x80000000) & M32,
                ext(5, min, 0x7fffffff) & M32, ext(5, max, -0x80000000) & M32]
        self.points = []
        return regs, hout, bands


def build_events(seed=41):
    rnd = random.Random(seed)
    ev, model, expected = [], Model(), []

    def wr(a, d):
        ev.append([4, a, d & M32] + [0] * 6)
        model.write(a, d & M32)

    wr(0x08, 200 * 65536); wr(0x0C, 18); wr(0x10, -60 * 65536); wr(0x14, 18); wr(0x04, 1)
    configs = [(1, 200, 18, -60, 18), (3, 150, 17, -40, 17), (1, 0, 20, 0, 16), (3, 300, 16, -100, 19)]
    for packet in range(14):
        if packet % 4 == 0:
            en, zo, zs, ho, hs = configs[(packet // 4) % len(configs)]
            wr(0x04, en); wr(0x08, zo * 65536); wr(0x0C, zs); wr(0x10, ho * 65536); wr(0x14, hs)
        count = 0 if packet == 5 else rnd.randint(50, 900)
        for _ in range(count):
            # packets 7 and 12: Z clustered in a few histogram bins, so consecutive points hit the
            # same bin (exercises the b13 block-RAM read-modify-write forwarding)
            lo, hi = (200, 211) if packet in (7, 12) else (120, 1700)
            cz = rnd.randint(lo, hi) * 65536 + rnd.randint(0, 65535)
            words = [rnd.getrandbits(32), rnd.randint(-300 * 65536, 300 * 65536) & M32,
                     rnd.randint(-300 * 65536, 300 * 65536) & M32, cz & M32,
                     rnd.randint(-120 * 65536, 120 * 65536) & M32, rnd.randint(-120 * 65536, 120 * 65536) & M32,
                     rnd.randint(-150 * 65536, 250 * 65536) & M32,
                     rnd.choice([0x101, 0x103, 0x102, 0x100, 0x001, 0x50000 | 0x3])]
            ev.append([1] + words)
            model.point(words)
        if packet in (3, 9):
            ev.append([3] + [0] * 8)
            model.points = []
        else:
            ev.append([2] + [0] * 8)
            expected.append(model.commit())
    ev.append([0] * 9)
    return ev, expected


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    ev, expected = build_events()
    (out / 'events.hex').write_text('\n'.join(' '.join('%08x' % (w & M32) for w in e) for e in ev) + '\n')
    vvp = out / 'sim.vvp'
    subprocess.run([str(IVER / 'iverilog.exe'), '-g2012', '-o', str(vvp), str(R40 / 'rtl/r41_measure.sv'),
                    str(R40 / 'tests/tb_r41_measure.sv')], check=True)
    gap = sys.argv[2] if len(sys.argv) > 2 else '5'       # idle cycles after each point (0: back-to-back)
    sim = subprocess.run([str(IVER / 'vvp.exe'), '-n', str(vvp), '+EVENTS=' + str(out / 'events.hex').replace('\\', '/'),
                          '+GAP=' + gap], capture_output=True, text=True)
    (out / 'sim.log').write_text(sim.stdout + sim.stderr, encoding='utf8')
    lines = sim.stdout.splitlines()
    dumps, cur = [], None
    for l in lines:
        if l.startswith('R '):
            cur = dict(regs=list(map(int, l.split()[1:])), bands=[])
            dumps.append(cur)
        elif l.startswith('B '):
            cur['bands'].append([int(t) if t.lstrip('-').isdigit() else -1 for t in l.split()[1:]])
    mism = []
    for k, ((regs, hout, bands), d) in enumerate(zip(expected, dumps)):
        got_regs = d['regs'][:16]
        if got_regs != regs:
            mism.append(dict(packet=k, what='regs', got=got_regs, want=regs))
        if d['regs'][16] != 0 or d['regs'][17] != hout:   # 0x60 dropped, 0x80 outside bands
            mism.append(dict(packet=k, what='dropped/hout', got=d['regs'][16:], want=[0, hout]))
        for b, row in enumerate(d['bands']):
            want = bands[b]
            got = dict(n=row[1], mx=row[2] | row[3] << 32, mn=row[4] | row[5] << 32, sm=row[6] | row[7] << 32)
            if got['n'] != want['n'] or got['sm'] != want['sm'] or (
                    want['n'] and (got['mx'] != want['mx'] or got['mn'] != want['mn'])):
                mism.append(dict(packet=k, what='band', band=b, got=got, want=want))
    ok = ('TEST_DONE' in sim.stdout and 'ERROR' not in sim.stdout and len(dumps) == len(expected)
          and not mism and 'DROPPED 0' in sim.stdout)
    report = dict(schema='R41_MEASURE_UNIT_V1', passed=ok, gap_cycles=int(gap), packets_committed=len(expected), dumps=len(dumps),
                  mismatches=mism[:10], mismatch_count=len(mism),
                  example=dict(n=expected[0][0][1], median_bin=expected[0][0][6] & 255,
                               bands_used=sum(1 for b in expected[0][2] if b['n'])))
    (out / 'RESULT.json').write_text(json.dumps(report, indent=1), encoding='utf8')
    print(json.dumps(report, indent=1)[:3000])
    print('R41_MEASURE_PASS' if ok else 'R41_MEASURE_FAIL')


if __name__ == '__main__':
    main()
