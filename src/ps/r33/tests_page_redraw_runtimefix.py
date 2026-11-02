"""Display-only optimization checks, without any hardware dependency."""
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from r33 import ui_theme as th
from r33 import tests_pages as fixtures
from r33.tests_pages import gray_with_line
from r33.ui_pages import Glyphs, PageCompositor, PageRenderer, camera_rgb, _sample_indices


class RedrawTests(unittest.TestCase):
    def test_camera_matches_original_nearest_neighbour_and_markers(self):
        gray = np.random.default_rng(58).integers(0, 256, (480, 752), np.uint8)
        centres = np.array([[0, 0], [479, 751], [180, 390]], dtype=np.int64)
        for w, h in ((640, 408), (290, 206), (1, 1)):
            ys, xs = np.arange(h) * 480 // h, np.arange(w) * 752 // w
            expected = np.repeat(gray[ys][:, xs][:, :, None], 3, axis=2)
            cy, cx = centres[:, 0] * h // 480, centres[:, 1] * w // 752
            for dx in (-1, 0, 1):
                expected[np.clip(cy, 0, h - 1), np.clip(cx + dx, 0, w - 1)] = th.CENTRE
            self.assertTrue(np.array_equal(camera_rgb(gray, centres, w, h), expected))
            a, b = _sample_indices(w, h)
            self.assertFalse(a.flags.writeable or b.flags.writeable)

    def test_native_mask_blend_matches_previous_float32_with_clipping(self):
        font = th.Fonts().get(16)
        glyphs = Glyphs()
        for text in ('相机 123.4 ms', 'ABC 截面', '-0.02 mm'):
            for xy, anchor in (((-4, 1), 'la'), ((110, 25), 'rm'), ((10, 15), 'lm')):
                expected = np.full((42, 120, 3), (19, 27, 130), np.uint8)
                left, top, mask = glyphs.mask(text, font, anchor)
                x, y = round(xy[0]) + left, round(xy[1]) + top
                alpha = np.asarray(mask, np.float32)[:, :, None] / 255.
                y0, x0, y1, x1 = max(0, y), max(0, x), min(42, y + mask.height), min(120, x + mask.width)
                if y1 > y0 and x1 > x0:
                    a = alpha[y0-y:y1-y, x0-x:x1-x]
                    reg = expected[y0:y1, x0:x1].astype(np.float32)
                    expected[y0:y1, x0:x1] = (reg + (np.array(th.TEXT, np.float32) - reg) * a + .5).astype(np.uint8)
                actual = Image.new('RGB', (120, 42), (19, 27, 130))
                glyphs.draw(actual, xy, text, font, th.TEXT, anchor)
                self.assertTrue(np.array_equal(np.asarray(actual), expected))

    def test_mask_cache_is_lru_and_bounded(self):
        glyphs = Glyphs()
        glyphs.LIMIT = 2
        glyphs.BYTE_LIMIT = 1000
        font = th.Fonts().get(11)
        glyphs.mask('A', font, 'la')
        glyphs.mask('B', font, 'la')
        glyphs.mask('A', font, 'la')
        glyphs.mask('C', font, 'la')
        self.assertEqual([key[0] for key in glyphs.masks], ['A', 'C'])
        self.assertEqual(glyphs.mask_bytes, sum(x[2].width * x[2].height for x in glyphs.masks.values()))
        self.assertLessEqual(glyphs.mask_bytes, glyphs.BYTE_LIMIT)

    def test_stripe_column_preserves_gaps_singletons_and_saturation(self):
        from PIL import ImageDraw
        from r33.profile_live import ROW_FIELDS
        renderer = PageRenderer()
        renderer._text = lambda *args, **kwargs: None
        rng = np.random.default_rng(17)
        for pattern in ('zero', 'full', 'fragmented'):
            data = np.zeros((480, 16), np.uint32)
            for name, high in (('preview_max_peak', 320), ('preview_max_width', 40)):
                v = rng.integers(0, high, 480, dtype=np.uint32)
                if pattern == 'zero':
                    v[:] = 0
                elif pattern == 'fragmented':
                    v[rng.random(480) < .55] = 0
                data[:, ROW_FIELDS.index(name)] = v
            data[:, ROW_FIELDS.index('sat250_count')] = rng.integers(0, 2, 480)
            old, new = Image.new('RGB', (160, 430), th.PANEL), Image.new('RGB', (160, 430), th.PANEL)
            d = ImageDraw.Draw(old)
            yy = np.arange(480) * 408 / 480
            for a, b, name, full, colour in ((4, 64, 'preview_max_peak', 255., th.ACCENT),
                                              (72, 132, 'preview_max_width', 24., th.CENTRE)):
                d.line((a, 0, a, 408), fill=th.GRID)
                v = data[:, ROW_FIELDS.index(name)].astype(float)
                pts = [(a + min(1., val / full) * (b - a), y) for val, y in zip(v, yy)]
                run = []
                for point, val in zip(pts, v):
                    if val > 0:
                        run.append(point)
                    elif len(run) > 1:
                        d.line(run, fill=colour)
                        run = []
                    else:
                        run = []
                if len(run) > 1:
                    d.line(run, fill=colour)
            for y, sat in zip(yy, data[:, ROW_FIELDS.index('sat250_count')]):
                if sat:
                    d.line((146, y, 156, y), fill=th.NG)
            renderer._stripe_column(ImageDraw.Draw(new), (0, 0, 160, 408), data)
            self.assertTrue(np.array_equal(np.asarray(old), np.asarray(new)), pattern)

    def test_compositor_does_not_repeat_camera_conversion_on_redraw(self):
        fixture = fixtures.PageTests()
        fixture.setUp()
        fixture.feed()
        gray, centres = fixture.ps.live['gray'], fixture.ps.live['centres']
        comp = PageCompositor(PageRenderer())
        with patch('r33.ui_pages.camera_rgb', wraps=camera_rgb) as draw:
            first = comp.frame('A', lambda: fixture.ps.build('A'), gray, centres)
            self.assertEqual(draw.call_count, 1)
            before = first.copy()
            second = comp.frame('A', lambda: fixture.ps.build('A'), gray_with_line(500), centres)
            self.assertEqual(draw.call_count, 2)
        self.assertIs(first, comp.base)
        self.assertFalse(first.flags.writeable or second.flags.writeable)
        self.assertTrue(np.array_equal(first, before))


if __name__ == '__main__':
    unittest.main()
