"""Offline SECM MMIO tests: model-backed packets, no PYNQ or hardware access."""
import importlib.util
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from . import live
from .pl_section import (
    BASE,
    MAGIC,
    PLSection,
    backend_for,
    comparison,
    declared,
    profile_frame,
    signed,
)
from .profile_live import ProfileConfig, SectionBasis, measure_frame
from .tests_profile_r41 import PLANE, rows, stepped, to_camera

SPEC = importlib.util.spec_from_file_location('section_fixed_model', Path(__file__).resolve().parents[2]
                                            / 'model/section_measure_model.py')
import sys

MODEL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODEL
SPEC.loader.exec_module(MODEL)
MANIFEST = {'section_measure': {'present': True, 'register_window': [0x700, 0x7ff], 'magic': MAGIC, 'version': 1}}


class MMIO:
    def __init__(self, *_):
        self.regs, self.access, self.table = {BASE: MAGIC, 0x750: 0, 0x79c: 0}, [], []
        self.steps, self.selector = [], 0

    def read(self, offset):
        self.access.append(('r', offset))
        i, j = (self.selector >> 8) & 15, self.selector & 15
        if offset in (0x780, 0x784):
            return self.steps[j][(offset - 0x780) // 4] & 0xffffffff if j < len(self.steps) else 0
        if 0x788 <= offset <= 0x798:
            return self.table[i][(offset - 0x788) // 4] & 0xffffffff if i < len(self.table) else 0
        return self.regs.get(offset, 0)

    def write(self, offset, value):
        self.access.append(('w', offset))
        self.regs[offset] = value
        if offset == 0x744:
            self.selector = value

    def publish(self, xyz, flags=None):
        r = self.regs
        def q(offset): return signed(r[BASE + offset])
        cfg = MODEL.Config(tuple(q(o) for o in (8, 12, 16)), tuple(q(o) for o in (20, 24, 28)),
                           tuple(q(o) for o in (32, 36, 40)), z_min=q(44), z_max=q(48),
                           jump=q(52), brk=q(56), level=q(60), min_points=q(64), preview=bool(q(4) & 2))
        records = np.zeros((len(xyz), 8), np.int64)
        records[:, 1:4] = np.rint(np.asarray(xyz) * 65536).astype(np.int64) & 0xffffffff
        records[:, 7] = 0x103 if flags is None else flags
        out = MODEL.SectionMeasure(cfg).packet(records)
        segs = out['seg']
        status = ((out['step'] is not None) | ((out['gap'] is not None) << 1)
                  | ((out['depth'] is not None) << 2) | (bool(segs) << 3)
                  | ((out['reference'] or 0) << 8) | ((out['step_segment'] or 0) << 12)
                  | (len(out['steps']) << 16))
        edges = out['gap_edges'] or (0, 0)
        vals = [r[0x750] + 1, out['points'], (out['overflow'] << 31) | len(segs), status,
                out['step'] or 0, out['gap'] or 0, out['depth'] or 0, out['noise'], out['level'],
                out['ref_rms'] or 0, out['ref_slope'] or 0, out['spacing']]
        for offset, v in zip(range(0x750, 0x780, 4), vals):
            r[offset] = v & 0xffffffff
        r[0x79c] += out['dropped']
        r[0x7a0], r[0x7a4], r[0x7a8] = edges[0] & 0xffffffff, edges[1] & 0xffffffff, 12345
        self.steps = out['steps']
        self.table = [(g.n, g.s0, g.s1, out['offsets'][i], g.fit['a']) for i, g in enumerate(segs)]
        return out


class SectionTests(unittest.TestCase):
    def setUp(self):
        self.mmio = MMIO()
        self.driver = PLSection(MANIFEST, lambda *_: self.mmio, 0)
        self.basis, self.cfg = SectionBasis(PLANE), ProfileConfig()
        self.driver.configure(self.basis, self.cfg)

    def test_absence_never_constructs_mmio_and_explicit_fallback(self):
        factory = lambda *_: self.fail('MMIO must not be constructed')
        self.assertEqual(backend_for({}, None), 'arm')
        self.assertEqual(backend_for(MANIFEST, None), 'pl')
        self.assertEqual(backend_for(MANIFEST, 'arm'), 'arm')
        with self.assertRaises(RuntimeError): PLSection({}, factory, 0)
        with self.assertRaises(RuntimeError): backend_for({}, 'pl')

    def test_manifest_window_magic_version_fail_closed(self):
        for key, bad in (('magic', 0), ('version', 2), ('register_window', [0x700, 0x800])):
            with self.subTest(key=key), self.assertRaises(ValueError):
                declared({'section_measure': dict(MANIFEST['section_measure'], **{key: bad})})
        m = MMIO()
        m.regs[BASE] = 0
        with self.assertRaises(RuntimeError): PLSection(MANIFEST, lambda *_: m, 0)

    def test_guarded_window_and_configuration_units(self):
        for offset in (-4, 1, 0x100, True):
            with self.assertRaises(ValueError): self.driver._read(offset)
            with self.assertRaises(ValueError): self.driver._write(offset, 1)
        self.assertTrue(all(0x700 <= off <= 0x7ff for _, off in self.mmio.access))
        self.assertEqual(self.mmio.regs[0x734], round(.8 * 65536))
        np.testing.assert_allclose(np.array(self.driver.cfg['es_q24']) / 16777216, self.basis.es, atol=1e-7)
        with self.assertRaises(ValueError): self.driver.configure(self.basis, replace(self.cfg, rows=(0, 479)))

    def test_step_gap_and_reference_model_conversion(self):
        xyz = np.rint(to_camera(self.basis, stepped()) * 65536) / 65536
        expected = self.mmio.publish(xyz)
        got = self.driver.frame()
        self.assertAlmostEqual(got['step_mm'], expected['step'] / 65536)
        self.assertAlmostEqual(got['gap_mm'], expected['gap'] / 65536)
        self.assertEqual(len(got['steps']), len(expected['steps']))
        pf = profile_frame(got, xyz, self.basis, self.driver.effective_config())
        arm = measure_frame(xyz, self.basis, self.driver.effective_config())
        self.assertLess(abs(pf['step_mm'] - arm['step_mm']), .001)
        self.assertEqual(pf['reference_rms_mm'], expected['ref_rms'] / 65536)
        evidence = comparison(0, pf, arm, next_z_max_mm=None)
        self.assertEqual(evidence['packet'], 1)
        self.assertIn('arm_segments', evidence)

    def test_empty_timeout_identity_and_overflow(self):
        self.mmio.publish(np.zeros((0, 3)))
        got = self.driver.frame()
        self.assertEqual(got['points'], 0)
        self.assertIsNone(got['reference'])
        with self.assertRaises(TimeoutError): self.driver.frame(timeout_s=0)
        self.mmio.regs[0x750] += 2
        with self.assertRaises(RuntimeError): self.driver.frame(timeout_s=0)
        self.mmio.regs[0x750] -= 2
        xyz = to_camera(self.basis, np.column_stack((np.arange(1025) * .05, np.zeros(1025))))
        self.mmio.publish(xyz)
        got = self.driver.frame()
        self.assertEqual(got['dropped_delta'], 1)
        self.assertFalse(got['valid'])

    def test_previous_frame_window_and_grade_isolation(self):
        old = self.driver.effective_config().z_max_mm
        xyz = np.array([[0., 0., 100.], [0., 0., 110.], [0., 0., 1000.]])
        new = self.driver.next_window(xyz)
        self.assertEqual(new, np.floor((np.percentile(xyz[:, 2], 5) + 250.) * 65536) / 65536)
        self.assertLess(new, old)
        self.assertEqual(self.driver.effective_config().z_max_mm, new)
        self.assertIsNone(self.driver.next_window(np.zeros((0, 3))))
        self.driver.configure(self.basis, self.cfg, preview=False)
        self.mmio.publish(np.array([[0., 0., 100.], [0., 0., 101.]]), flags=[0x101, 0x102])
        self.assertEqual(self.driver.frame()['points'], 1)

    def test_window_mismatch_is_its_own_error(self):
        from .pl_section import SectionWindowMismatch
        xyz = np.rint(to_camera(self.basis, stepped()) * 65536) / 65536
        self.mmio.publish(xyz)
        got = self.driver.frame()
        got['points'] += 3                  # a result from a packet that used another window
        with self.assertRaises(SectionWindowMismatch):
            profile_frame(got, xyz, self.basis, self.driver.effective_config())

    def test_adapter_missing_reference_does_not_index_none(self):
        xyz = np.rint(to_camera(self.basis, stepped()) * 65536) / 65536
        self.mmio.publish(xyz)
        got = self.driver.frame()
        self.assertTrue(got['segment_list'])
        got.update(reference=None, reference_rms_mm=None, reference_slope=None, valid=False)
        pf = profile_frame(got, xyz, self.basis, self.driver.effective_config())
        self.assertTrue(all(g['rms'] is None for g in pf['segment_list']))
        self.assertFalse(pf['valid'])
        self.assertIsNone(pf['step_mm'])

    def test_acquisition_hole_does_not_publish_gap(self):
        xyz = np.rint(to_camera(self.basis, stepped()) * 65536) / 65536
        self.mmio.publish(xyz)
        got = self.driver.frame()
        # Make every intermediate image row a detected-but-rejected observation.
        prow = np.rint((stepped()[:, 0] + 40) * 4).astype(int)
        r = rows(valid=(), seen=range(480))
        pf = profile_frame(got, xyz, self.basis, self.driver.effective_config(), prow, r)
        self.assertTrue(pf['gap_unresolved'])
        self.assertIsNone(pf['gap_mm'])

    def test_page_uses_pl_rms_not_display_residual_std(self):
        from .page_state import section_view
        from .profile_live import ProfileTracker, assess_quality
        xyz = np.rint(to_camera(self.basis, stepped()) * 65536) / 65536
        self.mmio.publish(xyz)
        pf = profile_frame(self.driver.frame(), xyz, self.basis, self.driver.effective_config())
        pf['reference_rms_mm'] = .123
        summary = ProfileTracker(self.cfg).update(pf, assess_quality(rows(), pf['points']))
        view = section_view(pf, summary, self.cfg, [], np.zeros((480, 752), np.uint8), [])
        self.assertEqual(next(item['value'] for item in view['items'] if item['name'] == '参考面残差'), .123)

    def test_live_main_with_model_backed_pl_section_and_comparison(self):
        import r33.pl_section as module

        from .tests_live_r42 import LiveIntegrationTests
        original = live.main
        write = live.write_json
        saved = []
        saved_frames, saved_ui_events = [], []
        mmio = MMIO()
        driver = []
        committed = []
        class AutoSection(PLSection):
            def __init__(self, *_):
                super().__init__(MANIFEST, lambda *_: mmio, 0)
                driver.append(self)
        def publish(identity, geo):
            # Publish when the mock PL commits, not when software reads: this
            # exposes a forgotten warm-up packet and cross-packet races.
            if identity['frame_id'] == 1:
                self.assertEqual(driver[0].packet, 1)
            committed.append(identity['frame_id'])
            mmio.publish(geo[:, 1:4].view(np.int32).astype(np.float64) / 65536.)
        def main(args):
            return original(args + ['--pages', 'A,B,C', '--section-backend', 'pl', '--initial-page', 'B'])
        def save(path, value):
            write(path, value)
            if Path(path).name == 'frames.json':
                saved_frames.extend(value)
            if Path(path).name == 'ui_display_events.json':
                saved_ui_events.extend(value)
            if Path(path).name == 'summary.json':
                import json
                saved.extend(json.loads(line) for line in (Path(path).parent / 'section_compare.jsonl')
                             .read_text(encoding='utf-8').splitlines())
        with patch.object(live, 'main', main), patch.object(module, 'backend_for', lambda *_: 'pl'), \
                patch.object(module, 'PLSection', AutoSection), patch.object(live, 'write_json', save):
            rc, state, _, _, backend, _, _ = LiveIntegrationTests().run_live(
                allow_motor=False, on_pl_packet=publish)
        self.assertEqual(rc, 0, state.get('error'))
        self.assertEqual(state['section_backend'], 'pl')
        self.assertEqual(state['warmup']['section']['packets'], 1)
        self.assertEqual(state['warmup']['section']['input_frame_id'], 0)
        self.assertEqual(state['frames'] + 1, mmio.regs[0x750])
        self.assertEqual(committed, list(range(len(committed))))
        self.assertEqual(state['frames'], len(saved))
        self.assertEqual([r['packet'] for r in saved], list(range(2, len(saved) + 2)))
        self.assertIn('input_to_ack_ms', state['display_timing_ms'])
        self.assertIn('running_start_monotonic', state)
        for frame in saved_frames:
            self.assertLessEqual(frame['capture_started_monotonic'], frame['capture_ready_monotonic'])
            self.assertLessEqual(frame['capture_ready_monotonic'], frame['processed_monotonic'])
        for event in saved_ui_events:
            self.assertIn('ack_monotonic', event)
        self.assertEqual(mmio.regs[0x704], 0)
        self.assertEqual(backend.moves, [])

    def test_warmup_section_packet_jump_fails_before_formal_packet(self):
        import r33.pl_section as module

        from .tests_live_r42 import LiveIntegrationTests
        original = live.main
        mmio, committed = MMIO(), []
        class AutoSection(PLSection):
            def __init__(self, *_): super().__init__(MANIFEST, lambda *_: mmio, 0)
        def publish(identity, geo):
            committed.append(identity['frame_id'])
            xyz = geo[:, 1:4].view(np.int32).astype(np.float64) / 65536.
            mmio.publish(xyz)
            mmio.publish(xyz)  # two publications for one input must never be hidden
        def main(args):
            return original(args + ['--pages', 'A,B,C', '--section-backend', 'pl'])
        with patch.object(live, 'main', main), patch.object(module, 'backend_for', lambda *_: 'pl'), \
                patch.object(module, 'PLSection', AutoSection):
            rc, state, _, _, backend, _, _ = LiveIntegrationTests().run_live(
                allow_motor=False, on_pl_packet=publish)
        self.assertEqual(rc, 1)
        self.assertIn('section packet identity advanced unexpectedly', state['error'])
        self.assertEqual(committed, [0])
        self.assertEqual(backend.moves, [])
        self.assertEqual(mmio.regs[0x704], 0)


if __name__ == '__main__':
    unittest.main()
