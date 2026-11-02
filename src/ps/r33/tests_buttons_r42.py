import unittest

from .buttons import BASE, MAGIC, Buttons, Pages
from .pl_view import PLCloudView, CTRL_AUTO, CTRL_CLEAR, CTRL_FREEZE, CTRL_INSERT, CTRL_OVERLAY, CTRL_PREVIEW


class MockMMIO:
    def __init__(self, *_):
        self.regs = {BASE: MAGIC, BASE + 8: 0, 0x468: 0x52343056}
        self.reads, self.writes = [], []

    def read(self, offset):
        self.reads.append(offset)
        return self.regs.get(offset, 0)

    def write(self, offset, value):
        self.writes.append((offset, value))
        self.regs[offset] = value


class ButtonTests(unittest.TestCase):
    def setUp(self):
        self.manifest = dict(buttons=dict(present=True, register_window=[0x600, 0x6ff], magic=MAGIC))
        self.mmio = MockMMIO()

    def test_absent_or_bad_manifest_never_creates_mmio(self):
        for manifest in ({}, dict(buttons=dict(present=True)), dict(buttons=dict(present=False))):
            with self.assertRaises(ValueError):
                Buttons(manifest, lambda *_: self.fail('MMIO must not be probed'), 0)

    def test_magic_mismatch(self):
        self.mmio.regs[BASE] = 0
        with self.assertRaises(RuntimeError):
            Buttons(self.manifest, lambda *_: self.mmio, 0)

    def test_baseline_events_wrap_and_read_only(self):
        self.mmio.regs[BASE + 8] = 0xfefdfcfa
        buttons = Buttons(self.manifest, lambda *_: self.mmio, 0)
        self.assertEqual(buttons.poll(), (0, 0, 0, 0))
        self.mmio.regs[BASE + 8] = 0x010101ff
        self.mmio.regs[BASE + 4] = 15
        self.assertEqual(buttons.poll(), (5, 5, 4, 3))
        self.assertEqual(buttons.level, 15)
        self.assertEqual(self.mmio.writes, [])
        self.assertTrue(set(self.mmio.reads) <= {0x600, 0x604, 0x608})

    def test_direct_pages_mapping_and_disabled_page(self):
        pages = Pages()
        self.assertTrue(pages.poll((0, 0, 1, 0)))
        self.assertEqual(pages.current, 'B')
        pages.poll((0, 0, 0, 4))
        self.assertEqual(pages.current, 'C')
        pages.poll((0, 8, 0, 0))
        self.assertEqual(pages.current, 'A')
        limited = Pages(('A', 'B'))
        self.assertFalse(limited.poll((0, 0, 0, 1)))
        only = Pages(('A',))
        self.assertFalse(only.poll((0, 0, 1, 1)))
        custom = Pages(('A', 'C'), ('C', 'B', 'A'))
        custom.poll((0, 1, 0, 0))
        self.assertEqual(custom.current, 'C')

    def test_overlay_preserves_grid_ingest_preview_auto_and_freeze(self):
        view = PLCloudView(lambda *_: self.mmio, 0)
        ctrl = CTRL_INSERT | CTRL_AUTO | CTRL_PREVIEW | CTRL_FREEZE | CTRL_OVERLAY
        self.mmio.regs[0x400] = ctrl
        view.stop_overlay()
        self.assertEqual(self.mmio.writes[-1], (0x400, ctrl & ~CTRL_OVERLAY))
        view.set_overlay(True)
        self.assertEqual(self.mmio.writes[-1], (0x400, ctrl))
        self.assertFalse(any(value & CTRL_CLEAR for _, value in self.mmio.writes))


if __name__ == '__main__':
    unittest.main()
