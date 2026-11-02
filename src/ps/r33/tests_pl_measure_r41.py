"""R41 PL measurement driver: register window guard, configuration writes and mm conversion."""
import math
import unittest

from .pl_measure import MAGIC, PLMeasure


class FakeMMIO:
    def __init__(self, base, size):
        self.regs = {0x500: MAGIC}
        self.writes = []

    def read(self, off):
        return self.regs.get(off, 0)

    def write(self, off, value):
        self.writes.append((off, value))
        self.regs[off] = value


class PLMeasureTests(unittest.TestCase):
    def setUp(self):
        self.fake = None

        def factory(base, size):
            self.fake = FakeMMIO(base, size)
            return self.fake
        self.m = PLMeasure(factory, 0x40000000)

    def test_absent_block_is_rejected(self):
        with self.assertRaises(RuntimeError):
            PLMeasure(lambda b, s: type('Z', (), dict(read=lambda self, o: 0))(), 0)

    def test_configure_disables_first_and_stays_in_window(self):
        cfg = self.m.configure(preview=True, z_origin_mm=100., z_shift=18)
        self.assertEqual(self.fake.writes[0], (0x504, 0))
        self.assertEqual(self.fake.writes[-1], (0x504, 3))
        self.assertTrue(all(0x500 <= o < 0x600 for o, _ in self.fake.writes))
        self.assertEqual(self.fake.regs[0x508], 100 * 65536)
        self.assertEqual(cfg['z_bin_mm'], 4.0)
        with self.assertRaises(ValueError):
            self.m.configure(z_shift=30)

    def test_frame_conversion(self):
        self.m.configure(preview=False, z_origin_mm=200., z_shift=18)
        r = self.fake.regs
        n, z = 4, [250, 260, 270, 280]
        zsum = sum(v * 65536 for v in z)
        r.update({0x520: 7, 0x524: n, 0x528: 250 * 65536, 0x52C: 280 * 65536, 0x530: zsum & 0xffffffff,
                  0x534: zsum >> 32, 0x538: (1 << 31) | 15, 0x53C: n,
                  0x548: (-10 * 65536) & 0xffffffff, 0x54C: 30 * 65536, 0x550: (-5 * 65536) & 0xffffffff,
                  0x554: 5 * 65536, 0x558: 0, 0x55C: 40 * 65536})
        f = self.m.frame()
        self.assertEqual((f['packets'], f['points']), (7, 4))
        self.assertAlmostEqual(f['z_mean_mm'], 265.0)
        self.assertAlmostEqual(f['z_median_mm'], 200 + 15.5 * 4)
        self.assertEqual((f['x_min_mm'], f['x_max_mm'], f['y_min_mm']), (-10, 30, -5))
        r.update({0x564: 2, 0x568: (50 * 65536) ** 2 & 0xffffffff, 0x56C: (50 * 65536) ** 2 >> 32,
                  0x570: (30 * 65536) ** 2 & 0xffffffff, 0x574: (30 * 65536) ** 2 >> 32,
                  0x578: 2 * (40 * 65536) ** 2 & 0xffffffff, 0x57C: 2 * (40 * 65536) ** 2 >> 32})
        band = self.m.frame(bands=True)['bands'][0]   # fake returns the same entry for every band
        self.assertAlmostEqual(band['r_max_mm'], 50)
        self.assertAlmostEqual(band['r_min_mm'], 30)
        self.assertTrue(math.isclose(band['r_rms_mm'], 40))

    def test_empty_packet_has_no_values(self):
        self.m.configure()
        self.assertEqual(set(self.m.frame()), {'packets', 'points', 'dropped_busy'})


if __name__ == '__main__':
    unittest.main()
