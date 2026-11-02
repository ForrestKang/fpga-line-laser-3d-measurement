"""Strict journal compatibility dispatches by explicit hardware envelope identity."""
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np

from .protocol import (HEADER_FIELDS, FINGERPRINT, BIN_FINGERPRINT,
                       SEED_RETRY_FINGERPRINT, assemble_r33, assemble, identity)
from .tests_protocol_native import synthetic
from .tests_seed_retry_contract import outer
from .storage import AsyncWriter, FrameResult, readback


def packet(policy, geometry):
    raw, _ = synthetic()
    data = bytearray(raw)
    fingerprint = {1: FINGERPRINT, 2: BIN_FINGERPRINT, 3: SEED_RETRY_FINGERPRINT}[policy]
    struct.pack_into('<I', data, HEADER_FIELDS.index('policy_revision') * 4, policy)
    for i, value in enumerate(struct.unpack('<8I', bytes.fromhex(fingerprint))):
        struct.pack_into('<I', data, HEADER_FIELDS.index('fingerprint' + str(i)) * 4, value)
    if geometry:
        data = outer(data)
    expected = identity(dict(zip(HEADER_FIELDS, struct.unpack_from('<64I', data))))
    frame, receipt = (assemble if geometry else assemble_r33)(data,
        expected_identity=expected, expected_fingerprint=fingerprint, binning=policy == 2)
    if geometry:
        receipt['geometry_binding'] = dict(epoch=frame.geometry_epoch,
            sha256=frame.geometry_sha256, parameters=[0] * 36,
            calibration_sha256='0' * 64, angle_rad=0.)
    return bytes(data), frame, receipt


class ArchiveVersionTests(unittest.TestCase):
    def save(self, root, primary, frame, receipt):
        result = FrameResult.create(raw=bytes(752 * 480), primary=primary, frame=frame,
            receipt=receipt, measurement_xyz=np.empty((0, 3)), preview_xyz=np.empty((0, 3)),
            metadata={'frame_id': frame.header['frame_id']})
        output = Path(root) / 'journal'
        writer = AsyncWriter(output, max_delay_s=0)
        writer.submit(result)
        writer.close()
        return output

    def test_both_protocol_envelopes_and_all_three_policies_roundtrip(self):
        for geometry in (False, True):
            for policy in (1, 2, 3):
                with self.subTest(geometry=geometry, policy=policy), tempfile.TemporaryDirectory() as root:
                    primary, frame, receipt = packet(policy, geometry)
                    output = self.save(root, primary, frame, receipt)
                    report = readback(output, strict_protocol=True)
                    self.assertTrue(report['passed'])
                    self.assertEqual(report['frames'], 1)

    def test_unknown_envelope_is_rejected_even_with_matching_component_digests(self):
        primary, frame, receipt = packet(1, False)
        primary = struct.pack('<I', 0x12345678) + primary[4:]
        receipt = dict(receipt, primary_stream_sha256=hashlib.sha256(primary).hexdigest())
        with tempfile.TemporaryDirectory() as root:
            output = self.save(root, primary, frame, receipt)
            with self.assertRaisesRegex(ValueError, 'envelope'):
                readback(output, strict_protocol=True)

    def test_truncated_primary_is_rejected_even_with_matching_component_digests(self):
        primary, frame, receipt = packet(1, False)
        primary = primary[:4]
        receipt = dict(receipt, primary_stream_sha256=hashlib.sha256(primary).hexdigest())
        with tempfile.TemporaryDirectory() as root:
            output = self.save(root, primary, frame, receipt)
            with self.assertRaises(ValueError):
                readback(output, strict_protocol=True)


if __name__ == '__main__':
    unittest.main()
