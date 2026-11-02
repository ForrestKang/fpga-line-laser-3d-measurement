"""Bounded source-sealing pipeline and shutdown tests; fake hardware only."""
import hashlib
from functools import partial
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from unittest.mock import patch

from . import replay
from . import tests_replay as helpers
from .storage import SourceSnapshot, FrameResult
from .tests_storage import fixture


class SourcePipelineTests(unittest.TestCase):
    def fake(self, folder, **kwargs):
        return helpers.ReplayTests(methodName='runTest').fake_run(folder, **kwargs)

    def test_worker_start_failure_keeps_original_error_evidence_and_releases_idle_lock(self):
        original_start = threading.Thread.start
        names = ('R33_resource_samples', 'R34_source_snapshot_owner',
                 'R33_bounded_input_preparation', 'R33_unique_DMA_owner')
        for name in names:
            with self.subTest(worker=name), tempfile.TemporaryDirectory(dir=helpers.TEMP) as folder:
                released = []
                def start(thread, *args, **kwargs):
                    if thread.name == name:
                        # The fake hardware lock is already acquired here.
                        # Observe its cleanup without changing the run logic.
                        lock_class = sys.modules['structured_light.motion.lifecycle'].HardwareProcessLock
                        release = lock_class.release
                        def observe_release(owner):
                            released.append(True)
                            return release(owner)
                        lock_class.release = observe_release
                        raise RuntimeError('injected start failure: ' + name)
                    return original_start(thread, *args, **kwargs)
                with patch.object(threading.Thread, 'start', start):
                    with self.assertRaisesRegex(RuntimeError, 'injected start failure: ' + name):
                        self.fake(folder, prefetch_depth=2)
                error = json.loads((Path(folder)/'out/ERROR.json').read_text())
                self.assertEqual(error['error'], 'RuntimeError: injected start failure: ' + name)
                self.assertTrue(error['producer_stopped'] and error['source_stopped']
                                and error['preparation_stopped'])
                self.assertTrue(error['failure_evidence_complete'])
                self.assertEqual(released, [True])
                self.assertFalse((Path(folder)/'out/RESULT.json').exists())

    def test_source_zero_blocks_while_unique_dma_one_proceeds(self):
        entered = threading.Event()
        second_dma = threading.Event()
        names = []
        original = replay.SourceSealer.seal_packet
        def seal(stage, packet, queued_at):
            names.append(threading.current_thread().name)
            if packet[0] == 0:
                entered.set()
                self.assertTrue(second_dma.wait(2.), 'next DMA did not overlap source sealing')
            return original(stage, packet, queued_at)
        def dma(i):
            if i == 1:
                self.assertTrue(entered.wait(2.))
                second_dma.set()
        with tempfile.TemporaryDirectory(dir=helpers.TEMP) as folder, \
                patch.object(replay.SourceSealer, 'seal_packet', seal):
            output, calls = self.fake(folder, dma_hook=dma)
            self.assertEqual([v[1] for v in calls if isinstance(v, tuple)], list(range(8)))
            self.assertEqual(set(names), {'R34_source_snapshot_owner'})
            rows = json.loads((output/'TRACE.json').read_text())
            self.assertEqual([r['frame_id'] for r in rows], list(range(8)))
            self.assertTrue(all(r['source_queue_wait_s'] >= 0 and r['source_output_wait_s'] >= 0 for r in rows))

    def test_producer_done_is_not_source_done_and_does_not_drop_last_frame(self):
        original = replay.SourceSealer.seal_packet
        seen = []
        def seal(stage, packet, queued_at):
            if packet[0] == 0:
                self.assertTrue(stage.producer_done.wait(2.))
                self.assertFalse(stage.done.is_set())
                seen.append(packet[0])
            return original(stage, packet, queued_at)
        with tempfile.TemporaryDirectory(dir=helpers.TEMP) as folder, \
                patch.object(replay.SourceSealer, 'seal_packet', seal):
            output, _ = self.fake(folder, limit=1)
            result = json.loads((output/'RESULT.json').read_text())
            self.assertEqual(result['frames'], 1)
            self.assertEqual(seen, [0])
            self.assertTrue(result['readback_content_matches_live'])

    def test_source_failure_preserves_every_started_dma_and_joins(self):
        started = []
        original = replay.SourceSealer.seal_packet
        def seal(stage, packet, queued_at):
            if packet[0] == 1:
                raise ValueError('injected source validation')
            return original(stage, packet, queued_at)
        with tempfile.TemporaryDirectory(dir=helpers.TEMP) as folder, \
                patch.object(replay.SourceSealer, 'seal_packet', seal):
            with self.assertRaisesRegex(RuntimeError, 'injected source validation'):
                self.fake(folder, dma_hook=started.append)
            output = Path(folder)/'out'
            error = json.loads((output/'ERROR.json').read_text())
            kept = json.loads((output/'failure_evidence/MANIFEST.json').read_text())['frames']
            self.assertTrue(error['source_stopped'] and error['producer_stopped'])
            self.assertTrue(error['failure_evidence_complete'])
            self.assertTrue(set(started) <= {r['frame_id'] for r in kept} | set(error['persisted_frame_ids']))
            self.assertEqual(error['source_errors'][0]['frame_id'], 1)
            failed = next(row for row in kept if row['frame_id'] == 1)
            self.assertEqual(failed['failure'], 'ValueError: injected source validation')
            self.assertIn('raw', failed['components'])
            self.assertIn('primary', failed['components'])
            self.assertIn('observations_r33', failed['components'])
            self.assertFalse((output/'RESULT.json').exists())

    def test_stalled_source_join_times_out_and_quarantines_incomplete_evidence(self):
        entered, release = threading.Event(), threading.Event()
        stages = []
        original = replay.SourceSealer.seal_packet
        def seal(stage, packet, queued_at):
            stages.append(stage)
            entered.set()
            release.wait(3.)
            return original(stage, packet, queued_at)
        def dma(i):
            if i == 1:self.assertTrue(entered.wait(1.))
        try:
            with tempfile.TemporaryDirectory(dir=helpers.TEMP) as folder, \
                    patch.object(replay.SourceSealer, 'seal_packet', seal), \
                    patch.object(replay, 'WORKER_JOIN_TIMEOUT_S', .04):
                output, calls = self.fake(folder, fail_transport_index=1, dma_hook=dma)
                error = json.loads((output/'ERROR.json').read_text())
                self.assertFalse(error['source_stopped'])
                self.assertFalse(error['failure_evidence_complete'])
                self.assertIn('quarantine', calls)
                self.assertNotIn('release', calls)
        finally:
            release.set()
            for stage in stages:stage.join(2.)

    def test_full_output_queue_cancel_keeps_preseal_ownership(self):
        ledger = replay.AdmissionLedger(1000, pending_bytes=300)
        stop, producer_done = threading.Event(), threading.Event()
        output = queue.Queue(1)
        errors = []
        stage = replay.SourceSealer(lambda **kw: kw, ledger, stop, producer_done, output, errors)
        for i in range(2):
            ledger.reserve(i, 100, stop)
            frame = types.SimpleNamespace(payload=b'payload')
            ledger.update(i, raw=b'raw', primary=b'primary', observations_r33=frame.payload)
            stage.submit((i,b'raw',frame,b'primary',{},0,0,0,0,0))
        stage.start()
        deadline = time.monotonic()+2.
        while output.qsize() != 1 and time.monotonic()<deadline:time.sleep(.001)
        stop.set()
        self.assertEqual(replay.join_workers_until((stage,), 1.), [])
        self.assertEqual(set(ledger.snapshot()), {0,1})
        self.assertEqual(ledger.status()['pending_bytes'], 200)
        self.assertTrue(stage.done.is_set())
        self.assertFalse(errors)

    def test_queue_slots_do_not_grant_extra_memory_for_third_worst_frame(self):
        ledger = replay.AdmissionLedger(2**30, max_inflight_frames=128)
        worst = replay.capacity_budgets(8)[0]
        stop = threading.Event()
        stage = replay.SourceSealer(lambda **kw: kw, ledger, stop, threading.Event(), queue.Queue(4), [])
        self.assertEqual(stage.incoming.maxsize, 4)
        ledger.reserve(0, worst, stop)
        ledger.reserve(1, worst, stop)
        with self.assertRaisesRegex(TimeoutError, 'before DMA'):
            ledger.reserve(2, worst, stop, timeout_s=.001)
        self.assertLessEqual(ledger.status()['total_owned_bytes'], 64*1024**2)
        self.assertEqual(set(ledger.snapshot()), {0,1})

    def test_actual_source_bytes_and_descriptors_equal_serial_reference(self):
        self.check_source_and_serial_reference('identity')

    def test_zlib_source_bytes_are_shared_and_decode_to_original(self):
        self.check_source_and_serial_reference('zlib')

    def check_source_and_serial_reference(self, raw_encoding):
        values = fixture()
        ledger = replay.AdmissionLedger(2**30)
        stop = threading.Event()
        ledger.reserve(0, replay.capacity_budgets(8)[0], stop)
        output = queue.Queue(4)
        factory = partial(SourceSnapshot.create, raw_encoding=raw_encoding)
        stage = replay.SourceSealer(factory, ledger, stop, threading.Event(), output, [])
        packet = (0,values['raw'],values['frame'],values['primary'],dict(values['receipt']),0,0,0,0,0)
        serial_source = factory(**{key: values[key] for key in ('raw', 'primary', 'frame', 'receipt')})
        expected = FrameResult.from_source(serial_source, measurement_xyz=values['measurement_xyz'],
            preview_xyz=values['preview_xyz'], metadata=values['metadata'])
        stage.submit(packet)
        stage.producer_done.set()
        stage.start()
        ready = output.get(timeout=2.)
        self.assertEqual(replay.join_workers_until((stage,), 1.), [])
        actual = FrameResult.from_source(ready[10], measurement_xyz=values['measurement_xyz'],
            preview_xyz=values['preview_xyz'], metadata=values['metadata'])
        self.assertEqual(actual, expected)
        saved_raw = dict(actual.components)['raw']
        if raw_encoding == 'identity':
            self.assertIs(saved_raw, values['raw'])
        else:
            self.assertEqual(zlib.decompress(saved_raw), values['raw'])
            self.assertIs(saved_raw, dict(ready[10].components)['raw'])
        for name, size, sha, crc in actual.descriptors:
            content = dict(actual.components)[name]
            if name == 'raw' and raw_encoding == 'zlib':
                content = zlib.decompress(content)
            # V2 descriptors bind logical bytes, not compressed transport bytes.
            self.assertEqual(size, len(content))
            self.assertEqual(sha, hashlib.sha256(content).hexdigest())
            self.assertEqual(crc, zlib.crc32(content) & 0xffffffff)

    def test_cli_default_on_and_serial_ab_explicit(self):
        common = ['replay','--manifest','m','--input-root','i','--output','o','--target','T05']
        for extra, value in (([], True), (['--no-source-worker'], False), (['--source-worker'], True)):
            with self.subTest(extra=extra), patch('sys.argv', common+extra), patch.object(replay, 'run') as run:
                replay.main()
                self.assertIs(run.call_args.args[0].source_worker, value)
        real_run = replay.run
        def serial(args):
            args.source_worker = False
            return real_run(args)
        with tempfile.TemporaryDirectory(dir=helpers.TEMP) as folder, patch.object(replay, 'run', serial):
            output, _ = self.fake(folder)
            config = json.loads((output/'CONFIG.json').read_text())
            self.assertEqual(config['source_sealing']['backend'], 'sole_DMA_producer_serial')
            self.assertEqual(json.loads((output/'RESULT.json').read_text())['frames'], 8)


if __name__ == '__main__':
    unittest.main()
