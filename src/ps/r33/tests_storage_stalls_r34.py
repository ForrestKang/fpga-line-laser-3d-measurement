"""Timed local fsync stalls. Not SD-card or optical evidence."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from .storage import AsyncWriter, FrameResult, readback, MAX_BYTES
from .tests_storage import fixture


class TimedStalls(unittest.TestCase):
    def test_local_10hz_arrivals_05_2_8_15_second_stalls(self):
        temp = Path(os.environ.get('R33_TEST_TEMP', 'D:/Workspace/CodexTemp/r34_storage_tests'))
        temp.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix='timed_stalls_', dir=temp))
        snapshots = [FrameResult.create(**fixture(i)) for i in range(150)]
        results = []
        for delay in (.5, 2., 8., 15.):
            with self.subTest(delay=delay):
                entered, release = threading.Event(), threading.Event()
                times = {}
                def sync(fd):
                    if not entered.is_set():
                        times['sync_enter_ns'] = time.perf_counter_ns()
                        entered.set()
                        threading.Timer(delay, release.set).start()
                    if not release.wait(delay+5):
                        raise TimeoutError('injected stall did not release')
                    os.fsync(fd)
                writer = AsyncWriter(root/str(delay), capacity_frames=128,
                    group=8, max_delay_s=.01, fsync=sync)
                accepted = 0
                rejected = False
                submit_ms = []
                maximum_pending = maximum_bytes = maximum_during_stall = 0
                target = min(150, int(delay*10)+15)
                start = time.perf_counter()
                try:
                    for snapshot in snapshots[:target]:
                        remaining = start+accepted/10-time.perf_counter()
                        if remaining > 0:
                            time.sleep(remaining)
                        before = time.perf_counter()
                        try:
                            writer.submit(snapshot, timeout_s=.02)
                        except BufferError:
                            rejected = True
                            break
                        accepted += 1
                        submit_ms.append((time.perf_counter()-before)*1000)
                        state = writer.counters()
                        maximum_pending = max(maximum_pending, state['pending_frames'])
                        maximum_bytes = max(maximum_bytes, state['pending_bytes'])
                        if entered.is_set() and not release.is_set():
                            maximum_during_stall = max(maximum_during_stall, state['persisted'])
                        self.assertLessEqual(state['pending_frames'], 128)
                        self.assertLessEqual(state['pending_bytes'], MAX_BYTES)
                    writer.close(timeout_s=delay+5)
                finally:
                    release.set()
                    writer.close(timeout_s=5)
                verified = readback(writer.output)
                self.assertEqual(verified['frames'], accepted)
                self.assertEqual(maximum_during_stall, 0)
                if delay <= 8:
                    self.assertFalse(rejected)
                    self.assertEqual(accepted, target)
                else:
                    self.assertTrue(rejected)
                results.append(dict(delay_s=delay, input_hz=10, accepted=accepted,
                    requested=target, bounded_backpressure=rejected,
                    max_pending_frames=maximum_pending, max_pending_bytes=maximum_bytes,
                    max_submit_ms=max(submit_ms), early_durability_acks=maximum_during_stall,
                    readback=verified, wall_s=time.perf_counter()-start))
        report = dict(evidence='LOCAL_INJECTED_FSYNC_DELAY_NOT_BOARD_SD',
            all_passed=True, queue_bytes_limit=MAX_BYTES, queue_frame_limit=128,
            results=results)
        out = Path(__file__).resolve().parents[2]/'evidence/R34_LOCAL_STORAGE_STALLS.json'
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    unittest.main()
