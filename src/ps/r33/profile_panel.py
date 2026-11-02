"""R41 fixed-point profile panel for the 1280x720 HDMI frame.

Right of the camera image: the live laser-plane section (s along the line, h toward the
camera) with the fitted reference line, the measured step, the gap edges and the change
reference. Under both: four result cards (step, gap, profile change, acquisition quality)
with the tolerance judgement and the quality hints. Chinese labels need the bundled subset
of Noto Sans CJK SC (assets/fonts, OFL); without it the panel falls back to English.
"""
from pathlib import Path
import numpy as np

PLOT_X, PLOT_Y, PLOT_W, PLOT_H = 800, 120, 456, 480
CARD_Y, CARD_H = 610, 106
FONT = Path(__file__).resolve().parents[1] / 'assets' / 'fonts' / 'NotoSansCJKsc-R41-subset.otf'

BG, GRID, TEXT, DIM = '#101822', '#26323f', '#e8eef4', '#8896a6'
GOOD, BAD, WARN, INFO = '#60ff90', '#ff6060', '#ffd060', '#80c8ff'
REF, STEP, OTHER, CHANGE_REF = '#80c8ff', '#ffa040', '#a0a8b0', '#506070'

# key -> (Chinese, English)
LABELS = {
    'title': ('截面轮廓（激光平面）', 'SECTION PROFILE (LASER PLANE)'),
    'axis_s': ('沿线 s / mm', 's along line / mm'),
    'axis_h': ('高度 h / mm', 'h / mm'),
    'step': ('台阶高差', 'STEP HEIGHT'),
    'gap': ('间隙', 'GAP'),
    'change': ('轮廓变化', 'PROFILE CHANGE'),
    'quality': ('采集质量', 'QUALITY'),
    'PASS': ('合格', 'PASS'),
    'FAIL': ('超差', 'OUT OF TOL'),
    'UNSTABLE': ('不稳定', 'UNSTABLE'),
    'WAIT': ('测量中', 'MEASURING'),
    'MEASURED': ('未设公差', 'NO TOLERANCE'),
    'GOOD': ('良好', 'GOOD'),
    'BAD': ('差：暂停判定', 'BAD: NO VERDICT'),
    'SAME': ('无变化', 'NO CHANGE'),
    'CHANGED': ('有变化', 'CHANGED'),
    'LEARNING': ('学习参考', 'LEARNING'),
    'OFF': ('关闭', 'OFF'),
    'NO_OVERLAP': ('无重叠', 'NO OVERLAP'),
    'none': ('未检出', 'NOT FOUND'),
    'nominal': ('名义', 'nominal'),
    'dev': ('偏差', 'dev'),
    'spread': ('波动', 'spread'),
    'depth': ('深', 'depth'),
    'max_dev': ('偏差', 'dev'),
    'threshold': ('阈值', 'threshold'),
    'noise': ('噪声', 'noise'),
    'typical': ('典型偏差', 'typical dev'),
    'at': ('位置', 'at'),
    'frames': ('帧', 'frames'),
    'dropped': ('自动重采：丢弃', 'auto re-acquire: dropped'),
    'outliers': ('异常剔除', 'outliers'),
    'grade': ('预览级点', 'preview-grade points'),
    'points': ('点', 'pts'),
    'reference': ('参考面', 'reference'),
}
STATUS_COLOUR = dict(PASS=GOOD, FAIL=BAD, UNSTABLE=WARN, WAIT=DIM, MEASURED=INFO, GOOD=GOOD, WARN=WARN,
                     BAD=BAD, SAME=GOOD, CHANGED=BAD, LEARNING=DIM, OFF=DIM, NO_OVERLAP=WARN)


class ProfilePanel:
    def __init__(self, font_path=FONT):
        from PIL import ImageFont
        self.fonts, self.chinese = {}, False
        self._truetype = None
        for path in (font_path, Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')):
            try:
                if path and Path(path).is_file():
                    ImageFont.truetype(str(path), 14)
                    self._truetype, self.chinese = str(path), path == font_path
                    break
            except OSError:
                continue
        self.view = None            # (s0, s1, h0, h1) with hysteresis
        self.shrink = 0
        self._bg_key = self._bg = None          # cached plot background (grid, labels, reference)
        self._cards_key = self._cards = None    # cached result cards
        self.glyphs = GlyphCache()

    def label(self, key):
        zh, en = LABELS[key]
        return zh if self.chinese else en

    def font(self, size):
        from PIL import ImageFont
        if size not in self.fonts:
            self.fonts[size] = (ImageFont.truetype(self._truetype, size) if self._truetype
                                else ImageFont.load_default())
        return self.fonts[size]

    def _fit_view(self, sh):
        if not len(sh):
            return self.view or (0., 1., 0., 1.)
        s0, s1 = np.percentile(sh[:, 0], [1, 99])
        h0, h1 = np.percentile(sh[:, 1], [1, 99])
        ps, ph = max(2., .08 * (s1 - s0)), max(1., .25 * (h1 - h0))
        want = (s0 - ps, s1 + ps, h0 - ph, h1 + ph)
        v = self.view
        if v is None or want[0] < v[0] or want[1] > v[1] or want[2] < v[2] or want[3] > v[3]:
            self.view, self.shrink = (min(want[0], v[0]) if v else want[0], max(want[1], v[1]) if v else want[1],
                                      min(want[2], v[2]) if v else want[2], max(want[3], v[3]) if v else want[3]), 0
        elif (want[1] - want[0]) < .5 * (v[1] - v[0]) or (want[3] - want[2]) < .5 * (v[3] - v[2]):
            self.shrink += 1
            if self.shrink > 30:
                self.view, self.shrink = want, 0
        return self.view

    def _background(self, view, reference, sx, sy, box):
        from PIL import Image, ImageDraw
        key = (view, id(reference))
        if key == self._bg_key:
            return self._bg
        s0, s1, h0, h1 = view
        x0, y0, x1, y1 = box
        img = Image.new('RGB', (PLOT_W, PLOT_H), BG)
        d = ImageDraw.Draw(img)
        d.text((10, 6), self.label('title'), fill=TEXT, font=self.font(18))
        small = self.font(12)
        step_mm, hstep = nice_step(s1 - s0), nice_step(h1 - h0)
        for v in np.arange(np.ceil(s0 / step_mm) * step_mm, s1, step_mm):
            x = float(sx(v)); d.line((x, y0, x, y1), fill=GRID); d.text((x - 10, y1 + 4), f'{v + 0.:g}', fill=DIM, font=small)
        for v in np.arange(np.ceil(h0 / hstep) * hstep, h1, hstep):
            y = float(sy(v)); d.line((x0, y, x1, y), fill=GRID); d.text((6, y - 7), f'{v + 0.:g}', fill=DIM, font=small)
        d.rectangle((x0, y0, x1, y1), outline=GRID)
        d.text((x1 - 110, y1 + 22), self.label('axis_s'), fill=DIM, font=small)
        d.text((x0 + 4, y0 + 2), self.label('axis_h'), fill=DIM, font=small)
        if reference:
            bins, bin_mm = reference['bins'], reference['bin_mm']
            keys = sorted(bins)
            pts = [(float(sx((k + .5) * bin_mm)), float(sy(bins[k]))) for k in keys]
            for a, b, ka, kb in zip(pts, pts[1:], keys, keys[1:]):
                if kb - ka <= 2:
                    d.line((a, b), fill=CHANGE_REF, width=3)
        self._bg_key, self._bg = key, img
        return img

    def plot(self, summary, reference=None):
        from PIL import ImageDraw
        f = summary['frame']
        sh = f['profile']
        x0, y0, x1, y1 = 56, 40, PLOT_W - 12, PLOT_H - 46
        s0, s1, h0, h1 = view = self._fit_view(sh)
        sx = lambda s: x0 + (np.asarray(s) - s0) * (x1 - x0) / (s1 - s0)
        sy = lambda h: y1 - (np.asarray(h) - h0) * (y1 - y0) / (h1 - h0)
        img = self._background(view, reference, sx, sy, (x0, y0, x1, y1)).copy()
        d = ImageDraw.Draw(img)
        labels = []                 # (xy, text, colour, size): drawn from the glyph cache below
        segs = f['segment_list']
        if f['reference'] is not None:
            g = segs[f['reference']]
            d.line((float(sx(s0)), float(sy(g['a'] * s0 + g['b'])), float(sx(s1)), float(sy(g['a'] * s1 + g['b']))),
                   fill=REF, width=1)
        if f['step_segment'] is not None:
            g, r = segs[f['step_segment']], segs[f['reference']]
            sm = .5 * (g['s0'] + g['s1'])
            xa, ya, yb = float(sx(sm)), float(sy(r['a'] * sm + r['b'])), float(sy(g['a'] * sm + g['b']))
            d.line((xa, ya, xa, yb), fill=STEP, width=2)
            labels.append(((min(xa + 6, PLOT_W - 72), .5 * (ya + yb) - 8), f'{f["step_mm"]:+.2f}', STEP, 16))
        if f['gap_edges'] is not None:
            r = segs[f['reference']]
            ea, eb = f['gap_edges']
            ya = float(sy(r['a'] * ea + r['b'])) - 18
            xa, xb = float(sx(ea)), float(sx(eb))
            d.line((xa, ya - 6, xa, ya + 6), fill=WARN, width=2); d.line((xb, ya - 6, xb, ya + 6), fill=WARN, width=2)
            d.line((xa, ya, xb, ya), fill=WARN, width=2)
            labels.append(((.5 * (xa + xb) - 20, ya - 24), f'{f["gap_mm"]:.2f}', WARN, 16))
        out = np.array(img)
        for xy, text, colour, size in labels:
            self.glyphs.text(out, xy, text, colour, self.font(size))
        if len(sh):
            # points as 3x3 dots straight into the pixel array (segment colour: reference / step / other)
            cls = np.zeros(len(sh), np.int8)
            for j, g in enumerate(segs):
                cls[g['i0']:g['i1']] = 1 if j == f['reference'] else 2 if j == f['step_segment'] else 0
            xs, ys = np.rint(sx(sh[:, 0])).astype(np.int64), np.rint(sy(sh[:, 1])).astype(np.int64)
            ok = (xs >= x0 + 1) & (xs <= x1 - 1) & (ys >= y0 + 1) & (ys <= y1 - 1)
            rgb = np.array([_rgb(OTHER), _rgb(REF), _rgb(STEP)], np.uint8)[cls[ok]]
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    out[ys[ok] + dy, xs[ok] + dx] = rgb
        return out

    def _value(self, j, unit='mm'):
        return self.label('none') if j['value'] is None else f'{j["value"]:.2f} {unit}'

    def cards(self, summary):
        from PIL import Image, ImageDraw
        d = _Ops()
        w = 1280 // 4
        big, mid, small = self.font(30), self.font(18), self.font(14)
        q = summary['quality']
        frozen = q['level'] == 'BAD'
        for k, key in enumerate(('step', 'gap')):
            j = summary[key]
            x = 16 + k * w
            d.text((x, 4), self.label(key), fill=DIM, font=mid)
            d.text((x, 28), self._value(j) if j['status'] != 'WAIT' else '--', fill=TEXT, font=big)
            status = 'BAD' if frozen else j['status']
            line = self.label(status)
            if j.get('nominal') is not None:
                line += f'  {self.label("nominal")} {j["nominal"]:.2f}±{j["tol"]:.2f}'
                if j['value'] is not None:
                    line += f'  {self.label("dev")} {j["deviation"]:+.2f}'
            d.text((x, 68), line, fill=STATUS_COLOUR.get(status, TEXT), font=small)
            noise = summary['frame'].get('noise_mm')
            tail = f'  {self.label("noise")} ±{noise:.2f}' if noise is not None else ''
            if j['spread'] is not None:
                d.text((x, 86), f'{self.label("spread")} {j["spread"]:.2f} mm  n={j["n"]}{tail}', fill=DIM, font=small)
            elif tail:
                d.text((x, 86), tail.strip(), fill=DIM, font=small)
        c = summary['change']
        x = 16 + 2 * w
        d.text((x, 4), self.label('change'), fill=DIM, font=mid)
        st = c.get('status', 'WAIT')
        d.text((x, 28), self.label(st) if st in LABELS else st, fill=STATUS_COLOUR.get(st, TEXT), font=big)
        if 'max_dev_mm' in c:
            d.text((x, 68), f'{self.label("max_dev")} {c["max_dev_mm"]:+.2f} mm  {self.label("at")} s={c["at_s_mm"]:.1f}'
                            f'  ({c["length_mm"]:.1f} mm)', fill=BAD, font=small)
        elif 'typical_dev_mm' in c:
            d.text((x, 68), f'{self.label("typical")} {c["typical_dev_mm"]:.2f} mm', fill=DIM, font=small)
        if 'threshold_mm' in c:
            d.text((x, 86), f'{self.label("threshold")} ±{c["threshold_mm"]:.2f} mm', fill=DIM, font=small)
        elif st == 'LEARNING':
            d.text((x, 68), f'{c.get("frames", 0)}/{c.get("needed", 0)} {self.label("frames")}', fill=DIM, font=small)
        x = 16 + 3 * w
        d.text((x, 4), self.label('quality'), fill=DIM, font=mid)
        d.text((x, 28), self.label(q['level']) if q['level'] != 'WARN' else (
            q['hints'][0]['zh' if self.chinese else 'en'] if q['hints'] else 'WARN'),
            fill=STATUS_COLOUR[q['level']], font=mid if q['level'] == 'WARN' else big)
        hints = [h['zh' if self.chinese else 'en'] for h in q['hints']][(1 if q['level'] == 'WARN' else 0):][:2]
        for i, h in enumerate(hints):
            d.text((x, 62 + 16 * i), h, fill=WARN, font=small)
        cnt = summary['counts']
        drops = cnt['rejected_quality'] + sum(cnt['outliers'].values())
        if drops:
            d.text((x, 90), f'{self.label("dropped")} {cnt["rejected_quality"]} / {self.label("outliers")} '
                            f'{sum(cnt["outliers"].values())}', fill=DIM, font=small)
        key = tuple(d.ops)
        if key != self._cards_key:
            out = np.empty((CARD_H, 1280, 3), np.uint8)
            out[:] = _rgb(BG)
            for xy, text, fill, font in d.ops:
                self.glyphs.text(out, xy, text, fill, font)
            self._cards_key, self._cards = key, out
        return self._cards


class _Ops:
    """Collects text draws (font objects are cached per size, so the list is a valid cache key)."""

    def __init__(self):
        self.ops = []

    def text(self, xy, text, fill=None, font=None):
        self.ops.append((tuple(float(v) for v in xy), text, fill, font))


def _rgb(colour):
    if not colour.startswith('#'):
        from PIL import ImageColor
        return ImageColor.getrgb(colour)[:3]
    return tuple(int(colour[k:k + 2], 16) for k in (1, 3, 5))


class GlyphCache:
    """Text without repeated Pillow work: each (string, font) is rendered once by Pillow into an
    alpha mask and pasted with one numpy blend; only new strings (changed numbers) cost a render."""

    LIMIT = 2048

    def __init__(self):
        self.masks = {}

    def _mask(self, text, font):
        key = (text, id(font))
        m = self.masks.get(key)
        if m is None:
            from PIL import Image, ImageDraw
            try:
                l, t, r, b = font.getbbox(text)
            except AttributeError:          # bitmap default font on older Pillow
                l, t, (r, b) = 0, 0, font.getsize(text)
            im = Image.new('L', (max(1, int(r) + 2), max(1, int(b) + 2)), 0)
            ImageDraw.Draw(im).text((0, 0), text, fill=255, font=font)
            if len(self.masks) >= self.LIMIT:
                self.masks.clear()
            m = self.masks[key] = np.asarray(im, np.float32)[:, :, None] / 255.
        return m

    def text(self, out, xy, text, fill, font):
        if not text.strip():
            return
        a = self._mask(text, font)
        x, y = int(round(xy[0])), int(round(xy[1]))
        H, W = out.shape[:2]
        y0, x0, y1, x1 = max(0, y), max(0, x), min(H, y + a.shape[0]), min(W, x + a.shape[1])
        if y1 <= y0 or x1 <= x0:
            return
        al = a[y0 - y:y1 - y, x0 - x:x1 - x]
        reg = out[y0:y1, x0:x1].astype(np.float32)
        out[y0:y1, x0:x1] = (reg + (np.array(_rgb(fill), np.float32) - reg) * al + .5).astype(np.uint8)


def nice_step(span, ticks=6):
    """1-2-5 grid step giving at most about `ticks` intervals over span."""
    raw = max(span, 1e-6) / ticks
    base = 10 ** np.floor(np.log10(raw))
    for m in (1, 2, 5, 10):
        if m * base >= raw:
            return float(m * base)


def blit(frame, rgb, y, x):
    """RGB uint8 image into the HDMI store (G,B,R,X byte lanes)."""
    h, w = rgb.shape[:2]
    region = frame[y:y + h, x:x + w]
    region[:, :, 0] = rgb[:, :, 1]
    region[:, :, 1] = rgb[:, :, 2]
    region[:, :, 2] = rgb[:, :, 0]
    region[:, :, 3] = 0


def glyph_text():
    """Every character the panel and the quality hints can draw (font subsetting input)."""
    from .profile_live import HINTS
    chars = set(chr(c) for c in range(32, 127)) | set('±–—…·°%：，（）')
    for zh, _ in LABELS.values():
        chars |= set(zh)
    for _, zh, _ in HINTS.values():
        chars |= set(zh)
    return ''.join(sorted(chars))
