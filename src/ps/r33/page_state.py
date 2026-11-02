"""Numbers behind the three HDMI pages: rolling trends, section view and scan summary.

No drawing and no hardware access. The live loop feeds it once per frame; `build(page)`
returns the dict that `ui_pages.PageRenderer.render` draws.
"""
import time
from collections import deque

import numpy as np

from r33 import ui_theme as th

PL_CLOCK_HZ = 100e6      # PFO3 frontend aclk (FCLK0)
HISTORY = 120
SCAN_STATE = {'IDLE': '待机', 'SCANNING': '扫描中', 'DONE': '已完成', 'STOPPED': '已停止', 'FAILED': '失败'}


def section_view(f, summary, cfg, trend, gray, centres):
    """Page B content from one measured frame and the running judgement."""
    sh, segs = np.asarray(f['profile'], float).reshape(-1, 2), f['segment_list']
    ref = segs[f['reference']] if f['reference'] is not None else None
    if ref is not None:
        h_rel = sh[:, 1] - (ref['a'] * sh[:, 0] + ref['b'])
        resid = h_rel[ref['i0']:ref['i1']]
    else:
        h_rel = sh[:, 1] - (np.median(sh[:, 1]) if len(sh) else 0.0)
        resid = np.zeros(0)
    if len(sh):
        lo, hi = np.percentile(h_rel, [1, 99])
        pad = max(2.0, 0.15 * (hi - lo))
        view = (sh[:, 0].min() - 2, sh[:, 0].max() + 2, lo - pad, hi + pad)
    else:
        view = (-10.0, 10.0, -5.0, 5.0)
    marker = None
    if f['step_segment'] is not None and f['step_mm'] is not None:
        marker = (segs[f['step_segment']]['s0'], f['step_mm'])

    step, gap = summary['step'], summary['gap']
    items = [dict(name='主台阶', value=step['value'] if step['value'] is not None else f['step_mm'],
                  nominal=cfg.step_nominal_mm, tol=cfg.step_tol_mm if cfg.step_nominal_mm is not None else None,
                  verdict=step['status'])]
    main = items[0]['value']
    shown = [v for v in (main, f['step_mm']) if v is not None]
    others = [s['step_mm'] for s in f['steps'] if all(abs(s['step_mm'] - v) > 0.5 for v in shown)]
    items += [dict(name=f'台阶 {i + 2}', value=v) for i, v in enumerate(sorted(others, key=abs, reverse=True)[:2])]
    items += [dict(name='间隙', value=gap['value'] if gap['value'] is not None else f['gap_mm'],
                   nominal=cfg.gap_nominal_mm, tol=cfg.gap_tol_mm if cfg.gap_nominal_mm is not None else None,
                   verdict=gap['status']),
              dict(name='参考面残差', value=f.get('reference_rms_mm') if f.get('backend') == 'pl' else float(np.std(resid)) if len(resid) else None),
              dict(name='噪声', value=f['noise_mm'])]

    rows = f['window'].get('rows') or (0, 479)
    c = np.asarray(centres).reshape(-1, 2)
    cols = (int(max(0, c[:, 1].min() - 30)), int(min(751, c[:, 1].max() + 30))) if len(c) else (0, 751)
    return dict(s=sh[:, 0], h_rel=h_rel, view=view, level_mm=f['level_mm'] or 0.0, step_marker=marker,
                items=items, trend=list(trend) or [0.0, 0.0], gray=gray, window=(rows[0], rows[1], *cols),
                quality=summary['quality'], residuals=resid)


def diameter_bands(points, axis_point, axis_dir, min_points=20, max_radius_mm=100.0):
    """(height, diameter, count) slabs along the turntable axis; at most 11 bands.

    Only points within max_radius_mm of the axis count: the background is far from it.
    """
    p = np.asarray(points, float).reshape(-1, 3) - np.asarray(axis_point, float)
    u = np.asarray(axis_dir, float) / np.linalg.norm(axis_dir)
    h = p @ u
    r = np.linalg.norm(p - h[:, None] * u, axis=1)
    near_axis = r <= max_radius_mm
    h, r = h[near_axis], r[near_axis]
    if len(h) < min_points:
        return []
    lo, hi = np.percentile(h, [1, 99])
    step = max(4.0, th.nice_step(hi - lo, 10))
    bands = []
    for z in np.arange(np.ceil(lo / step) * step, hi + 1e-9, step):
        near = np.abs(h - z) < 1.0
        if near.sum() >= min_points:
            bands.append((float(z), 2 * float(np.median(r[near])), int(near.sum())))
    return bands[:11]


class PageState:
    def __init__(self, *, run_name, binned, motor_allowed, motor_power_disconnected, turntable=None):
        self.run_name, self.binned = run_name, binned
        self.motor_allowed, self.motor_off = motor_allowed, motor_power_disconnected
        self.turntable = turntable
        self.trends = {k: deque(maxlen=HISTORY) for k in ('frame_ms', 'points', 'exposure', 'pl_ms')}
        self.step_trend = deque(maxlen=60)
        self.live = self.section = None
        self.common = dict(frame=0, fps=0.0, latency_ms=0.0)
        self.scan_status, self.scan_started, self.scan_ended = None, None, None
        self.scan_points, self._bands_key, self._bands = [], None, []
        self.cloud_frames = 0

    def frame(self, *, number, fps, capture_ms, frame_ms, cycles, gray, centres, rows, points, quality,
              exposure_rows=None, captured_at=None):
        pl_ms = 1e3 * cycles / PL_CLOCK_HZ
        self.trends['frame_ms'].append(round(frame_ms, 1))
        self.trends['points'].append(int(points))
        self.trends['pl_ms'].append(round(pl_ms, 2))
        if exposure_rows is not None:
            self.trends['exposure'].append(int(exposure_rows))
        latency = 1e3 * (time.perf_counter() - captured_at) if captured_at is not None else 0.0
        self.common = dict(frame=number, fps=fps, latency_ms=latency)
        self.live = dict(gray=gray, centres=centres, points=int(points), rows=rows, capture_ms=capture_ms,
                         pl_hw_ms=pl_ms, quality=quality)

    def profile(self, frame, summary, cfg, gray, centres):
        if frame['step_mm'] is not None:
            self.step_trend.append(round(frame['step_mm'], 2))
        self.section = section_view(frame, summary, cfg, self.step_trend, gray, centres)

    def scan(self, status, object_points):
        """Scan status each frame; object point blocks are only referenced, bands are made lazily."""
        now = time.monotonic()
        if status['state'] == 'SCANNING' and self.scan_started is None:
            self.scan_started, self.scan_ended = now, None
        elif status['state'] != 'SCANNING' and self.scan_started is not None and self.scan_ended is None:
            self.scan_ended = now
        self.scan_status, self.scan_points = status, object_points

    def _devices(self):
        s = (self.scan_status or {}).get('state', 'IDLE')
        if s == 'FAILED':
            motor = ('故障', th.NG)
        elif s == 'SCANNING':
            motor = ('运行', th.OK)
        elif self.motor_off:
            motor = ('12V 未接', th.DIM)
        elif not self.motor_allowed:
            motor = ('未授权', th.DIM)
        else:
            motor = ('就绪', th.OK)
        return [('相机', '正常', th.OK), ('FPGA', '已加载', th.OK), ('激光', '手动', th.DIM), ('电机', *motor)]

    def _alert(self, page):
        st = self.scan_status or {}
        if st.get('state') == 'FAILED':
            return '电机故障，扫描已停止，数据已保存', 'bad'
        q = (self.section or {}).get('quality') if page == 'B' else (self.live or {}).get('quality')
        if q and q['level'] in ('BAD', 'WARN') and q['hints']:
            text = q['hints'][0]['zh'] + ('，结果不判定' if page == 'B' and q['level'] == 'BAD' else '')
            return text, 'bad' if q['level'] == 'BAD' else 'warn'
        return None

    def _live_view(self, cloud_frames):
        t = self.trends
        third = ('曝光', list(t['exposure']), '行') if t['exposure'] else ('条纹提取', list(t['pl_ms']), 'ms')
        trends = [('单帧处理', list(t['frame_ms']), 'ms'), ('点数', list(t['points']), ''), third]
        return dict(self.live, trends=trends, cloud_frames=cloud_frames)

    def _scan_view(self):
        st = self.scan_status or dict(state='IDLE', views=0, target_views=200, angle_deg=0.0)
        k, n = int(st.get('visited_views', st['views'])), int(st['target_views'])
        start = self.scan_started
        elapsed = ((self.scan_ended or time.monotonic()) - start) if start is not None else 0.0
        remaining = elapsed / k * (n - k) if st['state'] == 'SCANNING' and k else None
        blocks = self.scan_points
        if self.turntable is not None and blocks and self._bands_key != len(blocks):
            p = np.concatenate(blocks)
            meas = p[p[:, 3] == 1, :3]
            self._bands = diameter_bands(meas if len(meas) >= 200 else p[:, :3],
                                         self.turntable.axis_point_mm, self.turntable.axis_direction)
            self._bands_key = len(blocks)
        started = time.strftime('%H:%M:%S', time.localtime(time.time() - (time.monotonic() - start))) if start else '--'
        state_label = SCAN_STATE.get(st['state'], st['state'])
        covered = st.get('measured_views', st['views']) + st.get('preview_views', 0)
        if st.get('motion_complete') and not st.get('coverage_complete', st.get('data_complete')):
            state_label = '转动完成，测量有缺口'
        record = [('状态', state_label), ('开始', started), ('文件', self.run_name),
                  ('有效/跳过', '%d / %d' % (covered, st.get('skipped_views', 0))),
                  ('转台', '3200 步/圈'), ('条纹', '合并' if self.binned else '单像素'), ('转轴', '未标定')]
        return dict(running=st['state'] == 'SCANNING', position=k, positions=n, angle_deg=float(st['angle_deg']),
                    frames=int(covered), elapsed_s=elapsed, remaining_s=remaining,
                    points=sum(len(b) for b in blocks), bands=self._bands, record=record, state_label=state_label)

    def build(self, page, cloud_frames=0):
        """Renderer state for `page`; None while that page has no data yet."""
        st = dict(self.common, devices=self._devices(), alert=self._alert(page), clock=time.strftime('%H:%M:%S'),
                  scan_state=(self.scan_status or {}).get('state', 'IDLE'))
        if page == 'A':
            return None if self.live is None else dict(st, live=self._live_view(cloud_frames))
        if page == 'B':
            return None if self.section is None else dict(st, section=self.section)
        return dict(st, scan=self._scan_view())
