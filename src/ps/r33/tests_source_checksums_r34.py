"""Equivalent bounded source digests; saved evidence only, no hardware IO."""
import hashlib
import json
import os
from pathlib import Path
import struct
import unittest
from unittest.mock import patch
import zlib

import numpy as np

from . import storage
from .protocol import Frame, HEADER_FIELDS


BASELINE = Path(os.environ.get('R34_STORAGE_BASELINE',
    'D:/Workspace/CodexWork/scan3d_r33_full_pixel_20261025_01'))


def saved_frames(target, limit=4):
    """Use actual old-board source/XYZ components, not a generated receipt."""
    path = BASELINE/f'board/r33_formal_v14_{target}_rep1/storage'/storage.NAME
    with path.open('rb') as stream:
        count = 0
        while count < limit:
            start = stream.tell()
            header = storage.HEADER.unpack(stream.read(storage.HEADER.size))
            data_start = start + storage.HEADER.size
            stream.seek(data_start + header[6])
            index = json.loads(stream.read(header[7]))
            for row in index['frames']:
                blobs = {}
                for part in row['components']:
                    stream.seek(data_start + part['offset'])
                    blobs[part['name']] = stream.read(part['bytes'])
                payload = blobs['observations_r33']
                frame = Frame(dict(zip(HEADER_FIELDS,
                    struct.unpack_from('<64I', payload))), None, None, payload)
                yield dict(raw=blobs['raw'], primary=blobs['primary'], frame=frame,
                    receipt=json.loads(blobs['receipt_json']),
                    metadata=json.loads(blobs['metadata_json']),
                    measurement_xyz=np.frombuffer(blobs['measurement_xyz'], '<f8').reshape(-1, 3),
                    preview_xyz=np.frombuffer(blobs['preview_xyz'], '<f8').reshape(-1, 3))
                count += 1
                if count == limit:
                    return
            stream.seek(data_start + header[6] + header[7] + storage.TRAILER.size)


class SourceChecksumTests(unittest.TestCase):
    def test_production_default_is_bulk(self):
        self.assertEqual(storage.SOURCE_CHECKSUM_CHUNK_BYTES, 0)

    def test_chunk_boundaries_and_maximum_payload_are_exact(self):
        # Include the contract's largest complete observation body, not only
        # the usual ~50KB payload. The view slices do not allocate full copies.
        for length in (0, 1, 2046, 2047, 2048, 4094, 4095, 752*480,
                       256 + 64*(180480+480)):
            with self.subTest(length=length):
                data = (bytes(range(256)) * ((length+255)//256))[:length]
                expected = (hashlib.sha256(data).hexdigest(), zlib.crc32(data)&0xffffffff)
                for chunk_bytes in (0, 2047):
                    with patch.object(storage, 'SOURCE_CHECKSUM_CHUNK_BYTES', chunk_bytes):
                        self.assertEqual(storage._source_checksums(data), expected)
                        self.assertEqual(storage._source_crc(memoryview(data)), expected[1])

    def test_actual_T05_T06_sources_and_full_results_equal_bulk(self):
        for target in ('T05', 'T06'):
            for args in saved_frames(target):
                with self.subTest(target=target, frame=args['metadata']['frame_id']):
                    with patch.object(storage, 'SOURCE_CHECKSUM_CHUNK_BYTES', 0):
                        bulk = storage.FrameResult.create(**args)
                    with patch.object(storage, 'SOURCE_CHECKSUM_CHUNK_BYTES', 2047):
                        chunked = storage.FrameResult.create(**args)
                    self.assertEqual(chunked, bulk)
                    # Original byte contents, all descriptors and charge match.
                    self.assertEqual(chunked.components, bulk.components)
                    self.assertEqual(chunked.charge_bytes, bulk.charge_bytes)

    def test_bad_receipt_and_body_crc_reject_in_both_modes(self):
        args = next(saved_frames('T05', 1))
        source_args = {key: args[key] for key in ('raw', 'primary', 'frame', 'receipt')}
        for chunk_bytes in (0, 2047):
            with self.subTest(chunk_bytes=chunk_bytes), \
                    patch.object(storage, 'SOURCE_CHECKSUM_CHUNK_BYTES', chunk_bytes):
                for key in ('input_sha256', 'raw_sha256',
                            'primary_stream_sha256', 'assembled_sha256'):
                    receipt = dict(source_args['receipt'], **{key: 'not-the-digest'})
                    with self.assertRaisesRegex(ValueError, 'mismatch'):
                        storage.SourceSnapshot.create(**dict(source_args, receipt=receipt))
                payload = bytearray(args['frame'].payload)
                payload[-1] ^= 1
                frame = Frame(args['frame'].header, None, None, bytes(payload))
                with self.assertRaisesRegex(ValueError, 'body CRC'):
                    storage.SourceSnapshot.create(**dict(source_args, frame=frame))

    def test_mutable_source_is_owned_before_checksumming(self):
        args = next(saved_frames('T05', 1))
        mutable = dict(raw=bytearray(args['raw']), primary=bytearray(args['primary']),
                       frame=args['frame'], receipt=dict(args['receipt']))
        snapshot = storage.SourceSnapshot.create(**mutable)
        mutable['raw'][0] ^= 1
        mutable['primary'][0] ^= 1
        mutable['receipt']['input_sha256'] = 'changed'
        for (name, blob), descriptor in zip(((name, zlib.decompress(blob) if name == 'raw' else blob)
                                             for name, blob in snapshot.components), snapshot.descriptors):
            self.assertIsInstance(blob, bytes)
            self.assertEqual(descriptor,
                (name, len(blob), hashlib.sha256(blob).hexdigest(), zlib.crc32(blob)&0xffffffff))


if __name__ == '__main__':
    unittest.main()
