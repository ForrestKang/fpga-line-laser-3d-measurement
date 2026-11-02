"""Offline checks of real archived PL packets and section MMIO. No board access."""
import json
from pathlib import Path
import struct
import sys
import os
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src/ps'), str(ROOT / 'tools')]
import numpy as np
from verify_release_pairing import verify, native_library_check
from r33.protocol import assemble, HEADER_FIELDS, identity, FINGERPRINT, BIN_FINGERPRINT, SEED_RETRY_FINGERPRINT
from r33.pl_section import PLSection, MAGIC, BASE, declared
from r33.profile_live import SectionBasis, ProfileConfig
from candidate.pfo3_r25 import PFO3Error


class MemoryMMIO:
    def __init__(self):
        self.regs, self.access = {BASE: MAGIC, BASE + 0x50: 0, BASE + 0x9c: 0}, []
    def read(self, offset):
        self.access.append(('read', offset))
        return self.regs.get(offset, 0)
    def write(self, offset, value):
        self.access.append(('write', offset))
        self.regs[offset] = value


class ReleasePairingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / 'board/firmware/manifest.json').read_text(encoding='utf-8'))
        cls.fixtures = sorted(path for path in (ROOT / 'sim/support/verification').rglob('*primary.bin')
                              if path.parent.name in ('r39_reference', 'r41_14_reference'))
        if len(cls.fixtures) != 27:
            raise AssertionError('Expected all 27 archived geometry primary packets')

    def packet(self, path):
        raw = path.read_bytes()
        header = dict(zip(HEADER_FIELDS, struct.unpack('<64I', raw[:256])))
        fp = {1: FINGERPRINT, 2: BIN_FINGERPRINT, 3: SEED_RETRY_FINGERPRINT}[header['policy_revision']]
        return raw, identity(header), dict(binning=header['policy_revision'] == 2, expected_fingerprint=fp,
                                          backend='reference')

    def test_firmware_source_and_register_contract(self):
        result = verify(ROOT)
        self.assertTrue(result['passed'], result)

    def test_declared_algorithm_fingerprints_match_runtime(self):
        self.assertEqual(self.manifest['pfo3']['parameter_fingerprint'], FINGERPRINT)
        self.assertEqual(self.manifest['pfo3_binning']['fingerprint'], BIN_FINGERPRINT)
        self.assertEqual(self.manifest['pfo3_binning']['header_policy_revision'], 2)
        self.assertEqual(self.manifest['pfo3']['n_lanes'], 2)

    def test_arm_native_binary_damage_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TEMP')) as folder:
            directory = Path(folder)
            for name in ('assemble_r33.c', 'libassemble_r33.so', 'libassemble_r33.so.json'):
                shutil.copy2(ROOT / 'src/ps/r33/native' / name, directory / name)
            binary = directory / 'libassemble_r33.so'
            raw = binary.read_bytes()
            binary.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
            result = native_library_check(directory, 'assemble_r33.c', binary.name)
            self.assertFalse(result['passed'])
            self.assertFalse(result['checks']['output'])

    def test_wrong_arm_architecture_is_rejected_even_with_matching_digest(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TEMP')) as folder:
            directory = Path(folder)
            for name in ('markers_r34.c', 'libmarkers_r34.so', 'libmarkers_r34.so.json'):
                shutil.copy2(ROOT / 'src/ps/r33/native' / name, directory / name)
            binary = directory / 'libmarkers_r34.so'
            raw = bytearray(binary.read_bytes())
            raw[18:20] = (62).to_bytes(2, 'little')
            binary.write_bytes(raw)
            receipt = directory / (binary.name + '.json')
            record = json.loads(receipt.read_text(encoding='utf-8'))
            import hashlib
            record['output_sha256'] = hashlib.sha256(raw).hexdigest()
            receipt.write_text(json.dumps(record), encoding='utf-8')
            result = native_library_check(directory, 'markers_r34.c', binary.name)
            self.assertFalse(result['passed'])
            self.assertFalse(result['checks']['elf_arm32_le'])

    def test_actual_archived_primary_packets_decode(self):
        for path in self.fixtures:
            with self.subTest(packet=path.name, family=path.parent.name):
                raw, wanted, mode = self.packet(path)
                frame, receipt = assemble(raw, expected_identity=wanted, **mode)
                self.assertIn('frame_decisions', frame.header)
                self.assertIn('measurement_count', frame.header)
                self.assertEqual(frame.rows.shape, (480, 16))
                self.assertEqual(receipt['ARM_pixel_algorithm_calls'], 0)
                self.assertEqual(receipt['ARM_geometry_calls'], 0)
                self.assertTrue(np.isfinite(frame.geometry_records[:, 1:4].view(np.int32) / 65536.).all())

    def test_identity_mismatch_is_rejected(self):
        raw, wanted, mode = self.packet(self.fixtures[0])
        wanted = dict(wanted, frame_id=wanted['frame_id'] + 1)
        with self.assertRaises(PFO3Error):
            assemble(raw, expected_identity=wanted, **mode)

    def test_bad_primary_magic_is_rejected(self):
        raw, wanted, mode = self.packet(self.fixtures[0])
        bad = bytes([raw[0] ^ 1]) + raw[1:]
        with self.assertRaises(PFO3Error):
            assemble(bad, expected_identity=wanted, **mode)

    def test_truncated_primary_is_rejected(self):
        raw, wanted, mode = self.packet(self.fixtures[0])
        with self.assertRaises(PFO3Error):
            assemble(raw[:-4], expected_identity=wanted, **mode)

    def test_section_driver_configure_and_snapshot_stays_in_window(self):
        mmio = MemoryMMIO()
        driver = PLSection(self.manifest, lambda *_: mmio, 0)
        driver.configure(SectionBasis([.2, .1, 1., -300.]), ProfileConfig())
        mmio.regs[BASE + 0x50] = 1
        frame = driver.frame()
        self.assertEqual(frame['packets'], 1)
        self.assertEqual(frame['points'], 0)
        self.assertTrue(all(BASE <= offset <= BASE + 0xfc and offset % 4 == 0
                            for _, offset in mmio.access))
        for bad in (-4, 1, 0x100):
            with self.assertRaises(ValueError):
                driver._read(bad)

    def test_bad_section_magic_rejected(self):
        mmio = MemoryMMIO()
        mmio.regs[BASE] ^= 1
        with self.assertRaisesRegex(RuntimeError, 'magic'):
            PLSection(self.manifest, lambda *_: mmio, 0)

    def test_undeclared_section_does_not_create_mmio(self):
        created = []
        with self.assertRaises(RuntimeError):
            PLSection({}, lambda *_: created.append(True), 0)
        self.assertEqual(created, [])
        with self.assertRaises(ValueError):
            declared({'section_measure': dict(self.manifest['section_measure'], register_window=[0x600, 0x6ff])})


if __name__ == '__main__':
    unittest.main(verbosity=2)
