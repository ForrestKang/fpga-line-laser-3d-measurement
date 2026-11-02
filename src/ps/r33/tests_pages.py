"""HDMI pages: live-data state, rendering, compositor and font coverage; no hardware."""
import types
import unittest
from pathlib import Path

import numpy as np

from . import ui_theme as th
from .page_state import PageState, diameter_bands
from .profile_live import ProfileConfig, ProfileTracker, SectionBasis, assess_quality, measure_frame
from .tests_profile_r41 import PLANE, rows, stepped, to_camera
from .ui_pages import CAMERA_BOX, PL_WINDOWS, PageCompositor, PageRenderer

FONTS = Path(__file__).resolve().parents[1] / 'assets/fonts'


def plain_rows(r):
    return np.column_stack([r[name] for name in r.dtype.names]).astype(np.uint32)


def gray_with_line(column):
    g = np.full((480, 752), 12, np.uint8)
    g[60:420, column - 2:column + 3] = 200
    return g


class PageTests(unittest.TestCase):
    def setUp(self):
        self.axis = types.SimpleNamespace(axis_point_mm=[0., 0., 300.], axis_direction=[0., -1., 0.])
        self.ps = PageState(run_name='scan_test', binned=True, motor_allowed=False,
                            motor_power_disconnected=True, turntable=self.axis)
        self.renderer = PageRenderer()
        self.r = plain_rows(rows())
        self.centres = np.column_stack((np.arange(60, 420), np.full(360, 300)))

    def feed(self, n=1, column=300):
        for i in range(n):
            self.ps.frame(number=i + 1, fps=12.5, capture_ms=60., frame_ms=70. + i, cycles=448813,
                          gray=gray_with_line(column), centres=self.centres, rows=self.r, points=340,
                          quality=assess_quality(self.r, 340))

    def test_live_page_leaves_pl_window_to_the_fpga(self):
        self.feed(3)
        out = self.renderer.render('A', self.ps.build('A', cloud_frames=17))
        self.assertEqual(out.shape, (720, 1280, 3))
        x, y = PL_WINDOWS['A']
        window = out[y:y + th.PL_WINDOW[1], x:x + th.PL_WINDOW[0]]
        self.assertTrue((window == th.PANEL).all())
        self.assertAlmostEqual(self.ps.live['pl_hw_ms'], 4.48813)

    def test_compositor_patches_camera_every_frame_and_redraws_on_period(self):
        clock = [0.]
        comp = PageCompositor(self.renderer, refresh_s=1.0)
        calls = []
        self.feed()

        def state():
            calls.append(1)
            return self.ps.build('A')
        import time
        real = time.monotonic
        time.monotonic = lambda: clock[0]
        try:
            a = comp.frame('A', state, gray_with_line(300), self.centres)
            clock[0] = .3
            moved = np.column_stack((np.arange(60, 420), np.full(360, 500)))
            b = comp.frame('A', state, gray_with_line(500), moved)
            clock[0] = 1.2
            comp.frame('A', state, gray_with_line(500), moved)
            c = comp.frame('B', lambda: None)
        finally:
            time.monotonic = real
        self.assertEqual(len(calls), 2)
        self.assertFalse(a.flags.writeable or b.flags.writeable)
        x0, y0, x1, y1 = CAMERA_BOX
        self.assertFalse(np.array_equal(a[y0:y1, x0:x1], b[y0:y1, x0:x1]))
        self.assertTrue(np.array_equal(a[:y0], b[:y0]))
        self.assertIsNotNone(c)          # page without data keeps the last image

    def test_section_page_from_measured_profile(self):
        b, cfg = SectionBasis(PLANE), ProfileConfig(step_nominal_mm=2.5, step_tol_mm=.2)
        tracker = ProfileTracker(cfg)
        r = rows()
        for seed in range(8):
            f = measure_frame(to_camera(b, stepped(noise=.02, seed=seed)), b, cfg)
            summary = tracker.update(f, assess_quality(r, f['points']))
            self.feed()
            self.ps.profile(f, summary, cfg, gray_with_line(300), self.centres)
        sec = self.ps.section
        self.assertEqual(sec['items'][0]['name'], '主台阶')
        self.assertEqual(sec['items'][0]['verdict'], 'PASS')
        self.assertEqual(len(sec['trend']), 8)
        out = self.renderer.render('B', self.ps.build('B'))
        self.assertEqual(out.shape, (720, 1280, 3))

    def test_empty_section_does_not_fail(self):
        b, cfg = SectionBasis(PLANE), ProfileConfig()
        f = measure_frame(np.zeros((0, 3)), b, cfg)
        summary = ProfileTracker(cfg).update(f, assess_quality(rows(valid=[], seen=[]), 0))
        self.feed()
        self.ps.profile(f, summary, cfg, gray_with_line(300), np.zeros((0, 2), int))
        self.renderer.render('B', self.ps.build('B'))
        self.assertEqual(self.ps.build('B')['alert'][1], 'bad')

    def test_scan_page_bands_and_failure_alert(self):
        angle = np.linspace(0, 2 * np.pi, 400)
        blocks = []
        for h in range(0, 40, 2):
            radius = 10 + h / 4
            blocks.append(np.column_stack((radius * np.cos(angle), -h * np.ones_like(angle),
                                           300 + radius * np.sin(angle), np.ones_like(angle))))
        bands = diameter_bands(np.concatenate(blocks)[:, :3], self.axis.axis_point_mm, self.axis.axis_direction)
        self.assertTrue(bands)
        for z, dia, _ in bands:
            self.assertAlmostEqual(dia, 2 * (10 + z / 4), delta=.6)
        self.feed()
        self.ps.scan(dict(state='SCANNING', views=50, target_views=200, angle_deg=90.), blocks)
        st = self.ps.build('C')
        self.assertTrue(st['scan']['running'])
        self.renderer.render('C', st)
        self.ps.scan(dict(state='FAILED', views=60, target_views=200, angle_deg=108.), blocks)
        st = self.ps.build('C')
        self.assertEqual(st['alert'][1], 'bad')
        self.assertIn(('电机', '故障', th.NG), st['devices'])
        out = self.renderer.render('C', st)
        x, y = PL_WINDOWS['C']
        self.assertTrue((out[y:y + 480, x:x + 448] == th.PANEL).all())

    def test_bundled_fonts_cover_every_page_character(self):
        import sys

        from fontTools.ttLib import TTFont
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
        try:
            from build_page_fonts import page_chars
        except ImportError:
            self.skipTest('font build script not beside the runtime')
        cmap = TTFont(str(FONTS / 'NotoSansCJKsc-pages-subset.otf')).getBestCmap()
        missing = [c for c in page_chars() if ord(c) not in cmap and not c.isspace()]
        self.assertEqual(missing, [])
        self.assertEqual(Path(th.Fonts().cjk).name, 'NotoSansCJKsc-pages-subset.otf')


if __name__ == '__main__':
    unittest.main()
