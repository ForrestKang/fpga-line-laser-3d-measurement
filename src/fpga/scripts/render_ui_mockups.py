"""Render the three HDMI pages (normal + alert variant each) from archived real data, on the PC.

Live/scan: C1 vase replay (simulated frames through the real PL; PL panel and cloud of that run)
with trends from board run r37_r41c_live01. Section: board run r37_r41f_live02 (0.65 m) and an
alert frame from r37_r41g_live02 (~0.4 m), both with 2x binning.
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
RT = ROOT / 'runtime'
sys.path[:0] = [str(RT), str(RT / 'structured_light_system_geometry_v1/src/python'), str(ROOT / 'reference')]
import geometry_reference as geo  # noqa: E402
import reference_r39 as ref  # noqa: E402
import reference_r41_bin2 as ref_bin  # noqa: E402
from r33 import ui_theme as th  # noqa: E402
from r33.geometry import load_calibration  # noqa: E402
from r33.page_state import section_view  # noqa: E402
from r33.profile_live import (  # noqa: E402
    ProfileConfig,
    ProfileTracker,
    SectionBasis,
    assess_quality,
    measure_frame,
    point_rows_from,
)
from r33.ui_pages import PageRenderer  # noqa: E402

CW = Path('D:/Workspace/CodexWork')
BOARD = CW / 'scan3d_r37_cdc_threshold_20261026_01/stageA13/boardprep21_session_board31/board'
VASE_RAW = CW / 'scan3d_F1_C1_sim_scan_20261026_01/runs/C1_RIPPLE_TWIST_VASE_240_run01/observations/raw.gray8'
VASE_RUN = BOARD / 'r37_r40e_c1_hold01'
FONTS = th.Fonts()          # the subset fonts shipped to the board
DEVICES = [('相机', '正常', th.OK), ('FPGA', '已加载', th.OK), ('激光', '手动', th.DIM), ('电机', '停止', th.DIM)]


def detect(image, binned):
    img = ref_bin.bin2(image) if binned else image
    recs, rows = ref.candidates(img)
    patches = ref.graph_patches(recs, rows)
    final = ref.final_rows(img, recs, rows, patches)
    return (ref_bin.to_full(recs) if binned else recs), patches, np.asarray(final, np.int64).astype(np.uint32)


def centres(recs, patches):
    ok = {p[0] for p in patches if p[13] >> 26 & 1}
    return np.array([(r[1], (r[4] + 2048) >> 12) for r in recs if r[0] in ok], int).reshape(-1, 2)


def frame_profile(image, binned, params, basis, cfg):
    recs, patches, rows = detect(image, binned)
    diag = {r[0]: r for r in recs}
    g = []
    for p in patches:
        modes = (p[13] >> 25 & 1) | ((p[13] >> 26 & 1) << 1)
        if modes:
            r = diag[p[0]]
            g.append(geo.reconstruct(params, r[4], r[5], r[0], modes))
    g = np.asarray(g, np.int64).reshape(-1, 8) & 0xffffffff
    show = (g[:, 7] & 0x102) == 0x102
    xyz = g[show, 1:4].astype(np.uint32).view(np.int32).astype(float) / 65536
    rec2d = np.asarray(recs, np.int64).astype(np.uint32).reshape(-1, 16)
    f = measure_frame(xyz, basis, cfg, point_rows_from(rec2d, g[show, 0]), rows)
    q = assess_quality(rows, f['points'], row_window=f['window'].get('rows'), width_scale=2 if binned else 1)
    return f, q, recs, patches


def section_state(run, raw_names, binned):
    cal_path = BOARD / run / 'calibration_live.json'
    params = geo.parameters(load_calibration(cal_path, admit_measured=True), 0.)
    basis = SectionBasis(json.loads(cal_path.read_text())['laser_plane_camera'])
    cfg = ProfileConfig()
    tracker, trend = ProfileTracker(cfg), []
    for name in raw_names:
        image = np.fromfile(BOARD / run / name, np.uint8).reshape(480, 752)
        f, q, recs, patches = frame_profile(image, binned, params, basis, cfg)
        summ = tracker.update(f, q)
        if f['step_mm'] is not None:
            trend.append(round(f['step_mm'], 2))
    return section_view(f, summ, cfg, trend, image, centres(recs, patches)), q


def main():
    out = ROOT / 'ui_mockups'
    out.mkdir(exist_ok=True)
    pages = PageRenderer(FONTS)
    base = dict(frame=12873, fps=13.1, latency_ms=74, clock='23:41:05', devices=DEVICES)

    vase = np.fromfile(VASE_RAW, np.uint8, count=752 * 480, offset=752 * 480 * 60).reshape(480, 752)
    recs, patches, rows = detect(vase, False)
    c = centres(recs, patches)
    run = json.loads((BOARD / 'r37_r41c_live01/frames.json').read_text())
    ae = json.loads((BOARD / 'r37_r41c_live01/status.json').read_text(encoding='utf8')).get('pl_ae_trace', [])
    trends = [('单帧处理', [round(f['pl_ms'], 1) for f in run][-120:], 'ms'),
              ('点数', [f['xyz'] for f in run][-120:], ''),
              ('曝光', [a['rows'] for a in ae] or [240, 240], '行')]
    capture_ms = float(np.median([f['capture_ms'] for f in run]))
    live = dict(gray=vase, centres=c, points=len(c), rows=rows, capture_ms=capture_ms,
                pl_hw_ms=4.49, trends=trends,   # 448,813 frontend cycles at 100 MHz (lane test)
                quality=assess_quality(rows, len(c)), pl_panel=np.load(VASE_RUN / 'PL_VIEW_PANEL.npy'),
                cloud_frames=240)
    Image.fromarray(pages.render('A', dict(base, live=live))).save(out / 'live.png')
    alert = ('过曝：饱和像素 3475，曝光下调中', 'bad')
    Image.fromarray(pages.render('A', dict(base, live=live, alert=alert))).save(out / 'live_alert.png')

    names = [f'raw_{k:06d}.gray8' for k in range(15, 360, 15)]
    sec, _ = section_state('r37_r41f_live02', [n for n in names if (BOARD / 'r37_r41f_live02' / n).exists()], True)
    Image.fromarray(pages.render('B', dict(base, section=sec))).save(out / 'section.png')
    sec2, q2 = section_state('r37_r41g_live02', [f'raw_{k:06d}.gray8' for k in range(75, 165, 15)], True)
    hint = q2['hints'][0]['zh'] if q2['hints'] else '质量不足'
    alert = (hint + '，结果不判定', 'warn')
    Image.fromarray(pages.render('B', dict(base, section=sec2, alert=alert))).save(out / 'section_alert.png')

    cloud = np.load(VASE_RUN / 'cloud.npy')
    r = np.hypot(cloud[:, 0], cloud[:, 1])
    bands = [(z, 2 * float(np.median(r[np.abs(cloud[:, 2] - z) < 0.5])), int((np.abs(cloud[:, 2] - z) < 0.5).sum()))
             for z in np.arange(4, 48, 4.0) if (np.abs(cloud[:, 2] - z) < 0.5).sum() > 20]
    record = [('状态', '扫描中'), ('开始', '23:40:32'), ('文件', 'scan_20261030_01'),
              ('转台', '3200 步/圈'), ('条纹', '合并'), ('转轴', '未标定')]
    scan = dict(running=True, position=110, positions=200, angle_deg=198.0, frames=110, elapsed_s=33,
                remaining_s=27, points=len(cloud), bands=bands, record=record,
                pl_panel=np.load(VASE_RUN / 'PL_VIEW_PANEL.npy'))
    devices = DEVICES[:3] + [('电机', '运行', th.OK)]
    Image.fromarray(pages.render('C', dict(base, devices=devices, scan=scan))).save(out / 'scan.png')
    stopped = dict(scan, running=False, remaining_s=None, record=[('状态', '失败')] + record[1:])
    failed = dict(base, devices=DEVICES[:3] + [('电机', '故障', th.NG)], scan=stopped,
                  alert=('电机故障，扫描已停止，数据已保存', 'bad'))
    Image.fromarray(pages.render('C', failed)).save(out / 'scan_alert.png')
    print('ok', len(c), sec['items'][0], len(sec['trend']), len(bands))


if __name__ == '__main__':
    main()
