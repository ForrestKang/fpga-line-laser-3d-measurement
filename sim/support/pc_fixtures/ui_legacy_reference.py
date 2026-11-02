"""HDMI pages (live, section, scan) in the industrial vision style; main thread only.

The FPGA draws the point cloud into PL_WINDOWS[page]; the software frame leaves that area to
it unless a panel image is passed in (PC mockups).
"""
from collections import OrderedDict
from functools import lru_cache

import numpy as np

from r33 import ui_theme as th
from r33.profile_live import row_fields

PAGES = {'A': ('实时', 'BTN1'), 'B': ('截面', 'BTN2'), 'C': ('扫描', 'BTN3')}
PL_WINDOWS = {'A': (823, 131), 'C': (9, 65)}
VERDICT = {'PASS': ('合格', th.OK), 'FAIL': ('超差', th.NG), 'UNSTABLE': ('不稳定', th.WARN),
           'MEASURED': ('未设公差', th.DIM), 'WAIT': ('测量中', th.DIM)}
QUALITY = {'GOOD': ('良好', th.OK), 'WARN': ('注意', th.WARN), 'BAD': ('差', th.NG), 'WAIT': ('等待', th.DIM)}
CAMERA_BOX = (9, 131, 649, 539)       # body of the live camera panel


@lru_cache(maxsize=8)
def _sample_indices(w, h):
    """Small immutable coordinate tables, shared by full views and thumbnails."""
    ys, xs = np.arange(h) * 480 // h, np.arange(w) * 752 // w
    ys.setflags(write=False)
    xs.setflags(write=False)
    return ys, xs


def camera_rgb(gray, centres, w, h):
    """Nearest-neighbour camera view with the stripe centres (row, column) marked."""
    ys, xs = _sample_indices(w, h)
    img = np.empty((h, w, 3), dtype=np.uint8)
    img[:] = np.asarray(gray)[ys[:, None], xs[None, :], None]
    c = np.asarray(centres, int).reshape(-1, 2)
    cy, cx = c[:, 0] * h // 480, c[:, 1] * w // 752
    for dx in (-1, 0, 1):
        img[np.clip(cy, 0, h - 1), np.clip(cx + dx, 0, w - 1)] = th.CENTRE
    return img


@lru_cache(maxsize=8)
def _dial_ticks(n, cx, cy, radius):
    """Keep the original floating arithmetic, but evaluate fixed ticks once."""
    ticks = []
    for i in range(n):
        angle = np.radians(90 - 360.0 * i / n)
        c, s = np.cos(angle), np.sin(angle)
        ticks.append(tuple((cx + (radius - inset) * c, cy - (radius - inset) * s,
                            cx + radius * c, cy - radius * s) for inset in (8, 14)))
    return tuple(ticks)


class Glyphs:
    """Each (text, font, anchor) is rasterised once; later frames only blend the mask."""

    LIMIT = 4096
    BYTE_LIMIT = 16 * 1024 * 1024

    def __init__(self):
        self.masks = OrderedDict()
        self.mask_bytes = 0

    def mask(self, text, font, anchor):
        key = (text, id(font), anchor)
        m = self.masks.get(key)
        if m is None:
            from PIL import Image, ImageDraw
            left, top, right, bottom = (int(v) for v in font.getbbox(text, anchor=anchor))
            im = Image.new('L', (max(1, right - left), max(1, bottom - top)), 0)
            ImageDraw.Draw(im).text((-left, -top), text, fill=255, font=font, anchor=anchor)
            size = im.width * im.height
            while self.masks and (len(self.masks) >= self.LIMIT or self.mask_bytes + size > self.BYTE_LIMIT):
                _, (_, _, old) = self.masks.popitem(last=False)
                self.mask_bytes -= old.width * old.height
            m = (left, top, im)
            if size <= self.BYTE_LIMIT:
                self.masks[key] = m
                self.mask_bytes += size
        else:
            self.masks.move_to_end(key)
        return m

    def draw(self, out, xy, text, font, colour, anchor):
        """Blend cached antialiased masks in Pillow's native byte loop.

        The old path made several float32 arrays per label on the ARM.  Keeping
        this operation on the existing Pillow canvas also avoids an intermediate
        1280x720 conversion before drawing the text.  No text geometry changes.
        """
        left, top, mask = self.mask(text, font, anchor)
        x, y = int(round(xy[0])) + left, int(round(xy[1])) + top
        out.paste(colour, (x, y, x + mask.width, y + mask.height), mask)



class PageRenderer:
    def __init__(self, fonts=None):
        self.fonts = fonts or th.Fonts()
        self.glyphs = Glyphs()
        self._texts = []
        self._lengths = OrderedDict()
        self._background_pixel = np.array((*th.BG, 255), dtype=np.uint8).view(np.uint32)[0]

    def render(self, page, st):
        from PIL import Image, ImageDraw
        # Each result owns a separate mapped canvas: retained frames cannot be
        # changed by later renders. RGB export through np.array(PIL_image) would
        # encode/join/copy the whole page. A three-channel view skips that work.
        canvas = np.empty((th.HEIGHT, th.WIDTH, 4), dtype=np.uint8)
        canvas.view(np.uint32).fill(self._background_pixel)
        img = Image.frombuffer('RGBA', (th.WIDTH, th.HEIGHT), canvas, 'raw', 'RGBA', 0, 1)
        # The storage is exclusively owned by this render call. Prevent Pillow's
        # default copy-on-write detachment of a mapped buffer.
        img.readonly = 0
        d = ImageDraw.Draw(img)
        self._texts = []
        self._title_bar(d, page)
        self._status_bar(d, st)
        pastes = {'A': self._live, 'B': self._section, 'C': self._scan}[page](d, st) or []
        for (x, y), rgb in pastes:
            img.paste(Image.fromarray(rgb), (x, y))
        for xy, s, font, colour, anchor in self._texts:   # text always lies on top of shapes
            self.glyphs.draw(img, xy, s, font, colour, anchor)
        out = canvas[:, :, :3]
        out.setflags(write=False)
        return out

    # --- drawing helpers

    def _text(self, d, xy, s, size=14, colour=th.TEXT, mono=False, anchor='la'):
        if s.strip():
            self._texts.append((xy, s, self.fonts.get(size, mono and s.isascii()), colour, anchor))

    def _text_width(self, d, text, font):
        key = (text, id(font))
        if key not in self._lengths:
            if len(self._lengths) >= 1024:
                self._lengths.popitem(last=False)
            self._lengths[key] = d.textlength(text, font=font)
        else:
            self._lengths.move_to_end(key)
        return self._lengths[key]

    def _panel(self, d, box, title, extra=None):
        x0, y0, x1, y1 = box
        d.rectangle(box, fill=th.PANEL, outline=th.BORDER)
        d.rectangle((x0 + 1, y0 + 1, x1 - 1, y0 + 22), fill=th.BAR)
        self._text(d, (x0 + 8, y0 + 12), title, 13, anchor='lm')
        if extra:
            self._text(d, (x1 - 8, y0 + 12), extra, 12, th.DIM, mono=True, anchor='rm')
        return x0 + 1, y0 + 23, x1 - 1, y1 - 1

    def _badge(self, d, xy, text, colour, size=13, anchor='lm'):
        font = self.fonts.get(size)
        w = self._text_width(d, text, font) + 12
        x, y = xy
        if anchor == 'mm':
            x -= w / 2
        d.rectangle((x, y - size // 2 - 4, x + w, y + size // 2 + 4), fill=colour)
        self._texts.append(((x + 6, y), text, font, th.TEXT, 'lm'))

    def _spark(self, d, body, values, colour=th.ACCENT, lo=None, hi=None, robust=False):
        x0, y0, x1, y1 = body[0] + 36, body[1] + 8, body[2] - 8, body[3] - 8
        v = np.asarray(values, float)
        if len(v) < 2:
            return
        if robust:
            med = np.nanmedian(v)
            mad = max(np.nanmedian(np.abs(v - med)), 0.05)
            lo, hi = max(np.nanmin(v), med - 6 * mad), min(np.nanmax(v), med + 6 * mad)
        lo = np.nanmin(v) if lo is None else lo
        hi = np.nanmax(v) if hi is None else hi
        if hi - lo < 1e-9:
            lo, hi = lo - 1, hi + 1
        for t in (lo, hi):
            y = y1 - (t - lo) * (y1 - y0) / (hi - lo)
            d.line((x0, y, x1, y), fill=th.GRID)
            self._text(d, (x0 - 4, y), f'{t:g}', 11, th.DIM, mono=True, anchor='rm')
        xs = x0 + np.arange(len(v)) * (x1 - x0) / (len(v) - 1)
        ys = y1 - (np.clip(v, lo, hi) - lo) * (y1 - y0) / (hi - lo)
        d.line(list(zip(xs, ys)), fill=colour, width=2)
        for x, y in zip(xs[(v < lo) | (v > hi)], ys[(v < lo) | (v > hi)]):
            d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=th.NG)   # outlier, clipped to the scale

    def _axes(self, d, area, s_range, h_range, s_ticks=8, h_ticks=6):
        x0, y0, x1, y1 = area
        (s0, s1), (h0, h1) = s_range, h_range
        def sx(v):
            return x0 + (v - s0) * (x1 - x0) / (s1 - s0)

        def sy(v):
            return y1 - (v - h0) * (y1 - y0) / (h1 - h0)

        for lo, hi, n, horiz in ((s0, s1, s_ticks, False), (h0, h1, h_ticks, True)):
            step = th.nice_step(hi - lo, n)
            for v in np.arange(np.ceil(lo / step) * step, hi + 1e-9, step):
                v = 0.0 if abs(v) < 1e-9 else v
                if horiz:
                    d.line((x0, sy(v), x1, sy(v)), fill=th.GRID)
                    self._text(d, (x0 - 6, sy(v)), f'{v:g}', 11, th.DIM, mono=True, anchor='rm')
                else:
                    d.line((sx(v), y0, sx(v), y1), fill=th.GRID)
                    self._text(d, (sx(v), y1 + 5), f'{v:g}', 11, th.DIM, mono=True, anchor='ma')
        d.rectangle(area, outline=th.BORDER)
        return sx, sy

    # --- frame

    def _title_bar(self, d, page):
        d.rectangle((0, 0, th.WIDTH, 36), fill=(16, 18, 21))
        self._text(d, (14, 18), th.TITLE, 18, anchor='lm')
        x = th.WIDTH - 14 - 3 * 128 - 8
        for key, (name, btn) in PAGES.items():
            on = key == page
            d.rectangle((x, 5, x + 124, 31), fill=th.ACCENT if on else th.BAR)
            self._text(d, (x + 12, 18), name, 15, th.TEXT if on else th.DIM, anchor='lm')
            self._text(d, (x + 112, 18), btn, 11, th.TEXT if on else th.DIM, mono=True, anchor='rm')
            x += 128

    def _status_bar(self, d, st):
        y = 694
        d.rectangle((0, y, th.WIDTH, th.HEIGHT), fill=(16, 18, 21))
        x = 14
        for name, state, colour in st.get('devices', []):
            d.rectangle((x, y + 9, x + 8, y + 17), fill=colour)
            label = f'{name} {state}'
            self._text(d, (x + 14, y + 13), label, 13, th.DIM, anchor='lm')
            x += 40 + self._text_width(d, label, self.fonts.get(13))
        alert = st.get('alert')
        if alert:
            self._badge(d, (640, y + 13), alert[0], th.NG if alert[1] == 'bad' else th.WARN, 13, anchor='mm')
        info = (f'帧 {st.get("frame", 0)}   {st.get("fps", 0):.1f} 帧/秒   '
                f'延迟 {st.get("latency_ms", 0):.0f} ms   {st.get("clock", "")}')
        self._text(d, (th.WIDTH - 14, y + 13), info, 13, th.DIM, anchor='rm')

    def _pl_window(self, page, panel):
        if panel is None:
            return []
        rgb = th.height_colours(int(panel.max()) or 1)[panel].repeat(2, 0).repeat(2, 1)
        return [(PL_WINDOWS[page], rgb[:th.PL_WINDOW[1], :th.PL_WINDOW[0]])]

    # --- pages

    def _live(self, d, st):
        lv = st['live']
        body = self._panel(d, (8, 42, 1272, 102), '处理流水线')
        stages = [('相机采集', f'{lv["capture_ms"]:.1f} ms  752×480', False),
                  ('条纹提取', f'{lv["pl_hw_ms"]:.1f} ms  {len(lv["centres"])} 中心', True),
                  ('坐标解算', f'{lv["points"]} 点  Q16 mm', True),
                  ('点云显示', f'{st.get("fps", 0):.1f} 帧/秒', False)]
        w = (body[2] - body[0]) / 4
        for i, (name, value, fpga) in enumerate(stages):
            x = body[0] + i * w + 10
            cy = (body[1] + body[3]) // 2
            if fpga:
                self._badge(d, (x, cy), 'FPGA', th.ACCENT, 11)
                x += 48
            self._text(d, (x, cy), name, 14, anchor='lm')
            self._text(d, (x + 66, cy), value, 13, th.DIM, mono=True, anchor='lm')
            if i < 3:
                ax = body[0] + (i + 1) * w - 6
                d.polygon([(ax - 8, cy - 6), (ax, cy), (ax - 8, cy + 6)], fill=th.DIM)

        cam = self._panel(d, (8, 108, 650, 540), '相机画面', f'752×480  条纹中心 {len(lv["centres"])}')
        img = camera_rgb(lv['gray'], lv['centres'], cam[2] - cam[0], cam[3] - cam[1])

        q = self._panel(d, (656, 108, 816, 540), '条纹')
        self._stripe_column(d, q, lv['rows'])

        self._panel(d, (822, 108, 1272, 612), '三维点云  FPGA 实时渲染', f'累积 {lv.get("cloud_frames", 0)} 帧')
        for i, (title, values, unit) in enumerate(lv['trends']):
            box = (8 + i * 270, 546, 272 + i * 270 if i < 2 else 816, 688)
            b = self._panel(d, box, title, f'{values[-1]:g} {unit}' if values else '--')
            self._spark(d, b, values)
        qb = self._panel(d, (822, 618, 1272, 688), '采集质量')
        name, colour = QUALITY[lv['quality']['level']]
        self._badge(d, (qb[0] + 10, (qb[1] + qb[3]) // 2), name, colour, 16)
        m = lv['quality']['metrics']
        text = (f'完整 {m.get("completeness_pct", 0):.0f}%   多峰 {m.get("ambiguous_pct", 0):.0f}%   '
                f'饱和 {m.get("sat_pixels", 0)}   宽度 {m.get("width", 0)} px')
        self._text(d, (qb[0] + 70, (qb[1] + qb[3]) // 2), text, 14, anchor='lm')
        return [((cam[0], cam[1]), img)] + self._pl_window('A', lv.get('pl_panel'))

    def _stripe_column(self, d, body, rows):
        """Per-row stripe brightness and width aligned with the camera image; saturated rows red."""
        x0, y0, x1, y1 = body
        cols = ((x0 + 4, x0 + 64, '亮度', 255.0, th.ACCENT), (x0 + 72, x0 + 132, '宽度', 24.0, th.CENTRE))
        r = row_fields(rows)
        yy = y0 + np.arange(480) * (y1 - y0) / 480
        for a, b, label, full, colour in cols:
            d.line((a, y0, a, y1), fill=th.GRID)
            v = np.asarray(r['preview_max_peak' if label == '亮度' else 'preview_max_width'], float)
            coords = np.column_stack((a + np.minimum(1.0, v / full) * (b - a), yy))
            edges = np.diff(np.r_[False, v > 0, False].astype(np.int8))
            starts, stops = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
            for start, stop in zip(starts, stops):
                if stop - start > 1:
                    d.line([tuple(p) for p in coords[start:stop].tolist()], fill=colour)
            self._text(d, ((a + b) // 2, y1 - 8), label, 11, th.DIM, anchor='mm')
        for y in yy[np.asarray(r['sat250_count']) != 0]:
            d.line((x1 - 14, y, x1 - 4, y), fill=th.NG)

    def _section(self, d, st):
        sc = st['section']
        body = self._panel(d, (8, 42, 816, 452), '截面轮廓', '沿光条方向  高度相对参考面')
        area = (body[0] + 52, body[1] + 12, body[2] - 14, body[3] - 26)
        sx, sy = self._axes(d, area, sc['view'][:2], sc['view'][2:])
        lvl = sc['level_mm']
        d.rectangle((area[0] + 1, sy(lvl), area[2] - 1, sy(-lvl)), fill=th.BAND)
        d.line((area[0], sy(0), area[2], sy(0)), fill=th.OK)
        self._text(d, (area[2] - 4, sy(lvl) - 2), f'参考面 ±{lvl:.2f} mm', 11, th.OK, anchor='rb')
        s, hr = np.asarray(sc['s']), np.asarray(sc['h_rel'])
        keep = (s >= sc['view'][0]) & (s <= sc['view'][1]) & (hr >= sc['view'][2]) & (hr <= sc['view'][3])
        for a, b in zip(s[keep], hr[keep]):
            d.rectangle((sx(a) - 1, sy(b) - 1, sx(a) + 1, sy(b) + 1), fill=th.TEXT)
        if sc.get('step_marker'):
            sm, top = sc['step_marker']
            x = sx(sm) - 10
            d.line((x, sy(0), x, sy(top)), fill=th.ACCENT, width=2)
            for yv in (0, top):
                d.line((x - 6, sy(yv), x + 6, sy(yv)), fill=th.ACCENT, width=2)
            self._badge(d, (x - 4, (sy(0) + sy(top)) / 2), f'{top:.2f} mm', th.ACCENT, 12, anchor='mm')

        self._measure_table(d, self._panel(d, (822, 42, 1272, 300), '测量项目', f'{len(sc["items"])} 项'), sc)
        tb = self._panel(d, (822, 306, 1272, 452), '台阶趋势', f'最近 {len(sc["trend"])} 帧')
        self._spark(d, tb, sc['trend'], th.ACCENT, robust=True)

        thumb_box = self._panel(d, (8, 458, 300, 688), '相机', '测量窗口')
        thumb = self._thumbnail(sc['gray'], sc.get('window'), thumb_box)
        self._quality_bars(d, self._panel(d, (306, 458, 816, 688), '采集质量'), sc['quality'])
        res = np.asarray(sc['residuals'])
        hb = self._panel(d, (822, 458, 1272, 688), '参考面残差分布', f'σ {np.std(res):.2f} mm' if len(res) else 'σ --')
        self._histogram(d, hb, sc['residuals'])
        return [((thumb_box[0], thumb_box[1]), thumb)]

    def _measure_table(self, d, body, sc):
        x0, y0, x1, _ = body
        cols = (x0 + 10, x0 + 120, x0 + 215, x0 + 290, x0 + 360)
        for x, t in zip(cols, ('项目', '测量值', '名义', '公差', '判定')):
            self._text(d, (x, y0 + 8), t, 12, th.DIM)
        d.line((x0 + 6, y0 + 28, x1 - 6, y0 + 28), fill=th.GRID)
        for i, it in enumerate(sc['items'][:7]):
            y = y0 + 42 + 28 * i
            nominal = '--' if it.get('nominal') is None else f'{it["nominal"]:.2f}'
            tol = '--' if it.get('tol') is None else f'±{it["tol"]:.2f}'
            value = '--' if it.get('value') is None else f'{it["value"]:.2f}'
            for x, t, mono in zip(cols, (it['name'], value, nominal, tol), (False, True, True, True)):
                self._text(d, (x, y), t, 14, th.TEXT if x == cols[1] else th.DIM, mono=mono, anchor='lm')
            name, colour = VERDICT.get(it.get('verdict', 'MEASURED'))
            if colour == th.DIM:
                self._text(d, (cols[4], y), name, 13, th.DIM, anchor='lm')
            else:
                self._badge(d, (cols[4], y), name, colour, 13)

    def _quality_bars(self, d, body, q):
        x0, y0, x1, y1 = body
        m = q['metrics']
        rows = [('完整度', m.get('completeness_pct', 0), 100, f'{m.get("completeness_pct", 0):.0f}%', 80),
                ('多峰行', m.get('ambiguous_pct', 0), 100, f'{m.get("ambiguous_pct", 0):.0f}%', -5),
                ('饱和像素', m.get('sat_pixels', 0), 400, str(m.get('sat_pixels', 0)), -200),
                ('条纹宽度', m.get('width', 0), 32, f'{m.get("width", 0)} px', -24),
                ('峰值亮度', m.get('peak', 0), 255, str(m.get('peak', 0)), 60)]
        name, colour = QUALITY[q['level']]
        self._badge(d, (x1 - 60, y0 + 18), name, colour, 14)
        for i, (label, v, full, text, limit) in enumerate(rows):
            y = y0 + 22 + 38 * i
            good = v >= limit if limit >= 0 else v <= -limit
            self._text(d, (x0 + 10, y), label, 13, th.DIM, anchor='lm')
            bx0, bx1 = x0 + 90, x1 - 150
            d.rectangle((bx0, y - 6, bx1, y + 6), fill=th.BG)
            d.rectangle((bx0, y - 6, bx0 + (bx1 - bx0) * min(1.0, v / full), y + 6), fill=th.OK if good else th.WARN)
            self._text(d, (bx1 + 10, y), text, 14, mono=True, anchor='lm')

    def _histogram(self, d, body, values):
        v = np.asarray(values, float)
        x0, y0, x1, y1 = body[0] + 14, body[1] + 12, body[2] - 14, body[3] - 24
        lim = max(0.5, float(np.percentile(np.abs(v), 98))) if len(v) else 1.0
        counts, edges = np.histogram(v, bins=31, range=(-lim, lim))
        top = max(1, counts.max())
        bw = (x1 - x0) / len(counts)
        for i, c in enumerate(counts):
            d.rectangle((x0 + i * bw + 1, y1 - c * (y1 - y0) / top, x0 + (i + 1) * bw - 1, y1), fill=th.ACCENT)
        d.line((x0, y1, x1, y1), fill=th.BORDER)
        for t in (-lim, 0, lim):
            self._text(d, (x0 + (t + lim) / (2 * lim) * (x1 - x0), y1 + 5), f'{t:+.2f}' if t else '0', 11, th.DIM,
                       mono=True, anchor='ma')

    def _thumbnail(self, gray, window, body):
        w, h = body[2] - body[0], body[3] - body[1]
        ys, xs = _sample_indices(w, h)
        rgb = np.empty((h, w, 3), dtype=np.uint8)
        rgb[:] = gray[ys[:, None], xs[None, :], None]
        if window:
            r0, r1, c0, c1 = window
            y0, y1, x0, x1 = r0 * h // 480, min(h - 1, r1 * h // 480), c0 * w // 752, min(w - 1, c1 * w // 752)
            rgb[[y0, y1], x0:x1 + 1] = th.ACCENT
            rgb[y0:y1 + 1, [x0, x1]] = th.ACCENT
        return rgb

    def _scan(self, d, st):
        sc = st['scan']
        k, n = sc['position'], sc['positions']
        self._panel(d, (8, 42, 458, 546), '三维点云  FPGA 实时渲染', '扫描累积' if sc.get('running') else '上次扫描')
        dial = self._panel(d, (464, 42, 864, 402), '角度覆盖', f'步距 {3200 // n} 步')
        cx, cy, r = (dial[0] + dial[2]) / 2, (dial[1] + dial[3]) / 2, 140
        for i, (pending, completed) in enumerate(_dial_ticks(n, cx, cy, r)):
            done = i < k
            d.line(completed if done else pending, fill=th.ACCENT if done else th.GRID, width=2)
        a = np.radians(90 - sc['angle_deg'])
        d.line((cx + 70 * np.cos(a), cy - 70 * np.sin(a), cx + (r - 20) * np.cos(a), cy - (r - 20) * np.sin(a)),
               fill=th.TEXT, width=2)
        self._text(d, (cx, cy - 14), f'{k}/{n}', 28, mono=True, anchor='mm')
        self._text(d, (cx, cy + 20), f'{sc["angle_deg"]:.1f}°', 18, th.DIM, mono=True, anchor='mm')

        pb = self._panel(d, (464, 408, 864, 546), '扫描进度')
        rem, used = sc.get('remaining_s'), sc.get('elapsed_s', 0)
        rows = [('已采', f'{sc.get("frames", 0)} 帧'), ('用时', f'{int(used) // 60}:{int(used) % 60:02d}'),
                ('剩余', '--' if rem is None else f'{int(rem) // 60}:{int(rem) % 60:02d}'),
                ('点数', f'{sc.get("points", 0):,}')]
        for i, (label, value) in enumerate(rows):
            x, y = pb[0] + 14 + (i % 2) * 196, pb[1] + 18 + (i // 2) * 34
            self._text(d, (x, y), label, 13, th.DIM, anchor='lm')
            self._text(d, (x + 44, y), value, 18, mono=True, anchor='lm')
        d.rectangle((pb[0] + 14, pb[3] - 22, pb[2] - 14, pb[3] - 12), fill=th.BG)
        if k:
            d.rectangle((pb[0] + 14, pb[3] - 22, pb[0] + 14 + (pb[2] - pb[0] - 28) * k / max(1, n), pb[3] - 12),
                        fill=th.ACCENT)

        bands = sc.get('bands', [])
        cb = self._panel(d, (870, 42, 1272, 402), '直径-高度曲线')
        if bands:
            hgt = np.array([b[0] for b in bands])
            dia = np.array([b[1] for b in bands])
            area = (cb[0] + 48, cb[1] + 12, cb[2] - 14, cb[3] - 26)
            sx, sy = self._axes(d, area, (0, np.ceil(dia.max() / 5) * 5 + 5), (0, np.ceil(hgt.max() / 5) * 5 + 5), 6, 6)
            d.line([(sx(a), sy(b)) for a, b in zip(dia, hgt)], fill=th.ACCENT, width=2)
            for a, b in zip(dia, hgt):
                d.rectangle((sx(a) - 2, sy(b) - 2, sx(a) + 2, sy(b) + 2), fill=th.TEXT)
            self._text(d, (area[2] - 4, area[3] - 4), '直径 mm', 11, th.DIM, anchor='rb')
            self._text(d, (area[0] + 4, area[1] + 4), '高度 mm', 11, th.DIM)
        else:
            self._text(d, ((cb[0] + cb[2]) / 2, (cb[1] + cb[3]) / 2), '按 BTN0 开始扫描', 15, th.DIM, anchor='mm')
        tb = self._panel(d, (870, 408, 1272, 688), '分带直径', f'{len(bands)} 带')
        cols = (tb[0] + 14, tb[0] + 140, tb[0] + 270)
        for x, t in zip(cols, ('高度 mm', '直径 mm', '点数')):
            self._text(d, (x, tb[1] + 8), t, 12, th.DIM)
        for i, (hgt_i, dia_i, pts) in enumerate(bands[:10]):
            y = tb[1] + 36 + 22 * i
            for x, t in zip(cols, (f'{hgt_i:5.1f}', f'{dia_i:6.2f}', f'{pts}')):
                self._text(d, (x, y), t, 14, mono=True, anchor='lm')

        rb = self._panel(d, (8, 552, 864, 688), '扫描记录')
        for i, (label, value) in enumerate(sc.get('record', [])):
            x, y = rb[0] + 14 + (i % 3) * 282, rb[1] + 20 + (i // 3) * 32
            self._text(d, (x, y), label, 13, th.DIM, anchor='lm')
            self._text(d, (x + 64, y), value, 15, mono=True, anchor='lm')
        return self._pl_window('C', sc.get('pl_panel'))


class PageCompositor:
    """Full page with Pillow at most every `refresh_s`; the live camera view is patched in every frame."""

    def __init__(self, renderer, refresh_s=1.0):
        from collections import deque
        self.renderer, self.refresh_s = renderer, refresh_s
        self.page, self.base, self.at = None, None, 0.0
        self.render_ms = deque(maxlen=600)

    def frame(self, page, state_fn, gray=None, centres=None):
        """Read-only RGB page; `state_fn()` is called only when the page is re-rendered."""
        import time
        now = time.monotonic()
        current_camera_rendered = False
        if page != self.page or self.base is None or now - self.at >= self.refresh_s:
            st = state_fn()
            if st is not None:
                t = time.perf_counter()
                base = self.renderer.render(page, st)
                self.render_ms.append(1e3 * (time.perf_counter() - t))
                base.setflags(write=False)
                self.page, self.base, self.at = page, base, now
                live = st.get('live', {})
                current_camera_rendered = (page == 'A' and live.get('gray') is gray
                                           and live.get('centres') is centres)
        if self.base is None:
            return None
        if self.page != 'A' or gray is None or current_camera_rendered:
            return self.base
        out = self.base.copy()
        x0, y0, x1, y1 = CAMERA_BOX
        out[y0:y1, x0:x1] = camera_rgb(gray, centres, x1 - x0, y1 - y0)
        out.setflags(write=False)
        return out
