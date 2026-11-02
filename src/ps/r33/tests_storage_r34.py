"""R34 source ownership, V1 compatibility and bounded stall tests; no board IO."""
from dataclasses import FrozenInstanceError
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest.mock import patch

import numpy as np

from . import storage
from .protocol import Frame, HEADER_FIELDS
from .storage import AsyncWriter, SourceSnapshot, FrameResult, HEADER, NAME, readback
from .tests_storage import fixture

BASELINE = Path(os.environ.get('R34_STORAGE_BASELINE',
    'D:/Workspace/CodexWork/scan3d_r33_full_pixel_20261025_01'))


class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = fixture()

    def test_factory_snapshot_matches_original_frozen_implementation(self):
        spec = importlib.util.spec_from_file_location('r33._storage_frozen_r34_test',
            BASELINE/'runtime/r33/storage.py')
        old = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = old
        spec.loader.exec_module(old)
        for target in ('T05', 'T06'):
            journal = BASELINE/f'board/r33_formal_v14_{target}_rep1/storage'/NAME
            with journal.open('rb') as stream:
                fields = HEADER.unpack(stream.read(HEADER.size))
                data_start = HEADER.size
                stream.seek(data_start + fields[6])
                index = json.loads(stream.read(fields[7]))
                row = index['frames'][0]
                blobs = {}
                for part in row['components']:
                    stream.seek(data_start + part['offset'])
                    blobs[part['name']] = stream.read(part['bytes'])
            payload = blobs['observations_r33']
            header = dict(zip(HEADER_FIELDS, struct.unpack_from('<64I', payload)))
            frame = Frame(header, None, None, payload)
            args = dict(raw=blobs['raw'], primary=blobs['primary'], frame=frame,
                measurement_xyz=np.frombuffer(blobs['measurement_xyz'], '<f8').reshape(-1, 3),
                preview_xyz=np.frombuffer(blobs['preview_xyz'], '<f8').reshape(-1, 3),
                receipt=json.loads(blobs['receipt_json']), metadata=json.loads(blobs['metadata_json']))
            reference = old.FrameResult.create(**args)
            actual = FrameResult.create(**args)
            logical = {}
            for (name, blob), (stored_name, encoding, stored_bytes) in zip(actual.components, actual.encodings):
                self.assertEqual(name, stored_name)
                self.assertEqual(len(blob), stored_bytes)
                logical[name] = zlib.decompress(blob) if encoding == 'zlib' else blob
            reference_parts = dict(reference.components)
            # The declared V2 layout compresses Raw and changes only the receipt
            # storage-format tag; all measurement/source bytes remain exact.
            for name in storage.COMPONENTS:
                expected = reference_parts[name]
                if name == 'receipt_json':
                    receipt = json.loads(expected)
                    self.assertEqual(receipt.pop('storage_contract'), old.FORMAT)
                    current_receipt = json.loads(logical[name])
                    self.assertEqual(current_receipt.pop('storage_contract'), storage.FORMAT)
                    self.assertEqual(receipt, current_receipt)
                else:
                    self.assertTrue(expected == logical[name], name + ' logical bytes changed')
            for name, size, digest, crc in actual.descriptors:
                blob = logical[name]
                self.assertEqual((size, digest, crc),
                    (len(blob), storage._digest(blob), storage._crc(blob)))
            self.assertEqual(reference.identity_items, actual.identity_items)
            template = storage._index_row(10**20-1, 10**20-1, dict(actual.identity_items),
                actual.descriptors, data_offset=10**20-1, encodings=actual.encodings)
            self.assertEqual(actual.charge_bytes,
                sum(len(blob) for _, blob in actual.components) + len(storage._json_bytes(template)) + 256)

    def test_source_is_owned_and_from_source_never_rehashes_source(self):
        values = dict(self.values)
        values['raw'] = bytearray(values['raw'])
        values['primary'] = bytearray(values['primary'])
        source = SourceSnapshot.create(**{k: values[k] for k in ('raw', 'primary', 'frame', 'receipt')})
        expected = FrameResult.create(**values)
        values['raw'][0] ^= 255
        values['primary'][0] ^= 255
        values['receipt'] = dict(values['receipt'], assembled_sha256='corrupt')
        originals = {id(blob) for _, blob in source.components}
        original_digest, original_crc = storage._digest, storage._crc
        def digest(blob):
            self.assertNotIn(id(blob), originals)
            return original_digest(blob)
        def crc(blob):
            self.assertNotIn(id(blob), originals)
            return original_crc(blob)
        with patch.object(storage, '_digest', digest), patch.object(storage, '_crc', crc):
            actual = FrameResult.from_source(source,
                measurement_xyz=values['measurement_xyz'], preview_xyz=values['preview_xyz'],
                metadata=values['metadata'])
        self.assertEqual(expected, actual)
        with self.assertRaises(FrozenInstanceError):
            source.components = ()

    def test_source_invalid_receipt_and_identity_still_rejected(self):
        args = {k: self.values[k] for k in ('raw', 'primary', 'frame', 'receipt')}
        for key in ('input_sha256', 'raw_sha256', 'primary_stream_sha256', 'assembled_sha256'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'mismatch'):
                SourceSnapshot.create(**dict(args, receipt=dict(args['receipt'], **{key: 'bad'})))
        source = SourceSnapshot.create(**args)
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            FrameResult.from_source(source, measurement_xyz=self.values['measurement_xyz'],
                preview_xyz=self.values['preview_xyz'], metadata={'frame_id': 999})
        with self.assertRaisesRegex(ValueError, 'finite Nx3'):
            FrameResult.from_source(source, measurement_xyz=np.array([[np.nan, 1., 2.]]),
                preview_xyz=self.values['preview_xyz'], metadata=self.values['metadata'])


class QueueTests(unittest.TestCase):
    def test_128_owned_frame_budget_keeps_v1_transaction_limit(self):
        base = Path(os.environ.get('R33_TEST_TEMP', 'D:/Workspace/CodexTemp/r34_storage_tests'))
        base.mkdir(parents=True, exist_ok=True)
        parent = Path(tempfile.mkdtemp(dir=base))
        entered, release = threading.Event(), threading.Event()
        def sync(fd):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('test release missing')
            os.fsync(fd)
        snapshots = [FrameResult.create(**fixture(i)) for i in range(80)]
        writer = AsyncWriter(parent/'journal', capacity_frames=128, group=8,
            max_delay_s=.01, fsync=sync)
        try:
            writer.submit(snapshots[0])
            self.assertTrue(entered.wait(2))
            for frame in snapshots[1:]:
                writer.submit(frame, timeout_s=.05)
            state = writer.status()
            self.assertEqual((state['pending_frames'], state['persisted']), (80, 0))
            self.assertLessEqual(state['pending_bytes'], storage.MAX_BYTES)
            self.assertGreaterEqual(state['pending_bytes'], sum(f.charge_bytes for f in snapshots))
        finally:
            release.set()
            writer.close()
        verified = readback(parent/'journal')
        self.assertEqual(verified['frames'], 80)
        self.assertTrue(all(t['frames'] <= 32 for t in writer.status()['transactions']))
        with self.assertRaises(ValueError):
            AsyncWriter(parent/'bad', capacity_frames=128, group=33)

    def test_keep_size_preallocation_is_optional_and_never_changes_ack_contract(self):
        base = Path(os.environ.get('R33_TEST_TEMP', 'D:/Workspace/CodexTemp/r34_storage_tests'))
        base.mkdir(parents=True, exist_ok=True)
        parent = Path(tempfile.mkdtemp(dir=base))
        calls = []
        def reserve(fd, offset, length):
            self.assertEqual(os.fstat(fd).st_size, 0)
            calls.append((offset, length))
            return False
        with patch.object(storage, '_keep_size_preallocate', reserve):
            writer = AsyncWriter(parent/'journal', group=1, preallocate_bytes=8*1024*1024)
            writer.submit(FrameResult.create(**fixture()))
            state = writer.close()
        self.assertEqual(len(calls), 1)
        self.assertEqual(state['persisted'], 1)
        self.assertFalse(state['preallocation_supported'])
        self.assertTrue(readback(parent/'journal')['passed'])


if __name__ == '__main__':
    unittest.main()
