"""Offline checks for PYNQ's collapsed multi-interface IP metadata. No board IO."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import xml.etree.ElementTree as ET

from .scope import PFO3ReplayScopeR31, _REGISTER_RANGES, PFO3Error


class RawMMIO:
    def read(self, offset):
        raise AssertionError("Scope construction must not read hardware")

    def write(self, offset, value):
        raise AssertionError("Scope construction must never write hardware")


class MultiInterfaceScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hwh = Path(self.temp.name) / "scope.hwh"
        self.ranges = dict(_REGISTER_RANGES)
        self.overlay = SimpleNamespace(ip_dict={
            name: {"phys_addr": base}
            for (name, _), (base, _) in self.ranges.items()
        })
        self.created = []
        self.write_hwh()

    def write_hwh(self):
        root = ET.Element("SYSTEM")
        for (name, interface), (base, high) in self.ranges.items():
            ET.SubElement(root, "MEMRANGE", MEMTYPE="REGISTER", INSTANCE=name,
                          SLAVEBUSINTERFACE=interface, BASEVALUE=hex(base),
                          HIGHVALUE=hex(high))
        ET.ElementTree(root).write(self.hwh, encoding="utf-8")

    def construct(self):
        def factory(base, length):
            self.created.append((base, length))
            return RawMMIO()
        return PFO3ReplayScopeR31(
            self.overlay, SimpleNamespace(acquired=True), factory,
            ctrl_raw=RawMMIO(), dma_raw=RawMMIO(), hwh_path=self.hwh)

    def test_both_legal_representatives_use_separate_verified_status_windows(self):
        for representative in (0x43C00000, 0x43C10000):
            with self.subTest(representative=hex(representative)):
                self.created.clear()
                self.overlay.ip_dict["processing_pipeline"]["phys_addr"] = representative
                scope = self.construct()
                self.assertEqual(self.created, [(0x40400000, 4096),
                    (0x43C00000, 4096),
                    (0x43C10000, 4096), (0x43C40000, 4096),
                    (0x43C50000, 4096)])
                with self.assertRaises(PFO3Error):
                    scope.diag.write(0x3C, 0)

    def test_wrong_pipeline_representative_rejected_before_mmio(self):
        for address in (0x43C00004, 0x43C20000, 0, 0x40430000):
            with self.subTest(address=hex(address)):
                self.overlay.ip_dict["processing_pipeline"]["phys_addr"] = address
                with self.assertRaisesRegex(PFO3Error, "address mismatch"):
                    self.construct()
                self.assertEqual(self.created, [])

    def test_other_ip_addresses_still_exact(self):
        self.overlay.ip_dict["raw_dma"]["phys_addr"] = 0x43C10000
        with self.assertRaisesRegex(PFO3Error, "address mismatch"):
            self.construct()
        self.assertEqual(self.created, [])

    def test_either_missing_hwh_interface_rejected(self):
        for interface in ("S_AXI_CONTROL", "S_AXI_DIAG"):
            with self.subTest(interface=interface):
                self.ranges = dict(_REGISTER_RANGES)
                del self.ranges[("processing_pipeline", interface)]
                self.write_hwh()
                with self.assertRaisesRegex(PFO3Error, "register-map contract mismatch"):
                    self.construct()
                self.assertEqual(self.created, [])

    def test_wrong_hwh_base_or_extent_rejected(self):
        for interface in ("S_AXI_CONTROL", "S_AXI_DIAG"):
            for wrong in ((0x43C20000, 0x43C2FFFF),
                          (_REGISTER_RANGES[("processing_pipeline", interface)][0],
                           0x43CFFFFF)):
                with self.subTest(interface=interface, wrong=wrong):
                    self.ranges = dict(_REGISTER_RANGES)
                    self.ranges[("processing_pipeline", interface)] = wrong
                    self.write_hwh()
                    with self.assertRaisesRegex(PFO3Error, "register-map contract mismatch"):
                        self.construct()
                    self.assertEqual(self.created, [])

    def test_missing_overlay_entry_rejected(self):
        del self.overlay.ip_dict["processing_pipeline"]
        with self.assertRaisesRegex(PFO3Error, "missing overlay IP address"):
            self.construct()
        self.assertEqual(self.created, [])


if __name__ == "__main__":
    unittest.main()
