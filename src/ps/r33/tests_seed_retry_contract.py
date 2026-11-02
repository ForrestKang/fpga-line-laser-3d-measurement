"""New algorithm identity is explicit, strict and round-trips old/new journals."""
import struct
import hashlib
import tempfile
import unittest
from pathlib import Path
import numpy as np

from .protocol import (HEADER_FIELDS, FINGERPRINT, SEED_RETRY_FINGERPRINT,
    assemble, assemble_r33, assemble_reference, PFO3Error)
from .tests_protocol_native import synthetic
from .storage import SourceSnapshot, FrameResult, AsyncWriter, readback


def stream(retry):
    data, ident = synthetic(1, False)
    fp = SEED_RETRY_FINGERPRINT if retry else FINGERPRINT
    data = bytearray(data)
    struct.pack_into('<I', data, 4*HEADER_FIELDS.index('policy_revision'), 3 if retry else 1)
    for i, value in enumerate(struct.unpack('<8I', bytes.fromhex(fp))):
        struct.pack_into('<I', data, 4*HEADER_FIELDS.index('fingerprint'+str(i)), value)
    return bytes(data), ident, fp


def outer(data):
    """Valid R40+ primary envelope with no accepted geometry or moments."""
    words = np.frombuffer(data, '<u4')
    head, tail = words[:64].copy(), words[-32:].copy()
    records = words[64:-32].reshape(-1,17).copy()
    records[:,0] = (records[:,0] & 255) | 0x35520000
    head[:2] = [0x35544850, 35]
    tail[:2] = [0x35544650, 35]
    metadata = np.zeros(17, dtype='<u4')
    metadata[0] = 0x35520006
    metadata[2:10] = np.frombuffer(bytes.fromhex(hashlib.sha256(struct.pack('<36q', *([0]*36))).hexdigest()), '<u4')
    metadata[12:14] = [1,2]
    tail[24] = 96 + 17*(len(records)+1)
    return head.tobytes()+records.tobytes()+metadata.tobytes()+tail.tobytes()


class SeedRetryContractTests(unittest.TestCase):
    def test_old_and_new_reference_native_equivalence(self):
        for retry in (False, True):
            data, ident, fp = stream(retry)
            ref, _ = assemble_reference(data, expected_identity=ident, expected_fingerprint=fp)
            actual, receipt = assemble_r33(data, expected_identity=ident, expected_fingerprint=fp)
            self.assertEqual(actual.payload, ref.payload)
            self.assertEqual(receipt['algorithm_fingerprint'], fp)

    def test_no_implicit_revision_or_fingerprint_acceptance(self):
        for retry in (False, True):
            data, ident, fp = stream(retry)
            wrong = FINGERPRINT if retry else SEED_RETRY_FINGERPRINT
            for parser in (assemble_reference, assemble_r33):
                with self.assertRaises(PFO3Error):
                    parser(data, expected_identity=ident, expected_fingerprint=wrong)
                if retry:
                    with self.assertRaises(PFO3Error):
                        parser(data, expected_identity=ident)
                    with self.assertRaises(PFO3Error):
                        parser(data, expected_identity=ident, expected_fingerprint=fp, binning=True)

    def test_strict_journal_roundtrip_old_and_new(self):
        for retry in (False, True):
            data, ident, fp = stream(retry)
            data = outer(data)
            frame, receipt = assemble(data, expected_identity=ident, expected_fingerprint=fp)
            receipt['geometry_binding'] = dict(epoch=frame.geometry_epoch,
                sha256=frame.geometry_sha256, parameters=[0]*36,
                calibration_sha256='0'*64, angle_rad=0.)
            source = SourceSnapshot.create(raw=bytes(752*480), primary=data, frame=frame, receipt=receipt)
            result = FrameResult.from_source(source, measurement_xyz=np.empty((0,3)),
                preview_xyz=np.empty((0,3)), metadata={'frame_id':ident['frame_id']})
            with tempfile.TemporaryDirectory() as root:
                writer = AsyncWriter(Path(root)/'run', max_delay_s=0)
                writer.submit(result)
                writer.close()
                report = readback(Path(root)/'run', strict_protocol=True)
                self.assertTrue(report['passed'])
                self.assertEqual(report['frames'], 1)

    def test_receipt_fingerprint_cannot_disagree(self):
        data, ident, fp = stream(True)
        data = outer(data)
        frame, receipt = assemble(data, expected_identity=ident, expected_fingerprint=fp)
        receipt['algorithm_fingerprint'] = FINGERPRINT
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            SourceSnapshot.create(raw=bytes(752*480), primary=data, frame=frame, receipt=receipt)


if __name__ == '__main__':
    unittest.main()
