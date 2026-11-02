"""CPU-only lazy display ownership/coalescing checks; no HDMI/board proof."""
from dataclasses import FrozenInstanceError
import threading
import unittest
import warnings
from unittest.mock import patch
import numpy as np

from .display import annotations_from_frame, deferred_annotations_from_frame
from .protocol import Frame
from .tests_display import make_frame
from candidate.display_publisher_r30 import DisplayPublisherR30, RAW_BYTES
from candidate.display_transport_r15 import pack_annotations


class Backend:
    def __init__(self, block=False, fail=False):
        self.entered = threading.Event(); self.release = threading.Event()
        self.block = block; self.fail = fail; self.frames = []

    def submit(self, gray, points, index, angle, **kwargs):
        self.frames.append((gray, points, index, angle, kwargs))
        self.entered.set()
        if self.block and len(self.frames) == 1 and not self.release.wait(3.):
            raise TimeoutError('test backend was not released')
        if self.fail: raise ValueError('injected display failure')


def frame_id(frame, index):
    return Frame(dict(frame.header, frame_id=index), frame.records, frame.rows, frame.payload)


class DeferredDisplayTests(unittest.TestCase):
    def test_freezes_identity_descriptors_options_without_sampling_or_record_copy(self):
        original = make_frame(12000); header = dict(original.header)
        source = Frame(header, original.records, original.rows, original.payload)
        with patch('r33.markers_native_r34.sample', side_effect=AssertionError('eager sampling')):
            with patch.dict('os.environ', {'SCAN3D_R34_MARKERS_BACKEND':'numpy'}):
                lazy = deferred_annotations_from_frame(source, actual_pl_execution=True)
        self.assertIs(lazy.frame.payload, original.payload)
        self.assertTrue(np.shares_memory(lazy.frame.records, original.records))
        self.assertIsNot(lazy.frame.records, original.records)
        header['frame_id'] = 8
        # Deliberately mutate the caller's ndarray descriptor to verify that
        # queued display descriptors are independent, even on newer NumPy.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            original.records.shape = (len(original.records)*16,)
        with patch.dict('os.environ', {'SCAN3D_R34_MARKERS_BACKEND':'invalid'}):
            result = lazy.materialize_compact()
        self.assertEqual(result.frame_id, 7)
        self.assertEqual(lazy.frame.records.shape, (12000,16))
        with self.assertRaises(TypeError): lazy.frame.header['frame_id'] = 9
        with self.assertRaises(ValueError): lazy.frame.records.setflags(write=True)
        with self.assertRaises(FrozenInstanceError): lazy.max_points = 1

    def test_exact_compact_output_all_frame_gates(self):
        for decisions in range(4):
            for count in (0, 4, 12000):
                frame = make_frame(count, decisions)
                lazy = deferred_annotations_from_frame(frame, actual_pl_execution=True, max_points=25)
                expected = annotations_from_frame(frame, actual_pl_execution=True, max_points=25, compact=True)
                self.assertEqual(pack_annotations(lazy.materialize_compact()), pack_annotations(expected))

    def test_maximum_diagnostic_frame_uses_eager_fallback_in_default_pool(self):
        from candidate.display_compact_r14 import CompactPLAnnotations
        frame = make_frame(180480)
        result = deferred_annotations_from_frame(frame, actual_pl_execution=True, max_points=512)
        self.assertIs(type(result), CompactPLAnnotations)
        expected = annotations_from_frame(frame, actual_pl_execution=True, max_points=512, compact=True)
        self.assertEqual(pack_annotations(result), pack_annotations(expected))
        backend = Backend(); publisher = DisplayPublisherR30(backend)
        try:
            publisher.submit(bytes(RAW_BYTES), np.empty((0,3)), 7, 0., result)
            summary = publisher.drain(3.)
            self.assertEqual(summary['published'], 1)
            self.assertLess(summary['peak_bytes'], 8*1024**2)
            self.assertIs(backend.frames[0][4]['annotations'], result)
        finally: publisher.close(3.)

    def _three_full_xyz_slots(self, count, cap, expected_type):
        frame = make_frame(count); backend = Backend(block=True)
        publisher = DisplayPublisherR30(backend); expected_marks = []
        try:
            for index in range(3):
                source = frame_id(frame, index)
                marks = deferred_annotations_from_frame(source, max_points=cap)
                self.assertIs(type(marks), expected_type)
                expected_marks.append(pack_annotations(annotations_from_frame(
                    source, max_points=cap, compact=True)))
                points = np.full((7680,3), index, np.float64)
                publisher.submit(bytes(RAW_BYTES), points, index, 0., marks, index+1)
                if index == 0: self.assertTrue(backend.entered.wait(2.))
            summary = publisher.summary()
            self.assertEqual((summary['active'], summary['pending'], summary['submitted']), (True,2,3))
            self.assertLessEqual(summary['retained_bytes'], 8*1024**2)
            backend.release.set(); summary = publisher.drain(3.)
            self.assertEqual((summary['accepted_points'], summary['published_points']), (3*7680,3*7680))
            self.assertEqual(summary['retained_bytes'], 0)
            self.assertEqual([item[2] for item in backend.frames], [0,1,2])
            for index, item in enumerate(backend.frames):
                np.testing.assert_array_equal(item[1], np.full((7680,3),index,np.float64))
                self.assertEqual(pack_annotations(item[4]['annotations']), expected_marks[index])
        finally:
            backend.release.set(); publisher.close(3.)

    def test_31900_records_and_7680_xyz_fit_active_and_two_pending(self):
        from candidate.display_compact_r14 import CompactPLAnnotations
        # Former 2 MiB annotation-only limit retained this source lazily;
        # three maximum XYZ deltas then overflowed the default pool at submit.
        self._three_full_xyz_slots(31900, 384, CompactPLAnnotations)

    def test_largest_lazy_source_reserves_raw_xyz_merge_and_metadata(self):
        from .display import DeferredR33Annotations, MAX_DEFERRED_BYTES
        for cap in (384,512):
            # This fixture's payload contains exactly 64 bytes per record.
            count = (MAX_DEFERRED_BYTES-cap*128-4096)//64
            self._three_full_xyz_slots(count, cap, DeferredR33Annotations)

    def test_rejects_mutable_alias_or_foreign_payload_and_bad_metadata(self):
        frame = make_frame(); records = frame.records.copy(); records.setflags(write=False)
        with self.assertRaisesRegex(ValueError, 'bytes-backed'):
            deferred_annotations_from_frame(Frame(frame.header, records, frame.rows, frame.payload))
        with self.assertRaisesRegex(ValueError, 'bytes-backed'):
            deferred_annotations_from_frame(Frame(frame.header, frame.records, frame.rows, bytes(bytearray(frame.payload))))
        with self.assertRaisesRegex(ValueError, 'ASCII'):
            deferred_annotations_from_frame(frame, geometry_evidence_type='\u6d4b\u8bd5')
        for value in (None, -1):
            bad = Frame(dict(frame.header, frame_id=value), frame.records, frame.rows, frame.payload)
            with self.assertRaises(ValueError): deferred_annotations_from_frame(bad)
        with self.assertRaisesRegex(ValueError, 'frame identity'):
            DisplayPublisherR30._annotations(deferred_annotations_from_frame(frame), 8)

    def test_coalescing_samples_only_survivors_and_keeps_every_point_delta(self):
        backend = Backend(block=True); publisher = DisplayPublisherR30(backend)
        raw = bytes(RAW_BYTES); calls = []
        original = annotations_from_frame
        def sample(frame, **kwargs):
            calls.append((frame.header['frame_id'], threading.current_thread().name))
            return original(frame, **kwargs)
        try:
            with patch('r33.display.annotations_from_frame', side_effect=sample):
                for index in range(4):
                    marks = deferred_annotations_from_frame(frame_id(make_frame(), index))
                    publisher.submit(raw, np.full((1,3), index, np.float64), index, index/10., marks, index+1)
                    if index == 0: self.assertTrue(backend.entered.wait(2.))
                self.assertEqual(calls, [(0, 'r30-display-publisher')])
                with publisher._condition:
                    self.assertIs(publisher._pending[-1].raw, raw)
                backend.release.set(); result = publisher.drain(3.)
            self.assertEqual([i for i, thread in calls], [0,1,3])
            self.assertTrue(all(thread == 'r30-display-publisher' for i, thread in calls))
            self.assertEqual([item[2] for item in backend.frames], [0,1,3])
            np.testing.assert_array_equal(np.concatenate([item[1] for item in backend.frames]),
                                          np.repeat(np.arange(4),3).reshape(4,3))
            self.assertEqual((result['accepted_points'], result['published_points'], result['coalesced']), (4,4,1))
            self.assertEqual(result['retained_bytes'], 0)
            gray = backend.frames[0][0]
            self.assertEqual(gray.shape, (480,752)); self.assertFalse(gray.flags.writeable)
            self.assertIs(gray.base.base, raw)
        finally:
            backend.release.set(); publisher.close(3.)

    def test_array_compatibility_snapshots_reused_or_strided_input(self):
        backend = Backend(block=True); publisher = DisplayPublisherR30(backend)
        image = np.full((480,1504), 19, np.uint8)[:,::2]
        try:
            publisher.submit(bytes(RAW_BYTES), np.empty((0,3)), 0, 0.)
            self.assertTrue(backend.entered.wait(2.))
            publisher.submit(image, np.empty((0,3)), 1, 0.)
            image[:] = 99
            backend.release.set(); publisher.drain(3.)
            np.testing.assert_array_equal(backend.frames[1][0], np.full((480,752),19,np.uint8))
            self.assertFalse(backend.frames[1][0].flags.writeable)
        finally:
            backend.release.set(); publisher.close(3.)

    def test_bad_raw_and_capacity_reject_before_acceptance(self):
        backend = Backend(); publisher = DisplayPublisherR30(backend, byte_capacity=RAW_BYTES+4096)
        try:
            for bad in (b'', bytes(RAW_BYTES-1), bytes(RAW_BYTES+1), bytearray(RAW_BYTES)):
                with self.assertRaises(ValueError): publisher.submit(bad, np.empty((0,3)), 7, 0.)
            lazy = deferred_annotations_from_frame(make_frame(12000))
            with self.assertRaises(BufferError): publisher.submit(bytes(RAW_BYTES), np.empty((0,3)),7,0.,lazy)
            self.assertEqual(publisher.summary()['submitted'], 0)
        finally: publisher.close(3.)

    def test_materialization_failure_retains_source_and_marks_failed_close(self):
        frame = make_frame(); records = frame.records.copy(); records[0,13] = 2 << 24
        payload = records.tobytes()
        invalid = Frame(frame.header, np.frombuffer(payload,'<u4').reshape(-1,16), frame.rows, payload)
        lazy = deferred_annotations_from_frame(invalid, marker_backend='numpy')
        backend = Backend(); publisher = DisplayPublisherR30(backend)
        publisher.submit(bytes(RAW_BYTES), np.empty((0,3)),7,0.,lazy)
        with self.assertRaisesRegex(RuntimeError, 'no center'): publisher.drain(3.)
        with self.assertRaisesRegex(RuntimeError, 'no center'): publisher.close(3.)
        result = publisher.summary()
        self.assertTrue(result['active']); self.assertGreater(result['retained_bytes'], 0)
        self.assertTrue(result['worker_joined']); self.assertFalse(backend.frames)
        self.assertIs(publisher._active.annotations, lazy)


if __name__ == '__main__': unittest.main()
