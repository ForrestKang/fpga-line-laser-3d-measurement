"""R40 display hold option: CLI parse and early bounds check (before any hardware import)."""
import sys
import types
import unittest
from unittest.mock import patch

from . import replay


class DisplayHoldTests(unittest.TestCase):
    def test_cli_default_and_value(self):
        seen = []
        base = ['replay.py', '--manifest', 'm', '--input-root', 'i', '--output', 'o', '--target', 'T06']
        with patch.object(replay, 'run', side_effect=seen.append):
            with patch.object(sys, 'argv', base):
                replay.main()
            with patch.object(sys, 'argv', base + ['--display-hold-s', '180']):
                replay.main()
        self.assertEqual(seen[0].display_hold_s, 0.)
        self.assertEqual(seen[1].display_hold_s, 180.)

    def test_out_of_range_rejected_before_hardware(self):
        # pynq is imported after the option checks; a ValueError proves nothing was touched.
        for bad in (-1., replay.DISPLAY_HOLD_MAX_S + 1., '60'):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, 'display hold'):
                    replay.run(types.SimpleNamespace(display_hold_s=bad))

    def test_limit_fits_harness_timeout(self):
        # run_board_replay38.py execs with --timeout 600; a 240-frame run takes ~110 s.
        self.assertLessEqual(replay.DISPLAY_HOLD_MAX_S + 120 + replay.DISPLAY_CLOSE_TIMEOUT_S, 600)


if __name__ == '__main__':
    unittest.main()
