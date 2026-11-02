"""R41 fixed-point profile: step/gap geometry, quality levels, tracker judgement and panel."""
import json
import math
import unittest
from pathlib import Path

import numpy as np

from .profile_live import (ProfileConfig, ProfileTracker, QualityLimits, SectionBasis, assess_quality,
                           log_view, measure_frame)

ROOT = Path(__file__).resolve().parents[1]
PLANE = json.loads((ROOT / 'calibration_r41' / 'laser_plane_r41.json').read_text())['plane_coefficients']
ROW_FIELDS = ('row raw_peak sat250_count sat255_count raw_candidate_count qualified_count '
              'measurement_count preview_count measurement_max_width preview_max_width measurement_max_peak '
              'preview_max_peak measurement_ambiguous preview_ambiguous reserved0 reserved1').split()


def to_camera(basis, sh):
    sh = np.asarray(sh, float)
    return basis.origin + sh[:, :1] * basis.es + sh[:, 1:] * basis.eh


def stepped(slope=.1, step=2.5, gap=(-5., -2.), noise=0., seed=1, groove_depth=None):
    rng = np.random.default_rng(seed)
    s = np.arange(-40, 30.001, .25)
    keep = ~((s > gap[0]) & (s < gap[1]))
    if groove_depth is not None:
        keep = np.ones_like(s, bool)
    h = slope * s + np.where(s > 5, step, 0.)
    if groove_depth is not None:
        h = np.where((s > gap[0]) & (s < gap[1]), h - groove_depth, h)
    s, h = s[keep], h[keep] + noise * rng.standard_normal(keep.sum())
    return np.column_stack((s, h))


def rows(valid=range(60, 420), peak=180, width=8, sat=0, ambiguous=(), seen=range(60, 420)):
    r = np.zeros(480, dtype=[(f, '<u4') for f in ROW_FIELDS])
    r['row'] = np.arange(480)
    v = np.zeros(480, bool)
    v[list(valid)] = True
    r['preview_count'][v] = 1
    r['qualified_count'][list(seen)] = 1
    r['preview_max_peak'][v] = peak
    r['preview_max_width'][v] = width
    r['sat250_count'][v] = sat
    r['preview_ambiguous'][list(ambiguous)] = 1
    return r


class BasisTests(unittest.TestCase):
    def test_basis_is_orthonormal_in_plane_and_faces_camera(self):
        b = SectionBasis(PLANE)
        self.assertAlmostEqual(float(b.es @ b.eh), 0, places=12)
        self.assertAlmostEqual(float(b.n @ b.es), 0, places=12)
        self.assertAlmostEqual(float(b.n @ b.eh), 0, places=12)
        self.assertLess(b.eh[2], 0)
        p = to_camera(b, [[3., 4.]])
        self.assertAlmostEqual(float(b.n @ p[0] + b.d), 0, places=9)
        np.testing.assert_allclose(b.project(p), [[3., 4.]], atol=1e-9)


class MeasureTests(unittest.TestCase):
    def setUp(self):
        self.b, self.cfg = SectionBasis(PLANE), ProfileConfig()

    def test_step_and_gap_exact(self):
        f = measure_frame(to_camera(self.b, stepped()), self.b, self.cfg)
        self.assertAlmostEqual(f['step_mm'], 2.5 / math.sqrt(1.01), places=6)
        self.assertAlmostEqual(f['gap_mm'], math.hypot(3, .3), places=6)
        self.assertIsNone(f['gap_depth_mm'])
        self.assertLess(f['step_angle_deg'], 1e-6)

    def test_lower_step_is_negative(self):
        f = measure_frame(to_camera(self.b, stepped(step=-1.2, slope=0)), self.b, self.cfg)
        self.assertAlmostEqual(f['step_mm'], -1.2, places=6)

    def test_noise(self):
        f = measure_frame(to_camera(self.b, stepped(noise=.03)), self.b, self.cfg)
        self.assertAlmostEqual(f['step_mm'], 2.5 / math.sqrt(1.01), delta=.02)
        self.assertAlmostEqual(f['gap_mm'], math.hypot(3, .3), delta=.3)

    def test_groove_with_bottom_returns_depth(self):
        f = measure_frame(to_camera(self.b, stepped(slope=0, groove_depth=1.5, step=3)), self.b, self.cfg)
        self.assertAlmostEqual(f['gap_depth_mm'], 1.5, places=6)
        self.assertAlmostEqual(f['gap_mm'], 3.0, places=6)    # top edge samples at s = -5 and -2

    def test_flat_has_no_step_or_gap(self):
        s = np.arange(-30, 30, .25)
        f = measure_frame(to_camera(self.b, np.column_stack((s, .05 * s))), self.b, self.cfg)
        self.assertEqual(f['segments'], 1)
        self.assertIsNone(f['step_mm'])
        self.assertIsNone(f['gap_mm'])

    def test_empty(self):
        f = measure_frame(np.zeros((0, 3)), self.b, self.cfg)
        self.assertEqual((f['points'], f['segments']), (0, 0))


class QualityTests(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(assess_quality(rows(), 300)['level'], 'GOOD')
        q = assess_quality(rows(sat=2), 300)
        self.assertEqual(q['level'], 'BAD')
        self.assertIn('saturated', [h['code'] for h in q['hints']])
        self.assertEqual(assess_quality(rows(peak=40), 300)['level'], 'WARN')
        self.assertEqual(assess_quality(rows(width=30), 300)['level'], 'WARN')
        holes = [r for r in range(60, 420) if r % 3]
        self.assertIn('incomplete', [h['code'] for h in assess_quality(rows(valid=holes), 240)['hints']])
        q = assess_quality(rows(valid=range(60, 420, 3)), 120)
        self.assertEqual((q['level'], [h['code'] for h in q['hints']]), ('WARN', ['broken']))
        # rows where the detector saw no stripe light (occlusion) are not missing rows
        q = assess_quality(rows(valid=list(range(0, 100)) + list(range(300, 400)),
                                seen=list(range(0, 100)) + list(range(300, 400))), 200)
        self.assertEqual(q['level'], 'GOOD')
        self.assertEqual(assess_quality(rows(), 300, row_window=(100, 199))['metrics']['valid_rows'], 100)
        self.assertEqual(assess_quality(rows(valid=()), 0)['level'], 'BAD')
        self.assertEqual(assess_quality(rows(), 10)['level'], 'BAD')
        q = assess_quality(rows(ambiguous=range(60, 100)), 300)
        self.assertIn('ambiguous', [h['code'] for h in q['hints']])
        self.assertTrue(all(h['zh'] and h['en'] for h in q['hints']))


class WindowTests(unittest.TestCase):
    def test_auto_window_drops_far_background_and_explicit_rows(self):
        b, cfg = SectionBasis(PLANE), ProfileConfig()
        near = to_camera(b, stepped())
        s = np.arange(-60, 60, .5)
        wall = to_camera(b, np.column_stack((s, np.full_like(s, -900.))))
        self.assertGreater(wall[:, 2].min() - near[:, 2].max(), 300)
        f = measure_frame(np.vstack((near, wall)), b, cfg)
        self.assertEqual(f['window']['points_out'], len(wall))
        self.assertAlmostEqual(f['step_mm'], 2.5 / math.sqrt(1.01), places=6)
        rows_ = np.r_[np.arange(len(near)), 1000 + np.arange(len(wall))] % 480
        f = measure_frame(np.vstack((near, wall)), b, ProfileConfig(auto_depth_mm=0, rows=(0, 99)), rows_)
        self.assertEqual(f['window']['rows'], (0, 99))
        self.assertLessEqual(f['points'], 100 + len(wall))


class TrackerTests(unittest.TestCase):
    def frames(self, cfg, n, **kw):
        b = SectionBasis(PLANE)
        t = ProfileTracker(cfg)
        out = None
        for i in range(n):
            out = t.update(measure_frame(to_camera(b, stepped(noise=.02, seed=i, **kw)), b, cfg),
                           assess_quality(rows(), 300))
        return t, out

    def test_pass_fail_and_wait(self):
        exp = 2.5 / math.sqrt(1.01)
        cfg = ProfileConfig(step_nominal_mm=exp, step_tol_mm=.1, gap_nominal_mm=2.0, gap_tol_mm=.2)
        _, first = self.frames(cfg, 2)
        self.assertEqual(first['step']['status'], 'WAIT')
        _, out = self.frames(cfg, 9)
        self.assertEqual(out['step']['status'], 'PASS')
        self.assertEqual(out['gap']['status'], 'FAIL')
        self.assertTrue(json.dumps(log_view(out)))

    def test_bad_quality_frames_are_dropped(self):
        b, cfg = SectionBasis(PLANE), ProfileConfig()
        t = ProfileTracker(cfg)
        f = measure_frame(to_camera(b, stepped()), b, cfg)
        for _ in range(4):
            out = t.update(f, assess_quality(rows(sat=5), 300))
        self.assertEqual(out['dropped'], 'quality')
        self.assertEqual(out['counts']['rejected_quality'], 4)
        self.assertEqual(len(t.values['step']), 0)

    def test_outlier_rejected(self):
        b, cfg = SectionBasis(PLANE), ProfileConfig()
        t, _ = self.frames(cfg, 6)
        out = t.update(measure_frame(to_camera(b, stepped(step=4.)), b, cfg), assess_quality(rows(), 300))
        self.assertEqual(out.get('outlier'), ['step'])

    def test_change_detection(self):
        b, cfg = SectionBasis(PLANE), ProfileConfig(reference_frames=3)
        t = ProfileTracker(cfg)
        base = stepped(slope=0)
        for _ in range(3):
            out = t.update(measure_frame(to_camera(b, base), b, cfg), assess_quality(rows(), 300))
        self.assertEqual(out['change']['status'], 'LEARNING')
        out = t.update(measure_frame(to_camera(b, base), b, cfg), assess_quality(rows(), 300))
        self.assertEqual(out['change']['status'], 'SAME')
        bumped = base.copy()
        bumped[(bumped[:, 0] > -30) & (bumped[:, 0] < -25), 1] += .8
        out = t.update(measure_frame(to_camera(b, bumped), b, cfg), assess_quality(rows(), 300))
        self.assertEqual(out['change']['status'], 'CHANGED')
        self.assertAlmostEqual(out['change']['max_dev_mm'], .8, places=6)
        self.assertAlmostEqual(out['change']['threshold_mm'], .5)


class LiveArrayFormTests(unittest.TestCase):
    """The live R33 path hands rows/records over as plain 2-D uint32 arrays."""

    def test_rows_2d_equal_structured(self):
        r = rows(sat=1, ambiguous=range(60, 80))
        flat = r.view('<u4').reshape(480, 16)
        self.assertEqual(assess_quality(flat, 300, row_window=(50, 300)), assess_quality(r, 300, row_window=(50, 300)))

    def test_point_rows_from_records(self):
        from .profile_live import point_rows_from
        rec = np.zeros((5, 16), np.uint32)
        rec[:, 0] = np.arange(5)
        rec[:, 1] = [10, 11, 12, 13, 14]
        np.testing.assert_array_equal(point_rows_from(rec, [4, 0, 2]), [14, 10, 12])
        self.assertIsNone(point_rows_from(rec, [7]))
        self.assertEqual(len(point_rows_from(np.zeros((0, 16), np.uint32), [])), 0)


class PanelTests(unittest.TestCase):
    def test_render_shapes_and_chinese_font(self):
        from .profile_panel import CARD_H, PLOT_H, PLOT_W, ProfilePanel, blit
        b, cfg = SectionBasis(PLANE), ProfileConfig(step_nominal_mm=2.5, gap_nominal_mm=3.)
        t = ProfileTracker(cfg)
        for i in range(12):
            out = t.update(measure_frame(to_camera(b, stepped(noise=.02, seed=i)), b, cfg),
                           assess_quality(rows(), 300))
        p = ProfilePanel()
        self.assertTrue(p.chinese)
        plot, cards = p.plot(out, t.reference), p.cards(out)
        self.assertEqual(plot.shape, (PLOT_H, PLOT_W, 3))
        self.assertEqual(cards.shape, (CARD_H, 1280, 3))
        frame = np.zeros((720, 1280, 4), np.uint8)
        blit(frame, plot, 120, 800)
        self.assertTrue(frame[120:600, 800:1256, :3].any())
        self.assertFalse(frame[:, :, 3].any())


if __name__ == '__main__':
    unittest.main()
