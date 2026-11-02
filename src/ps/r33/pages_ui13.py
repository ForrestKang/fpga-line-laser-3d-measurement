"""UI13 software pages: real observations only, with no hardware access.

Pillow is used only on the caller/main thread for cached backgrounds and glyphs.
Raw placement, centre markers, section dots and cloud projection use NumPy.  The
returned frame owns its memory and is read-only, so the display worker can safely
consume it after the next render. This renderer never supplies measurement data.
"""
from pathlib import Path
import time
import numpy as np

WIDTH, HEIGHT = 1280, 720
BG = '#0a111e'
PANEL, LINE = '#101d30', '#263a52'
WHITE, DIM = '#eaf3ff', '#91a9c5'
CYAN, GREEN, AMBER = '#4bdded', '#60eea9', '#ffcd70'
FONT_DIR = Path(__file__).resolve().parents[1] / 'assets/fonts'


class PageRenderer:
    """Bounded page composition, not a reconstruction or measurement backend."""

    backend = 'NUMPY_OBSERVED_POINTS_UI13'

    def __init__(self, font_path=None, max_points=16000):
        from PIL import ImageFont
        from r33.profile_panel import GlyphCache
        self.max_points = max(1, min(16000, int(max_points)))
        candidates = [font_path] if font_path else []
        candidates += [FONT_DIR / 'NotoSansCJKsc-UI13-subset.otf',
                       FONT_DIR / 'NotoSansCJKsc-R41-subset.otf',
                       Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
        self.font_path = next((str(p) for p in candidates if p and Path(p).is_file()), None)
        self.fonts = {n: ImageFont.truetype(self.font_path, n) if self.font_path
                      else ImageFont.load_default() for n in (12, 14, 16, 18, 20, 24, 28)}
        self.glyphs, self.backgrounds = GlyphCache(), {}
        self.render_ms = 0.
        self.last_display_points = 0

    def text(self, out, xy, message, size=16, colour=WHITE):
        self.glyphs.text(out, xy, str(message), colour, self.fonts[size])

    def _background(self, page):
        if page in self.backgrounds:
            return self.backgrounds[page]
        from PIL import Image, ImageDraw
        im = Image.new('RGB', (WIDTH, HEIGHT), BG)
        d = ImageDraw.Draw(im)
        d.rectangle((0, 0, WIDTH - 1, 3), fill=CYAN)
        d.text((24, 15), 'LINE / SCAN 3D', font=self.fonts[24], fill=WHITE)
        d.text((270, 23), '线激光三维测量系统', font=self.fonts[16], fill=DIM)
        for j, (letter, name) in enumerate((('A', '实时观测'), ('B', '截面测量'), ('C', '全屏三维'))):
            x = 24 + 202 * j
            d.rounded_rectangle((x, 65, x + 186, 103), 8,
                                fill='#1a3b50' if page == letter else '#111f31',
                                outline=CYAN if page == letter else LINE)
            d.text((x + 12, 71), 'BTN%d  %s' % (j + 1, name), font=self.fonts[16],
                   fill=CYAN if page == letter else DIM)
        if page == 'A':
            for box in ((24, 151, 744, 613), (760, 151, 1256, 613)):
                d.rounded_rectangle(box, 9, fill=PANEL, outline=LINE)
            d.text((40, 159), '同帧原图 / PL光条观测', font=self.fonts[18], fill=WHITE)
            d.text((778, 159), '真实观测点云', font=self.fonts[18], fill=WHITE)
            for j, label in enumerate(('处理速度', '当前观测', '正式测量点', '模型状态')):
                x = 24 + j * 311
                d.rounded_rectangle((x, 625, x + 299, 694), 8, fill=PANEL, outline=LINE)
                d.text((x + 12, 630), label, font=self.fonts[14], fill=DIM)
        elif page == 'B':
            d.rounded_rectangle((24, 151, 886, 601), 9, fill=PANEL, outline=LINE)
            d.rounded_rectangle((902, 151, 1256, 408), 9, fill=PANEL, outline=LINE)
            d.rounded_rectangle((902, 423, 1256, 601), 9, fill=PANEL, outline=LINE)
            d.text((41, 159), '当前截面 / 激光平面', font=self.fonts[18], fill=WHITE)
            d.text((917, 159), '光条定位 / 同一帧', font=self.fonts[16], fill=WHITE)
            d.text((917, 435), '采集质量与可信状态', font=self.fonts[18], fill=WHITE)
            for j, label in enumerate(('台阶高差 / 预览', '间隙 / 预览', '轮廓变化', '尺寸状态')):
                x = 24 + j * 311
                d.rounded_rectangle((x, 617, x + 299, 694), 8, fill=PANEL, outline=LINE)
                d.text((x + 12, 623), label, font=self.fonts[14], fill=DIM)
            for x in np.linspace(90, 855, 7).astype(int):
                d.line((int(x), 218, int(x), 552), fill=LINE)
            for y in np.linspace(218, 552, 6).astype(int):
                d.line((90, int(y), 855, int(y)), fill=LINE)
            d.text((744, 574), '沿线 s / mm', font=self.fonts[12], fill=DIM)
            d.text((41, 190), '高度 h / mm', font=self.fonts[12], fill=DIM)
        else:
            # The 3D viewport spans the whole width; no camera inset or sidebar.
            d.rounded_rectangle((24, 151, 1256, 665), 9, fill=PANEL, outline=LINE)
        self.backgrounds[page] = np.array(im)
        return self.backgrounds[page]

    def _raw(self, out, gray, centres, box):
        x, y, w, h = box
        a = np.asarray(gray)
        if a.shape != (480, 752) or a.dtype != np.uint8:
            raise ValueError('gray must be uint8 480x752')
        scale = min(w / 752., h / 480.)
        rw, rh = max(1, int(752 * scale)), max(1, int(480 * scale))
        x, y, w, h = x + (w - rw) // 2, y + (h - rh) // 2, rw, rh
        # Integer nearest-neighbour coordinates keep the full field of view.
        yy = np.arange(h) * 480 // h
        xx = np.arange(w) * 752 // w
        out[y:y + h, x:x + w] = a[yy[:, None], xx[None, :], None]
        c = np.asarray(centres)
        if c.size:
            if c.ndim != 2 or c.shape[1] != 2:
                raise ValueError('centres must have row,column pairs')
            c = c[np.isfinite(c).all(axis=1)]
            c = c[(c[:, 0] >= 0) & (c[:, 0] < 480) & (c[:, 1] >= 0) & (c[:, 1] < 752)]
            ys = y + np.minimum(h - 1, (c[:, 0] * h / 480).astype(np.int64))
            xs = x + np.minimum(w - 1, (c[:, 1] * w / 752).astype(np.int64))
            for dx in (-1, 0, 1):
                out[ys, np.clip(xs + dx, x, x + w - 1)] = (179, 255, 37)

    def _cloud(self, out, xyz, box, state):
        x, y, w, h = box
        p = np.asarray(xyz)
        if p.size == 0:
            p = np.empty((0, 3), np.float64)
        if p.ndim != 2 or p.shape[1] not in (3, 4):
            raise ValueError('xyz must be Nx3 or Nx4')
        # Bound work before allocation. No mesh, completion, or CAD geometry.
        p = p[::max(1, (len(p) + self.max_points - 1) // self.max_points), :3]
        p = p[np.isfinite(p).all(axis=1)]
        self.last_display_points = len(p)
        if not len(p):
            self.text(out, (x + 30, y + h // 2 - 18), '等待有效三维观测', 20, DIM)
            self.text(out, (x + 30, y + h // 2 + 17), '无点时保留空白，不生成表面', 14, DIM)
            return
        q = p - (np.min(p, axis=0) + np.max(p, axis=0)) / 2
        # Static camera XYZ has camera -Y as the approximate vertical.  Object
        # clouds use their declared object Z; never infer a rotation angle.
        camera = state.get('cloud_frame', 'camera') == 'camera'
        if camera:
            u = q[:, 0] - .46 * q[:, 2]
            v = q[:, 1] + .20 * q[:, 2]
            depth, height = q[:, 2], -p[:, 1]
        else:
            u = .85 * q[:, 0] - .53 * q[:, 1]
            depth = .53 * q[:, 0] + .85 * q[:, 1]
            v, height = -.95 * q[:, 2] + .25 * depth, p[:, 2]
        span_u, span_v = max(float(np.ptp(u)), 1e-6), max(float(np.ptp(v)), 1e-6)
        scale = min((w - 64) / span_u, (h - 62) / span_v)
        xs = np.rint((u - (u.max() + u.min()) / 2) * scale + x + w / 2).astype(np.int64)
        ys = np.rint((v - (v.max() + v.min()) / 2) * scale + y + h / 2).astype(np.int64)
        t = (height - height.min()) / max(float(np.ptp(height)), 1e-6)
        # Bounded blue/cyan/green/yellow/magenta height ramp.
        anchors = np.array(((40, 99, 255), (25, 218, 231), (79, 244, 112),
                            (249, 228, 58), (247, 71, 176)), dtype=np.float64)
        f = np.minimum(t * 4, 3.999999)
        k = f.astype(np.int64)
        rgb = (anchors[k] * (1 - f[:, None] % 1) + anchors[k + 1] * (f[:, None] % 1)).astype(np.uint8)
        order = np.argsort(-depth, kind='stable')
        xs, ys, rgb = xs[order], ys[order], rgb[order]
        good = (xs >= x + 2) & (xs < x + w - 2) & (ys >= y + 2) & (ys < y + h - 2)
        xs, ys, rgb = xs[good], ys[good], rgb[good]
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                out[ys + dy, xs + dx] = rgb

    def _section(self, out, profile):
        p = np.asarray((profile or {}).get('frame', {}).get('profile', np.empty((0, 2))))
        if not p.size:
            self.text(out, (280, 360), '等待有效截面', 24, DIM)
            return
        p = p[np.isfinite(p).all(axis=1), :2]
        if not len(p):
            return
        lo, hi = p.min(axis=0), p.max(axis=0)
        span = np.maximum(hi - lo, .1)
        lo, hi = lo - span * .08, hi + span * .08
        xs = np.rint(90 + (p[:, 0] - lo[0]) / (hi[0] - lo[0]) * 765).astype(np.int64)
        ys = np.rint(552 - (p[:, 1] - lo[1]) / (hi[1] - lo[1]) * 334).astype(np.int64)
        for j in range(7):
            self.text(out, (76 + j * 127, 557), '%.1f' % (lo[0] + (hi[0] - lo[0]) * j / 6), 12, DIM)
        for j in range(6):
            self.text(out, (32, 542 - j * 67), '%.1f' % (lo[1] + (hi[1] - lo[1]) * j / 5), 12, DIM)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                out[ys + dy, xs + dx] = (75, 221, 237)

    def render(self, page, gray, centres, xyz, profile=None, state=None):
        """Return an owned, immutable RGB HDMI frame; do not mutate any input."""
        start = time.perf_counter()
        if page not in ('A', 'B', 'C'):
            raise ValueError('page must be A, B or C')
        s = state or {}
        out = self._background(page).copy()
        self.text(out, (836, 22), s.get('raw_source', s.get('source_label', '相机实采')), 16, CYAN)
        motor_label = ('12V未接 / 运动禁用' if s.get('motor_power_disconnected') is True else
                       '运动未授权' if not s.get('motor_authorized', False) else '有限运动已授权')
        self.text(out, (1020, 22), motor_label, 16, AMBER)
        self.text(out, (652, 72), '帧 %s  |  %.1f 帧/秒  |  PL %.1f ms' % (
            s.get('frame', 0), float(s.get('fps', 0)), float(s.get('pl_ms', 0))), 16, DIM)
        self.text(out, (26, 115), s.get('calibration_label', '标定候选 / 尺寸待验证'), 16, AMBER)
        kind = s.get('display_kind', '静态剖面 / 未旋转')
        if page == 'A':
            self._raw(out, gray, centres, (36, 192, 696, 410))
            self._cloud(out, xyz, (772, 205, 472, 357), s)
            self.text(out, (778, 571), kind, 14, DIM)
            self.text(out, (36, 652), '%.1f 帧/秒' % float(s.get('fps', 0)), 24, CYAN)
            self.text(out, (347, 652), '%s 点' % s.get('preview_points', len(np.asarray(xyz))), 24, GREEN)
            self.text(out, (658, 652), '%s 点' % s.get('measurement_points', 0), 24)
            self.text(out, (969, 655), '已拼接' if s.get('cloud_frame') == 'object' else '静止截面', 20, AMBER)
        elif page == 'B':
            self._raw(out, gray, centres, (914, 191, 330, 207))
            self._section(out, profile)
            q = (profile or {}).get('quality', {})
            level = q.get('level', 'WAIT')
            level_text = {'GOOD': '良好', 'WARN': '存在质量提示', 'BAD': '不足，暂停判定', 'WAIT': '等待有效观测'}.get(level, level)
            self.text(out, (918, 470), level_text, 20, GREEN if level == 'GOOD' else AMBER)
            self.text(out, (918, 510), '截面点 %s' % (profile or {}).get('frame', {}).get('points', 0), 16, DIM)
            self.text(out, (918, 541), '预览值不作精度结论', 16, AMBER)
            hints = q.get('hints', [])
            if hints:
                self.text(out, (918, 570), str(hints[0].get('zh', hints[0].get('en', '')))[:19], 12, DIM)
            for j, key in enumerate(('step', 'gap')):
                value = (profile or {}).get(key, {}).get('value')
                value = float(value) if value is not None else None
                message = '%.2f mm' % value if value is not None and np.isfinite(value) else '--'
                self.text(out, (36 + j * 311, 649), message, 28, CYAN)
                self.text(out, (185 + j * 311, 659), '待标定', 14, AMBER)
            change = (profile or {}).get('change', {}).get('status', 'WAIT')
            self.text(out, (658, 653), {'WAIT': '等待参考', 'SAME': '无明显变化', 'CHANGED': '发现变化',
                      'LEARNING': '学习参考', 'OFF': '关闭', 'NO_OVERLAP': '无重叠'}.get(change, change), 20, DIM)
            self.text(out, (969, 653), '待实物验证', 20, AMBER)
        else:
            self._cloud(out, xyz, (36, 188, 1208, 428), s)
            self.text(out, (42, 160), kind, 18, WHITE)
            self.text(out, (42, 628), '显示 %s / 已观测 %s 点  ·  保留缺口，不补面' % (
                self.last_display_points, s.get('total_points', len(np.asarray(xyz)))), 16, DIM)
            self.text(out, (32, 680), 'BTN1 主界面  /  BTN2 截面测量  /  BTN3 全屏三维', 16, DIM)
            self.text(out, (872, 680), 'BTN0 有限扫描 / 停止' if s.get('motor_authorized', False)
                      else 'BTN0 运动未授权', 16, AMBER)
        out.setflags(write=False)
        self.render_ms = 1000 * (time.perf_counter() - start)
        return out
