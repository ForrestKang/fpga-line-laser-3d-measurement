"""Offline full-frame storage, ownership and commit/fault tests. No board IO."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from .protocol import HEADER_FIELDS, FINGERPRINT, assemble_r33 as assemble, identity
from .storage import (AsyncWriter, FrameResult, StorageError, readback,
                      HEADER, TRAILER, NAME, MAX_BYTES, FORMAT)

ROOT = Path(__file__).resolve().parents[2]
SIM = Path(os.environ.get('R33_TEST_FIXTURES', ROOT/'pl/direction_tests/evidence04'))
PIXELS = Path(os.environ.get('R33_TEST_PIXELS', SIM/'T05_000.hex' if
              'R33_TEST_FIXTURES' in os.environ or (SIM/'T05_000.hex').exists() else
              'D:/Workspace/CodexWork/scan3d_r31_dsp_storage_20261025_01/pl/sim/T05_000.hex'))

# Only immutable pixel input is shared. Each fixture still assembles and checks
# its own frame/header/footer/receipt and allocates independent XYZ/metadata.
_PIXEL_CACHE = {}
_PIXEL_CACHE_LOCK = threading.Lock()


def _fixture_pixels(path):
    resolved = Path(path).resolve(strict=True)
    with _PIXEL_CACHE_LOCK:
        before = resolved.stat()
        key = (str(resolved), before.st_size, before.st_mtime_ns,
               before.st_ctime_ns, before.st_ino)
        entry = _PIXEL_CACHE.get(str(resolved))
        if entry is not None and entry[0] == key:
            return entry[1]
        raw = bytes(int(value, 16) for value in resolved.read_text().split())
        after = resolved.stat()
        if key != (str(resolved), after.st_size, after.st_mtime_ns,
                   after.st_ctime_ns, after.st_ino):
            raise RuntimeError('pixel fixture changed while being decoded')
        # Replace the previous version, rather than accumulating stale images.
        _PIXEL_CACHE[str(resolved)] = (key, raw)
        return raw


def fixture(frame_id=0):
    """Actual RTL record fixture wrapped in a synthetic valid R33 stream.

    This is explicitly a protocol/storage test input, not a real hardware run.
    """
    header = {key: 0 for key in HEADER_FIELDS}
    header.update(frame_id=frame_id, source=1, epoch=33, cfg_version=1,
                  width=752, height=480, roi_x1=752, roi_y1=480,
                  roi_pixels=752*480, algorithm_revision=33,
                  fixed_revision=1, policy_revision=1)
    for i, value in enumerate(struct.unpack('<8I', bytes.fromhex(FINGERPRINT))):
        header['fingerprint'+str(i)] = value
    start = [header[key] for key in HEADER_FIELDS]
    start[:5] = [0x33544850, 33, 752, 480, frame_id+1]
    words = list(start)
    for line in (SIM/'T05_000.out').read_text().splitlines():
        kind, value = line.split()
        packed = int(value, 16)
        words.append(0x33520000 | int(kind))
        words.extend((packed >> (32*i)) & 0xffffffff for i in range(16))
    s = list(map(int, (SIM/'T05_000.summary').read_text().split()))
    footer = [0]*32
    footer[:12] = [0x33544650, 33, *s[2:11], s[20]]
    footer[14:18] = [s[18]&0xffffffff, s[18]>>32, s[19], s[1]]
    footer[23:28] = [frame_id+1, len(words)+32, s[21], s[22], s[23]]
    words += footer
    primary = struct.pack('<'+str(len(words))+'I', *words)
    frame, receipt = assemble(primary, expected_identity=identity(header))
    raw = _fixture_pixels(PIXELS)
    m = np.array([[1., 2., 3.], [4., 5., 6.]])
    p = np.vstack((m, [[7., 8., 9.]]))
    metadata = dict(frame_id=frame_id, target='T05_FIXTURE_NOT_BOARD', angle_rad=.25,
                    source_ready_ns=1, xyz_ready_ns=2)
    return dict(raw=raw, primary=primary, frame=frame,
                measurement_xyz=m, preview_xyz=p, receipt=receipt, metadata=metadata)


class StorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = fixture()

    def setUp(self):
        default_temp = ('D:/Workspace/CodexTemp/r33_storage_tests' if os.name == 'nt'
                        else Path(tempfile.gettempdir())/'r33_storage_tests')
        base = Path(os.environ.get('R33_TEST_TEMP', default_temp))
        base.mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(prefix='run_', dir=base))
        self.writers = []

    def tearDown(self):
        for writer in self.writers:
            try:
                writer.close(timeout_s=2)
            except StorageError:
                pass
        # Keep generated test files/evidence. No production artifacts deleted.

    def writer(self, name='storage', **kwargs):
        w = AsyncWriter(self.temp/name, **kwargs)
        self.writers.append(w)
        return w

    def test_cached_pixels_immutable_thread_safe_and_stat_invalidated(self):
        path = self.temp/'cache.hex'
        path.write_text('00 01 fe ff')
        first = _fixture_pixels(path)
        self.assertIs(type(first), bytes)
        results = []
        threads = [threading.Thread(target=lambda: results.append(_fixture_pixels(path)))
                   for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(value is first for value in results))
        path.write_text('ff 01 fe ff 02')
        updated = _fixture_pixels(path)
        self.assertEqual(updated, bytes((255,1,254,255,2)))
        self.assertEqual(first, bytes((0,1,254,255)))
        self.assertIsNot(first, updated)

    def test_cached_raw_does_not_reuse_frame_identity_or_mutable_outputs(self):
        first, second = fixture(1000), fixture(1001)
        self.assertIs(first['raw'], second['raw'])
        self.assertNotEqual(first['primary'], second['primary'])
        self.assertEqual(first['frame'].header['frame_id'], 1000)
        self.assertEqual(second['frame'].header['frame_id'], 1001)
        self.assertNotEqual(first['receipt']['assembled_sha256'], second['receipt']['assembled_sha256'])
        first['measurement_xyz'][:] = 0
        self.assertTrue(np.any(second['measurement_xyz']))
        first['metadata']['frame_id'] = 7
        self.assertEqual(second['metadata']['frame_id'], 1001)

    def test_full_snapshot_grouped_roundtrip_strict_primary(self):
        syncs = []
        w = self.writer(group=8, max_delay_s=1, fsync=lambda fd: (os.fsync(fd), syncs.append(fd)))
        inputs = [fixture(i) for i in range(8)]
        snapshots = [FrameResult.create(**value) for value in inputs]
        self.assertEqual([w.submit(s) for s in snapshots], list(range(1, 9)))
        status = w.close()
        self.assertEqual(len(syncs), 1)
        self.assertEqual(status['persisted'], 8)
        self.assertEqual(status['pending_frames'], 0)
        self.assertEqual(status['pending_bytes'], 0)
        self.assertEqual(len(w.commits), 8)
        self.assertTrue(all(c['persisted_ns'] >= c['sync_start_ns'] >= c['write_start_ns'] for c in w.commits))
        self.assertTrue(all(c['persisted_ns'] >= c['accepted_ns'] for c in w.commits))
        result = readback(w.output)
        self.assertEqual(result['frames'], 8)
        self.assertEqual(result['transactions'], 1)
        self.assertEqual((result['measurement_points'], result['preview_points']), (16, 24))
        self.assertFalse(result['original_live_durability_ack_inferred'])

    def test_mutable_inputs_cannot_change_submitted_snapshot(self):
        value = fixture()
        raw = bytearray(value['raw'])
        primary = bytearray(value['primary'])
        value.update(raw=raw, primary=primary)
        snapshot = FrameResult.create(**value)
        expected = tuple((name, hashlib.sha256(blob).hexdigest()) for name, blob in snapshot.components)
        raw[0] ^= 255
        primary[0] ^= 255
        value['measurement_xyz'][:] = 99
        value['preview_xyz'][:] = 100
        value['metadata']['frame_id'] = 900
        value['receipt']['assembled_sha256'] = 'bad'
        self.assertEqual(expected, tuple((name, hashlib.sha256(blob).hexdigest()) for name, blob in snapshot.components))
        w = self.writer()
        w.submit(snapshot)
        w.close()
        self.assertTrue(readback(w.output)['passed'])

    def test_actual_transport_input_sha_binding(self):
        value = fixture()
        value['receipt']['input_sha256'] = hashlib.sha256(value['raw']).hexdigest()
        FrameResult.create(**value)
        value['receipt']['input_sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'input_sha256 mismatch'):
            FrameResult.create(**value)

    def test_active_item_counts_in_both_capacity_bounds_no_early_ack(self):
        entered = threading.Event()
        release = threading.Event()
        def sync(fd):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test release missing')
            os.fsync(fd)
        snapshot = FrameResult.create(**fixture())
        w = self.writer(capacity_frames=1, capacity_bytes=snapshot.charge_bytes, group=1, fsync=sync)
        w.submit(snapshot)
        self.assertTrue(entered.wait(2))
        try:
            s = w.status()
            self.assertEqual((s['pending_frames'], s['queued_frames'], s['persisted']), (1, 0, 0))
            self.assertEqual(s['pending_bytes'], snapshot.charge_bytes)
            self.assertEqual(w.commits_since(), {'cursor': 0, 'commits': ()})
            self.assertEqual(w.counters()['persisted'], 0)
            with self.assertRaises(BufferError):
                w.submit(FrameResult.create(**fixture(1)), timeout_s=.01)
            with self.assertRaises(StorageError):
                w.drain(timeout_s=.01)
            self.assertEqual(len(w.retained_on_failure), 1)
            with self.assertRaises(StorageError):
                w.close(timeout_s=.01)
            self.assertFalse(w.status()['worker_stopped'])
        finally:
            release.set()
        w.close()
        self.assertEqual(w.commits[0]['frame_id'], 0)

    def test_fsync_failure_preserves_all_accepted_originals_no_ack(self):
        entered = threading.Event()
        release = threading.Event()
        def fail(_):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test failure gate was not released')
            raise OSError('injected fsync failure')
        snapshots = [FrameResult.create(**fixture(i)) for i in range(2)]
        w = self.writer(group=2, max_delay_s=1, fsync=fail)
        try:
            # Admit both before the worker can take its first batch. This test
            # requires two physically written frames despite failed durability;
            # fixture generation speed must not select a one-frame batch.
            with w._cv:
                self.assertEqual([w.submit(s) for s in snapshots], [1, 2])
            self.assertTrue(entered.wait(2))
            self.assertEqual(w.counters()['accepted'], 2)
            self.assertEqual(w.commits_since()['commits'], ())
        finally:
            release.set()
        with self.assertRaises(StorageError):
            w.close()
        status = w.status()
        self.assertEqual(status['persisted'], 0)
        self.assertEqual(status['pending_frames'], 2)
        self.assertEqual(status['unacknowledged_sequences'], (1, 2))
        self.assertEqual(len(w.retained_on_failure), 2)
        self.assertTrue(status['worker_stopped'])
        # Physically complete bytes can exist despite fsync failure. Reader
        # never infers a durability ACK from these bytes.
        result = readback(w.output)
        self.assertEqual(result['frames'], 2)
        self.assertFalse(result['original_live_durability_ack_inferred'])

    def test_short_write_is_retried_without_corrupting_journal(self):
        from .storage import _write_all
        class Partial:
            def __init__(self): self.data = bytearray()
            def write(self, value):
                count = min(3, len(value))
                self.data += value[:count]
                return count
        p = Partial()
        _write_all(p, b'1234567890')
        self.assertEqual(p.data, b'1234567890')

    def test_write_failure_retains_accepted_frame_and_partial_evidence(self):
        from . import storage
        w = self.writer(group=1)
        original = storage._write_all
        calls = []
        def injected(stream, data):
            calls.append(len(data))
            if len(calls) == 2:
                original(stream, memoryview(data)[:100])
                raise OSError('injected partial data write')
            return original(stream, data)
        with patch.object(storage, '_write_all', injected):
            w.submit(FrameResult.create(**fixture()))
            with self.assertRaises(StorageError):
                w.close()
        self.assertEqual(w.status()['persisted'], 0)
        self.assertEqual(w.status()['pending_frames'], 1)
        self.assertEqual(len(w.retained_on_failure), 1)
        self.assertGreater((w.output/NAME).stat().st_size, HEADER.size)
        with self.assertRaises(ValueError):
            readback(w.output)
        self.assertEqual(readback(w.output, recover_incomplete_tail=True)['frames'], 0)

    def test_maximum_180480_diagnostic_capacity_roundtrip(self):
        base = fixture(99)
        start = np.frombuffer(base['primary'][:256], '<u4').copy()
        n = 180480
        records = np.zeros((n, 17), '<u4')
        records[:, 0] = 0x33520000
        records[:, 1] = np.arange(n)
        records[:, 2] = np.repeat(np.arange(480), 376)
        records[:, 3] = np.tile(np.arange(376)*2, 480)
        records[:, 4] = records[:, 3]
        records[:, 14] = 1  # Locally rejected, no valid center or final patch.
        records[:, 15] = 0xffffffff
        rows = np.zeros((480, 17), '<u4')
        rows[:, 0] = 0x33520002
        rows[:, 1] = np.arange(480)
        rows[:, 5] = 376
        footer = np.zeros(32, '<u4')
        footer[[0, 1, 2, 11, 16, 23]] = [0x33544650, 33, n, 1, 752*480, int(start[4])]
        footer[27] = 3  # No preview / unusable measurement; not a slot overflow.
        footer[24] = 64+records.size+rows.size+32
        primary = start.tobytes()+records.tobytes()+rows.tobytes()+footer.tobytes()
        frame, receipt = assemble(primary, expected_identity=identity(base['frame'].header))
        value = FrameResult.create(raw=base['raw'], primary=primary, frame=frame,
            measurement_xyz=np.empty((0, 3)), preview_xyz=np.empty((0, 3)),
            receipt=receipt, metadata=base['metadata'])
        self.assertGreater(len(frame.payload), 11*1024**2)
        self.assertLess(value.charge_bytes, MAX_BYTES)
        w = self.writer(group=1)
        w.submit(value)
        w.close()
        result = readback(w.output)
        self.assertEqual(result['frames'], 1)
        self.assertEqual(result['measurement_points'], 0)
        self.assertTrue(result['strict_protocol'])

    def test_existing_output_refused_and_duplicate_identity_refused(self):
        w = self.writer(max_delay_s=1)
        with self.assertRaises(FileExistsError):
            AsyncWriter(w.output)
        value = FrameResult.create(**fixture())
        w.submit(value)
        with self.assertRaises(ValueError):
            w.submit(value)
        w.close()
        with self.assertRaises(StorageError):
            w.submit(FrameResult.create(**fixture(1)))

    def test_snapshot_validation_errors(self):
        for change in [dict(raw=b''), dict(measurement_xyz=np.ones((2, 2))),
                       dict(preview_xyz=np.array([[np.nan, 0, 0]])),
                       dict(receipt={'primary_stream_sha256': 'bad'}),
                       dict(metadata={'frame_id': 17}),
                       dict(metadata={'nonfinite': float('inf')})]:
            with self.subTest(change=tuple(change)):
                value = dict(self.base)
                value.update(change)
                with self.assertRaises((ValueError, TypeError)):
                    FrameResult.create(**value)
        value = dict(self.base)
        value['preview_xyz'] = np.zeros((1000, 3))
        with self.assertRaises(ValueError):
            FrameResult.create(**value)

    def test_capacity_configuration_and_oversized_item_rejected_before_accept(self):
        for kwargs in [dict(capacity_frames=129), dict(capacity_bytes=MAX_BYTES+1),
                       dict(group=33), dict(max_delay_s=6)]:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.writer(name='bad'+str(len(self.writers)), **kwargs)
        snapshot = FrameResult.create(**fixture())
        w = self.writer(capacity_bytes=snapshot.charge_bytes-1)
        with self.assertRaises(BufferError):
            w.submit(snapshot)
        self.assertEqual(w.status()['accepted'], 0)
        w.close()

    def test_corruption_and_truncated_tail_recovery(self):
        w = self.writer(group=1)
        w.submit(FrameResult.create(**fixture()))
        w.drain()
        w.submit(FrameResult.create(**fixture(1)))
        w.close()
        original = (w.output/NAME).read_bytes()
        first_bytes = w.status()['transactions'][0]['bytes']
        for name, blob, recover_ok in [
                ('bitflip', original[:100]+bytes([original[100]^1])+original[101:], False),
                ('partial_header', original[:first_bytes+7], True),
                ('partial_body', original[:-200], True),
                ('partial_commit', original[:-5], True)]:
            out = self.temp/name
            out.mkdir()
            shutil.copyfile(w.output/'FORMAT.json', out/'FORMAT.json')
            (out/NAME).write_bytes(blob)
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    readback(out)
                if recover_ok:
                    result = readback(out, recover_incomplete_tail=True)
                    self.assertEqual(result['frames'], 1)
                    self.assertTrue(result['incomplete_tail'])
                    self.assertEqual(result['valid_bytes'], first_bytes)
                else:
                    with self.assertRaises(ValueError):
                        readback(out, recover_incomplete_tail=True)

    def test_delay_forces_single_frame_and_idle_drain_flushes_batch(self):
        w = self.writer(group=8, max_delay_s=.025)
        w.submit(FrameResult.create(**fixture()))
        deadline = time.monotonic()+2
        while not w.commits and time.monotonic() < deadline:
            time.sleep(.002)
        self.assertEqual(len(w.commits), 1)
        w.submit(FrameResult.create(**fixture(1)))
        w.drain()
        self.assertEqual(len(w.commits), 2)
        w.close()
        self.assertEqual(readback(w.output)['transactions'], 2)

    def test_incremental_commit_cursor_is_independent_and_snapshot_owned(self):
        w = self.writer(group=1)
        self.assertEqual(w.commits_since(), {'cursor': 0, 'commits': ()})
        w.submit(FrameResult.create(**fixture()))
        w.drain()
        first = w.commits_since()
        self.assertEqual(first['cursor'], 1)
        self.assertEqual([c['sequence'] for c in first['commits']], [1])
        first['commits'][0]['frame_id'] = -1
        self.assertEqual(w.commits[0]['frame_id'], 0)
        w.submit(FrameResult.create(**fixture(1)))
        w.close()
        second = w.commits_since(first['cursor'])
        self.assertEqual(second['cursor'], 2)
        self.assertEqual([c['sequence'] for c in second['commits']], [2])
        self.assertEqual(w.commits_since(second['cursor']), {'cursor': 2, 'commits': ()})
        # A second observer can independently consume the full same ACK history.
        self.assertEqual(w.commits_since()['commits'], w.commits)
        status, counters = w.status(), w.counters()
        self.assertEqual(counters['transaction_count'], len(status['transactions']))
        self.assertNotIn('transactions', counters)
        self.assertNotIn('unacknowledged_sequences', counters)
        for key, value in counters.items():
            if key != 'transaction_count':
                self.assertEqual(value, status[key])
        for cursor in (-1, True, 0.5, 3):
            with self.assertRaises(ValueError):
                w.commits_since(cursor)

    def test_light_counters_do_not_walk_owned_or_history(self):
        class NoWalkList(list):
            def __iter__(self):
                raise AssertionError('light counter copied history')
            def __getitem__(self, key):
                raise AssertionError('light counter indexed history')
        class NoWalkDict(dict):
            def __iter__(self):
                raise AssertionError('light counter sorted owned frames')
        w = self.writer(group=1)
        w.submit(FrameResult.create(**fixture()))
        w.close()
        commits, transactions, owned = w._commits, w._transactions, w._owned
        try:
            w._commits = NoWalkList(commits)
            w._transactions = NoWalkList(transactions)
            w._owned = NoWalkDict(owned)
            s = w.counters()
            self.assertEqual((s['persisted'], s['transaction_count'], s['pending_frames']), (1, 1, 0))
        finally:
            w._commits, w._transactions, w._owned = commits, transactions, owned

    def test_incremental_commit_does_not_copy_previously_consumed_prefix(self):
        class Uncopyable:
            def keys(self):
                raise AssertionError('already consumed prefix copied again')
        w = self.writer(group=1)
        w.submit(FrameResult.create(**fixture()))
        w.submit(FrameResult.create(**fixture(1)))
        w.close()
        commits = w._commits
        try:
            w._commits = [Uncopyable(), commits[1]]
            tail = w.commits_since(1)
            self.assertEqual(tail, {'cursor': 2, 'commits': (commits[1],)})
            self.assertEqual(w.commits_since(2), {'cursor': 2, 'commits': ()})
            with self.assertRaisesRegex(AssertionError, 'prefix copied'):
                _ = w.commits
        finally:
            w._commits = commits


if __name__ == '__main__':
    unittest.main()
