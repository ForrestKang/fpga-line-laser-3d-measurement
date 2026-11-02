"""Atomic capacity admission under local fsync stalls; no board IO."""
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from .storage import AsyncWriter, FrameResult, StorageError, readback
from .tests_storage import fixture


class NonblockingAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshots = [FrameResult.create(**fixture(i)) for i in range(4)]

    def setUp(self):
        base = Path(os.environ.get('R33_TEST_TEMP', 'D:/Workspace/CodexTemp/r34_storage_tests'))
        base.mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(prefix='nonblocking_', dir=base))
        self.writers = []
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        for writer in self.writers:
            try:
                writer.close(timeout_s=3.)
            except StorageError:
                pass

    def writer(self, name='journal', **kwargs):
        writer = AsyncWriter(self.temp/name, **kwargs)
        self.writers.append(writer)
        return writer

    def stalled_writer(self, **kwargs):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def sync(fd):
            entered.set()
            if not release.wait(5.):
                raise TimeoutError('test fsync gate not released')
            os.fsync(fd)
        writer = self.writer(group=1, fsync=sync, **kwargs)
        self.assertEqual(writer.try_submit(self.snapshots[0]), 1)
        self.assertTrue(entered.wait(2.))
        return writer, release

    def test_frame_and_byte_full_refuse_without_wait_or_ownership_then_retry(self):
        for bound in ('frames', 'bytes'):
            with self.subTest(bound=bound):
                kwargs = dict(capacity_frames=1 if bound == 'frames' else 4)
                if bound == 'bytes':
                    kwargs['capacity_bytes'] = max(s.charge_bytes for s in self.snapshots)
                writer, release = self.stalled_writer(**dict(kwargs, name=bound))
                candidate = self.snapshots[1]
                before = writer.counters()
                start = time.monotonic()
                for _ in range(100):
                    self.assertIsNone(writer.try_submit(candidate))
                self.assertLess(time.monotonic()-start, .5)
                self.assertEqual(writer.counters(), before)
                self.assertEqual(writer.retained_on_failure, (self.snapshots[0],))
                self.assertNotIn(candidate.identity_items, writer._identities)
                self.assertEqual(writer.commits_since(), {'cursor': 0, 'commits': ()})
                release.set()
                writer.drain(timeout_s=3.)
                self.assertEqual(writer.try_submit(candidate), 2)
                writer.close(timeout_s=3.)
                self.assertEqual([r['sequence'] for r in writer.commits], [1, 2])
                self.assertEqual(readback(writer.output)['frames'], 2)

    def test_two_producers_compete_for_one_slot_atomically(self):
        writer, release = self.stalled_writer(capacity_frames=2)
        barrier = threading.Barrier(3)
        results, errors = [], []
        def producer(snapshot):
            try:
                barrier.wait(timeout=2.)
                results.append((snapshot, writer.try_submit(snapshot)))
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=producer, args=(s,)) for s in self.snapshots[1:3]]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2.)
        for thread in threads:
            thread.join(2.)
            self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(sorted(sequence for _, sequence in results if sequence is not None), [2])
        refused = [snapshot for snapshot, sequence in results if sequence is None]
        self.assertEqual(len(refused), 1)
        self.assertEqual(writer.status()['unacknowledged_sequences'], (1, 2))
        self.assertEqual(len(writer.retained_on_failure), 2)
        release.set()
        writer.drain(timeout_s=3.)
        self.assertEqual(writer.try_submit(refused[0]), 3)
        writer.close(timeout_s=3.)
        self.assertEqual(readback(writer.output)['frames'], 3)

    def test_errors_do_not_become_capacity_refusals(self):
        writer, release = self.stalled_writer(capacity_frames=1)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            writer.try_submit(self.snapshots[0])
        with self.assertRaises(TypeError):
            writer.try_submit(object())
        tiny = self.writer(name='tiny', group=1,
            capacity_bytes=self.snapshots[0].charge_bytes-1)
        with self.assertRaisesRegex(BufferError, 'exceeds'):
            tiny.try_submit(self.snapshots[0])
        self.assertEqual(tiny.counters()['accepted'], 0)
        release.set()
        writer.close(timeout_s=3.)
        with self.assertRaises(StorageError):
            writer.try_submit(self.snapshots[1])
        committed = self.writer(name='committed', group=1)
        committed.try_submit(self.snapshots[0])
        committed.drain(timeout_s=3.)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            committed.try_submit(self.snapshots[0])

    def test_failed_fsync_keeps_accepted_snapshot_and_rejects_new_work(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def fail(_):
            entered.set()
            if not release.wait(5.):
                raise TimeoutError('test failure gate not released')
            raise OSError('nonblocking injected fsync failure')
        writer = self.writer(group=1, capacity_frames=1, fsync=fail)
        self.assertEqual(writer.try_submit(self.snapshots[0]), 1)
        self.assertTrue(entered.wait(2.))
        release.set()
        with self.assertRaises(StorageError):
            writer.close(timeout_s=3.)
        with self.assertRaisesRegex(StorageError, 'storage failed'):
            writer.try_submit(self.snapshots[1])
        self.assertEqual(writer.retained_on_failure, (self.snapshots[0],))
        self.assertEqual(writer.commits_since(), {'cursor': 0, 'commits': ()})

    def test_blocking_wrapper_timeout_is_bounded_and_retries_after_commit(self):
        writer, release = self.stalled_writer(capacity_frames=1)
        for timeout in (-1., float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                writer.submit(self.snapshots[1], timeout_s=timeout)
        start = time.monotonic()
        with self.assertRaisesRegex(BufferError, 'backpressure timeout'):
            writer.submit(self.snapshots[1], timeout_s=.02)
        self.assertLess(time.monotonic()-start, .5)
        self.assertEqual(writer.counters()['accepted'], 1)
        timer = threading.Timer(.03, release.set)
        timer.start()
        try:
            self.assertEqual(writer.submit(self.snapshots[1], timeout_s=2.), 2)
        finally:
            timer.join(1.)
        writer.close(timeout_s=3.)
        self.assertEqual(readback(writer.output)['frames'], 2)

    def test_try_submit_snapshot_stays_immutable_through_commit(self):
        values = fixture(100)
        values.update(raw=bytearray(values['raw']), primary=bytearray(values['primary']))
        snapshot = FrameResult.create(**values)
        writer, release = self.stalled_writer(capacity_frames=2)
        self.assertEqual(writer.try_submit(snapshot), 2)
        values['raw'][0] ^= 255
        values['primary'][0] ^= 255
        values['measurement_xyz'][:] = -1
        values['preview_xyz'][:] = -2
        values['metadata']['frame_id'] = 999
        values['receipt']['assembled_sha256'] = 'changed'
        for name, blob in snapshot.components:
            self.assertEqual(hashlib.sha256(blob).hexdigest(),
                next(part[2] for part in snapshot.descriptors if part[0] == name))
        self.assertIs(writer.retained_on_failure[1], snapshot)
        self.assertEqual(writer.counters()['persisted'], 0)
        release.set()
        writer.close(timeout_s=3.)
        result = readback(writer.output)
        self.assertEqual(result['frames'], 2)
        self.assertEqual(result['frame_summaries'][1]['frame_id'], 100)
        self.assertEqual(result['frame_summaries'][1]['component_sha256'],
            {part[0]: part[2] for part in snapshot.descriptors})


if __name__ == '__main__':
    unittest.main()
