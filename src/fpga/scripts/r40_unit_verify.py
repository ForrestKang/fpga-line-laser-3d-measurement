"""R40 unit verification: point_cloud_renderer (Icarus) against model/point_cloud_model.py.

Point stream: every frame of a verified board archive (axis XYZ Q16, one packet per
frame) plus synthetic packets for each ingest rule:
  frame 10  a discarded packet (must leave no voxel)
  frame 20  2100 in-range points in one packet (staging overflow: 52 dropped)
  frame 30  preview-only and invalid-flag records (ignored)
  frame 40  out-of-range records (counted, not inserted)
  frame 50  the real frame's points ended by an error (discarded)
Pass requires bit equality of: bypass video frame, voxel grid, ingest counters,
fixed-yaw panel, overlaid video frame and an auto-rotated panel.
"""
import json
import re
import os
import random
import subprocess
import sys
from pathlib import Path

import numpy as np

R40 = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(R40 / 'model'), str(R40 / 'runtime'),
                'D:/Workspace/CodexWork/scan3d_r39_row_overflow_20261027_01/tools']
import point_cloud_model as M  # noqa: E402

IVER = Path('D:/Tools/structured_light_iverilog/bin')
ARCHIVE = Path('D:/Workspace/CodexWork/scan3d_r37_cdc_threshold_20261026_01/stageA13/'
               'boardprep21_session_board31/board/r37_r39_f1_240')
W, H = 464, 496
PARAMS = dict(ctrl=0x3, inv=109227, ox=-1258291, oy=-1258291, oz=-65536,
              yaw_fixed=147, yaw_step=300, k=6554, kse=2561, kce=6033,
              sx0=112, sy0=118, zc=82, wx=8, wy=8, splat=3)


def frames_q16():
    os.environ.setdefault('R33_NATIVE_ASSEMBLER',
                          'D:/Workspace/CodexWork/scan3d_r39_row_overflow_20261027_01/build/native_host/assemble_r33_r39.dll')
    from test_v2_equivalence import archived_frames
    _, frames = archived_frames(ARCHIVE / 'storage')
    out = []
    for _, blobs in frames:
        xyz = np.frombuffer(blobs['measurement_xyz'], '<f8').reshape(-1, 3) * 65536
        if not np.all(xyz == np.round(xyz)):
            raise SystemExit('archived XYZ is not exact Q16')
        out.append(xyz.astype(np.int64))
    return out


def u32(v):
    return int(v) & 0xffffffff


def build_events(frames):
    rnd = random.Random(40)
    ev = []

    def inrange():
        return [rnd.randint(-18 * 65536, 18 * 65536), rnd.randint(-18 * 65536, 18 * 65536),
                rnd.randint(0, 47 * 65536)]

    for f, pts in enumerate(frames):
        if f == 10:
            ev += [(1, *inrange(), 0x101) for _ in range(40)] + [(3, 0, 0, 0, 0)]
        if f == 20:
            ev += [(1, *inrange(), 0x101) for _ in range(2100)] + [(2, 0, 0, 0, 0)]
        if f == 30:
            ev += [(1, *inrange(), 0x102) for _ in range(20)] + [(1, *inrange(), 0x001) for _ in range(20)]
        if f == 40:
            ev += [(1, 100 * 65536, 0, 0, 0x101), (1, 0, -30 * 65536, 0, 0x101), (1, 0, 0, 70 * 65536, 0x101),
                   (1, 0, 0, -5 * 65536, 0x101)]
        ev += [(1, int(x), int(y), int(z), 0x101) for x, y, z in pts]
        ev.append((3 if f == 50 else 2, 0, 0, 0, 0))
    return ev


def model_ingest(ev):
    ing = M.Ingest(PARAMS)
    for t, x, y, z, flags in ev:
        if t == 1:
            ing.point([0, 0, 0, 0, u32(x), u32(y), u32(z), flags])
        else:
            ing.end_packet(t == 2)
    return ing


def write_inputs(out, ev):
    with (out / 'events.hex').open('w') as f:
        for e in ev:
            f.write(' '.join('%08x' % u32(v) for v in e) + '\n')
        f.write('00000000 00000000 00000000 00000000 00000000\n')
    p = PARAMS
    regs = [(0x04, p['inv']), (0x08, p['ox']), (0x0c, p['oy']), (0x10, p['oz']), (0x18, p['k']),
            (0x1c, p['kse']), (0x20, p['kce']), (0x24, (p['sy0'] << 16) | p['sx0']), (0x28, p['zc']),
            (0x2c, (p['wy'] << 16) | p['wx']), (0x30, p['splat']), (0x00, 0x0a)]
    with (out / 'regs.hex').open('w') as f:
        for a, v in regs:
            f.write('%08x %08x\n' % (a, u32(v)))
        f.write('000000ff 00000000\n')


def read_hex(path):
    return np.array([int(x, 16) for x in path.read_text().split()], np.int64)


def pattern():
    y, x = np.mgrid[0:H, 0:W]
    return (0x5a000000 | ((x & 255) << 16) | ((y & 255) << 8) | ((x ^ y) & 255)).astype(np.int64)


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    tables = R40 / 'rtl' / 'point_cloud_tables.sv'
    if not tables.exists():
        tables.write_text(M.tables_sv(), encoding='ascii')
    if tables.read_text(encoding='ascii') != M.tables_sv():
        raise SystemExit('rtl/point_cloud_tables.sv differs from the model tables')
    frames = frames_q16()
    ev = build_events(frames)
    ing = model_ingest(ev)
    write_inputs(out, ev)
    srcs = [R40 / 'rtl' / n for n in ('point_cloud_renderer.sv', 'point_cloud_memory.sv', 'point_cloud_tables.sv')]
    srcs += [R40 / 'tests' / 'xpm_stubs_sim.v', R40 / 'tests' / 'tb_r40_view.sv']
    subprocess.run([str(IVER / 'iverilog.exe'), '-g2012', '-o', str(out / 'sim.vvp'), *map(str, srcs)],
                   check=True, cwd=out)
    log = out / 'sim.log'
    with log.open('w') as f:
        subprocess.run([str(IVER / 'vvp.exe'), '-n', str(out / 'sim.vvp'),
                        '+EVENTS=' + str(out / 'events.hex').replace('\\', '/'),
                        '+REGS=' + str(out / 'regs.hex').replace('\\', '/'),
                        '+YAW1=%d' % PARAMS['yaw_fixed'], '+STEP2=%d' % PARAMS['yaw_step']],
                       check=True, cwd=out, stdout=f, stderr=subprocess.STDOUT)
    text = log.read_text()
    if 'TEST_DONE' not in text or 'ERROR' in text:
        raise SystemExit('simulation failed: ' + text[-2000:])
    # Board bug b04: the first publish landed while the video side was in reset and no swap came.
    # The TB now holds the video reset past the first render; the retry must recover it.
    rel = re.search(r'PHASE video reset released after (\d+) cycles, render_count (\d+) republish (\d+)', text)
    rep = re.search(r'PHASE republish_count (\d+)', text)
    late_video = dict(released_after_cycles=int(rel.group(1)), renders_before_release=int(rel.group(2)),
                      republish_count=int(rep.group(1)))
    words = ing.grid.words.astype(np.int64)
    pat = pattern()
    status = read_hex(out / 'out_status.hex')
    panel = M.render(ing.grid.words, PARAMS, PARAMS['yaw_fixed'])
    p2 = read_hex(out / 'out_panel2.hex')
    yaw2 = int(p2[0])
    checks = dict(
        bypass=np.array_equal(read_hex(out / 'out_bypass.hex').reshape(H, W), pat),
        voxels=np.array_equal(read_hex(out / 'out_voxels.hex'), words),
        counters=[int(v) for v in status[:6]] == [ing.inserted, ing.dropped, ing.frames_committed,
                                                    ing.frames_discarded, ing.grid.occupied, ing.out_of_range],
        magic=int(status[10]) == M.MAGIC,
        panel_fixed_yaw=np.array_equal(read_hex(out / 'out_panel.hex').reshape(M.PH, M.PW), panel),
        overlay_frame=np.array_equal(read_hex(out / 'out_final.hex').reshape(H, W),
                                     M.overlay(pat, panel, PARAMS, True)),
        panel_auto_rotate=np.array_equal(p2[1:].reshape(M.PH, M.PW), M.render(ing.grid.words, PARAMS, yaw2)),
        late_video_reset_recovered=late_video['renders_before_release'] >= 1 and late_video['republish_count'] >= 1)
    report = dict(schema='R40_UNIT_VERIFY_V1', passed=all(checks.values()), checks=checks,
                  events=len(ev), frames=len(frames),
                  model=dict(inserted=ing.inserted, dropped=ing.dropped, frames_committed=ing.frames_committed,
                             frames_discarded=ing.frames_discarded, occupied=ing.grid.occupied,
                             out_of_range=ing.out_of_range, panel_nonzero=int(np.count_nonzero(panel))),
                  rtl_status=[int(v) for v in status], auto_rotate_yaw=yaw2,
                  last_render_cycles=int(status[7]), late_video=late_video, params=PARAMS)
    (out / 'RESULT.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    print(json.dumps({k: report[k] for k in ('passed', 'checks', 'model', 'last_render_cycles', 'auto_rotate_yaw')}, indent=1))
    print('R40_UNIT_PASS' if report['passed'] else 'R40_UNIT_FAIL')


if __name__ == '__main__':
    main()
