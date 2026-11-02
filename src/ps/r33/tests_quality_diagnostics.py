"""PL quality is the normal source; scanning Raw requires explicit diagnostics."""
import types
import unittest
from unittest.mock import patch

import numpy as np

from . import live


class UnreadableRaw:
    def __array__(self, *args, **kwargs):
        raise AssertionError('normal quality must not convert Raw')

    def __ge__(self, other):
        raise AssertionError('normal quality must not scan Raw')


def frame(decisions=3):
    rows = np.zeros((4, 16), np.uint32)
    rows[:, 2] = [0, 2, 3, 0]
    rows[:, 6] = [0, 1, 1, 0]
    rows[:, 12] = [0, 0, 2, 0]
    return types.SimpleNamespace(rows=rows, header=dict(
        frame_decisions=decisions, measurement_count=7))


class QualityDiagnosticsTests(unittest.TestCase):
    def test_default_uses_pl_and_never_reads_raw(self):
        quality = live.measurement_quality(frame(), UnreadableRaw())
        self.assertIsNone(quality['raw_sat250'])
        self.assertFalse(quality['raw_quality_diagnostic'])
        self.assertEqual(quality['pl_sat250'], 5)
        self.assertEqual(quality['measurement_rows'], 2)
        self.assertEqual(quality['ambiguous_rows'], 1)
        self.assertEqual(quality['local_measurement_count'], 7)

    def test_default_needs_no_raw_buffer(self):
        quality = live.measurement_quality(frame())
        self.assertIsNone(quality['raw_sat250'])
        self.assertTrue(quality['measurement_frame_usable'])
        self.assertTrue(quality['preview_frame_usable'])

    def test_explicit_diagnostic_preserves_ge250_semantics(self):
        gray = np.array([[0, 249, 250, 254, 255]], np.uint8)
        quality = live.measurement_quality(frame(), gray, raw_diagnostics=True)
        self.assertEqual(quality['raw_sat250'], 3)
        self.assertTrue(quality['raw_quality_diagnostic'])
        self.assertEqual(quality['pl_sat250'], 5)

    def test_diagnostic_requires_raw(self):
        with self.assertRaisesRegex(ValueError, 'Raw'):
            live.measurement_quality(frame(), raw_diagnostics=True)

    def test_diagnostic_cannot_change_pl_acceptance(self):
        for decisions in (0, 1, 2, 3):
            normal = live.measurement_quality(frame(decisions))
            diagnostic = live.measurement_quality(frame(decisions),
                np.full((480, 752), 255, np.uint8), raw_diagnostics=True)
            for name in ('frame_decisions', 'measurement_frame_usable',
                         'preview_frame_usable', 'measurement_rows',
                         'local_measurement_count', 'ambiguous_rows', 'pl_sat250'):
                self.assertEqual(normal[name], diagnostic[name], name)

    def test_missing_protocol_decisions_remains_rejected(self):
        result = frame()
        del result.header['frame_decisions']
        with self.assertRaises(KeyError):
            live.measurement_quality(result, UnreadableRaw())

    def test_missing_protocol_count_remains_rejected(self):
        result = frame()
        del result.header['measurement_count']
        with self.assertRaises(KeyError):
            live.measurement_quality(result, UnreadableRaw())

    def test_live_default_does_not_pass_raw_to_quality(self):
        from .tests_live_r42 import LiveIntegrationTests
        actual = live.measurement_quality
        calls = []
        def quality(result, gray=None, *, raw_diagnostics=False):
            self.assertIsNone(gray)
            self.assertFalse(raw_diagnostics)
            calls.append(True)
            return actual(result, gray, raw_diagnostics=raw_diagnostics)
        with patch.object(live, 'measurement_quality', quality):
            rc, state, *_ = LiveIntegrationTests(methodName='runTest').run_live(allow_motor=False)
        self.assertEqual(rc, 0, state.get('error'))
        self.assertTrue(calls)
        self.assertFalse(state['raw_quality_diagnostic'])
        self.assertIsNone(state['measurement_quality']['raw_sat250'])

    def test_live_diagnostic_flag_is_explicit_and_recorded(self):
        from .tests_live_r42 import LiveIntegrationTests
        actual_main, actual_quality = live.main, live.measurement_quality
        calls = []
        def main(argv):
            return actual_main([*argv, '--diagnostic-raw-quality'])
        def quality(result, gray=None, *, raw_diagnostics=False):
            self.assertIsNotNone(gray)
            self.assertTrue(raw_diagnostics)
            calls.append(True)
            return actual_quality(result, gray, raw_diagnostics=raw_diagnostics)
        with patch.object(live, 'main', main), patch.object(live, 'measurement_quality', quality):
            rc, state, *_ = LiveIntegrationTests(methodName='runTest').run_live(allow_motor=False)
        self.assertEqual(rc, 0, state.get('error'))
        self.assertTrue(calls)
        self.assertTrue(state['raw_quality_diagnostic'])
        self.assertEqual(state['measurement_quality']['raw_sat250'], 0)


if __name__ == '__main__':
    unittest.main()
