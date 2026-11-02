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
PL_WINDOWS = {'A': (796, 137), 'C': (36, 67)}
VERDICT = {'PASS': ('合格', th.OK), 'FAIL': ('超差', th.NG), 'UNSTABLE': ('不稳定', th.WARN),
           'MEASURED': ('未设公差', th.DIM), 'WAIT': ('测量中', th.DIM)}
QUALITY = {'GOOD': ('良好', th.OK), 'WARN': ('注意', th.WARN), 'BAD': ('差', th.NG), 'WAIT': ('等待', th.DIM)}
CAMERA_BOX = (9, 137, 761, 617)       # body of the live camera panel: 752x480, one pixel per pixel


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
        self._text(d, (th.WIDTH - 14, y + 13), st.get('clock', ''), 13, th.DIM, mono=True, anchor='rm')

    def _pl_window(self, page, panel):
        if panel is None:
            return []
        rgb = th.height_colours(int(panel.max()) or 1)[panel].repeat(2, 0).repeat(2, 1)
        return [(PL_WINDOWS[page], rgb[:th.PL_WINDOW[1], :th.PL_WINDOW[0]])]

    # --- pages: few, large values; the camera and the FPGA point cloud carry the page

    def _live(self, d, st):
        lv = st['live']
        # camera and FPGA point cloud only, centred between the title and status bars
        cam = self._panel(d, (8, 114, 762, 618), '相机画面', '绿线为激光中心')
        img = camera_rgb(lv['gray'], lv['centres'], cam[2] - cam[0], cam[3] - cam[1])
        self._panel(d, (768, 114, 1272, 618), '三维点云  FPGA 实时渲染')
        return [((cam[0], cam[1]), img)] + self._pl_window('A', lv.get('pl_panel'))

    def _section(self, d, st):
        sc = st['section']
        body = self._panel(d, (8, 44, 900, 548), '截面轮廓', '高度相对参考面')
        area = (body[0] + 52, body[1] + 14, body[2] - 16, body[3] - 28)
        sx, sy = self._axes(d, area, sc['view'][:2], sc['view'][2:])
        lvl = sc['level_mm']
        d.rectangle((area[0] + 1, sy(lvl), area[2] - 1, sy(-lvl)), fill=th.BAND)
        d.line((area[0], sy(0), area[2], sy(0)), fill=th.OK)
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
        self._text(d, (area[2] - 4, area[3] - 4), '沿光条 mm', 11, th.DIM, anchor='rb')

        items = {it['name']: it for it in sc['items']}
        main = items.get('主台阶', {})
        box = (906, 44, 1272, 300)
        d.rectangle(box, fill=th.PANEL, outline=th.BORDER)
        self._text(d, (box[0] + 16, box[1] + 26), '台阶高度', 16, th.DIM, anchor='lm')
        value = '--' if main.get('value') is None else f'{main["value"]:.2f}'
        self._text(d, (box[0] + 16, box[1] + 124), value, 72, mono=True, anchor='lm')
        self._text(d, (box[2] - 18, box[1] + 140), 'mm', 20, th.DIM, anchor='rm')
        name, colour = VERDICT.get(main.get('verdict', 'MEASURED'))
        self._badge(d, (box[0] + 16, box[3] - 34), name, colour, 18)
        for i, label in enumerate(('间隙', '噪声')):
            v = items.get(label, {}).get('value')
            y0 = 306 + i * 70
            d.rectangle((906, y0, 1272, y0 + 64), fill=th.PANEL, outline=th.BORDER)
            self._text(d, (922, y0 + 32), label, 16, th.DIM, anchor='lm')
            self._text(d, (1222, y0 + 32), '--' if v is None else f'{v:.2f}', 30, mono=True, anchor='rm')
            self._text(d, (1256, y0 + 36), 'mm', 14, th.DIM, anchor='rm')
        level, colour = QUALITY[sc['quality']['level']]
        qb = (906, 446, 1272, 548)
        d.rectangle(qb, fill=th.PANEL, outline=th.BORDER)
        self._text(d, (qb[0] + 16, qb[1] + 24), '采集质量', 15, th.DIM, anchor='lm')
        self._badge(d, (qb[0] + 16, qb[1] + 68), level, colour, 22)

        tb = self._panel(d, (8, 556, 900, 688), '台阶趋势', f'最近 {len(sc["trend"])} 帧')
        self._spark(d, tb, sc['trend'], th.ACCENT, robust=True)
        thumb_box = self._panel(d, (906, 556, 1272, 688), '相机')
        return [((thumb_box[0], thumb_box[1]), self._thumbnail(sc['gray'], sc.get('window'), thumb_box))]

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
        self._panel(d, (8, 44, 512, 548), '三维点云  FPGA 实时成形', '扫描中' if sc.get('running') else '上次扫描')

        ring = (520, 44, 1272, 340)
        d.rectangle(ring, fill=th.PANEL, outline=th.BORDER)
        cx, cy, r = 680, 192, 118
        for i, (pending, completed) in enumerate(_dial_ticks(n, cx, cy, r)):
            d.line(completed if i < k else pending, fill=th.ACCENT if i < k else th.GRID, width=2)
        a = np.radians(90 - sc['angle_deg'])
        d.line((cx + 60 * np.cos(a), cy - 60 * np.sin(a), cx + (r - 18) * np.cos(a), cy - (r - 18) * np.sin(a)),
               fill=th.TEXT, width=3)
        self._text(d, (cx, cy - 10), f'{100 * k // max(1, n)}%', 40, mono=True, anchor='mm')
        self._text(d, (cx, cy + 30), f'{sc["angle_deg"]:.0f}°', 18, th.DIM, mono=True, anchor='mm')
        used, rem = sc.get('elapsed_s', 0), sc.get('remaining_s')
        rows = [('状态', sc.get('state_label', '待机')), ('用时', f'{int(used) // 60}:{int(used) % 60:02d}'),
                ('剩余', '--' if rem is None else f'{int(rem) // 60}:{int(rem) % 60:02d}'),
                ('点数', f'{sc.get("points", 0):,}')]
        for i, (label, value) in enumerate(rows):
            y = 84 + i * 66
            self._text(d, (850, y), label, 15, th.DIM, anchor='lm')
            self._text(d, (930, y), value, 30, mono=value.isascii(), anchor='lm')

        bands = sc.get('bands', [])
        cb = self._panel(d, (520, 348, 1272, 688), '直径-高度', f'{len(bands)} 层' if bands else None)
        if bands:
            hgt = np.array([b[0] for b in bands])
            dia = np.array([b[1] for b in bands])
            area = (cb[0] + 48, cb[1] + 12, cb[0] + 470, cb[3] - 26)
            sx, sy = self._axes(d, area, (0, np.ceil(dia.max() / 5) * 5 + 5), (0, np.ceil(hgt.max() / 5) * 5 + 5), 6, 5)
            d.line([(sx(a), sy(b)) for a, b in zip(dia, hgt)], fill=th.ACCENT, width=2)
            for a, b in zip(dia, hgt):
                d.rectangle((sx(a) - 2, sy(b) - 2, sx(a) + 2, sy(b) + 2), fill=th.TEXT)
            self._text(d, (area[2] - 4, area[3] - 4), '直径 mm', 11, th.DIM, anchor='rb')
            self._text(d, (area[0] + 4, area[1] + 4), '高度 mm', 11, th.DIM)
            for i, (label, value) in enumerate((('最大直径', f'{dia.max():.1f}'), ('高度范围', f'{np.ptp(hgt):.1f}'))):
                y = cb[1] + 40 + i * 140
                self._text(d, (cb[0] + 500, y), label, 15, th.DIM, anchor='lm')
                self._text(d, (cb[0] + 500, y + 52), value, 44, mono=True, anchor='lm')
                self._text(d, (cb[2] - 14, y + 60), 'mm', 16, th.DIM, anchor='rm')
        else:
            self._text(d, ((cb[0] + cb[2]) / 2, (cb[1] + cb[3]) / 2), '按 BTN0 开始扫描', 22, th.DIM, anchor='mm')

        hb = (8, 556, 512, 688)
        d.rectangle(hb, fill=th.PANEL, outline=th.BORDER)
        self._text(d, (hb[0] + 16, hb[1] + 34), 'BTN0  开始 / 停止扫描', 18, anchor='lm')
        self._text(d, (hb[0] + 16, hb[1] + 84), 'BTN1 实时   BTN2 截面   BTN3 扫描', 18, th.DIM, anchor='lm')
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
