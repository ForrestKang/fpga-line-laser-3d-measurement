"""R34 compact display equivalence, ownership and IPC negative checks."""
import importlib
import json
import sys
import types
import unittest
from dataclasses import asdict
from unittest.mock import patch
import numpy as np
from .display import annotations_from_frame, renderer_factory
from .tests_display import make_frame
from candidate.display_compact_r14 import CompactPLAnnotations
from candidate.display_transport_r15 import pack_annotations, unpack_annotations
from candidate.display_publisher_r30 import DisplayPublisherR30


class CompactR34Tests(unittest.TestCase):
    def test_exact_frozen_marker_sample_all_modes_and_caps(self):
        for count in (0, 1, 4, 33, 12000):
            for decisions in range(4):
                for cap in (1, 2, 25, 384, 512):
                    frame = make_frame(count, decisions)
                    old = annotations_from_frame(frame, max_points=cap)
                    new = annotations_from_frame(frame, max_points=cap, compact=True)
                    self.assertIs(type(new), CompactPLAnnotations)
                    for field in asdict(old):
                        if field in ('measurement', 'preview', 'rejected'):
                            index = ('measurement', 'preview', 'rejected').index(field)
                            actual = tuple(map(tuple, new.groups[index].tolist()))
                            self.assertEqual(actual, getattr(old, field))
                        else:
                            self.assertEqual(getattr(new, field), getattr(old, field))
                    self.assertEqual(new.point_count, old.point_count)
                    for group in new.groups:
                        self.assertFalse(group.flags.writeable)
                        with self.assertRaises(ValueError): group.setflags(write=True)

    def test_maximum_diagnostic_input_keeps_bounded_marker_transport(self):
        frame = make_frame(180480)
        old = annotations_from_frame(frame, max_points=512)
        new = annotations_from_frame(frame, max_points=512, compact=True)
        self.assertEqual(new.point_count, 512)
        self.assertLess(len(pack_annotations(new)), 16384)
        for i, key in enumerate(('measurement', 'preview', 'rejected')):
            self.assertEqual(tuple(map(tuple, new.groups[i].tolist())), getattr(old, key))

    def test_owned_binary_ipc_crc_and_frame_binding(self):
        frame = make_frame(12000)
        new = annotations_from_frame(frame, actual_pl_execution=True, compact=True)
        snapshot, charge = DisplayPublisherR30._annotations(new, 7)
        self.assertIs(snapshot, new)
        payload = pack_annotations(new)
        self.assertGreater(charge, len(payload))
        restored = unpack_annotations(memoryview(payload), 7)
        for left, right in zip(new.groups, restored.groups):
            np.testing.assert_array_equal(left, right)
        self.assertEqual(asdict(new.metadata), asdict(restored.metadata))
        with self.assertRaisesRegex(ValueError, 'frame identity'):
            DisplayPublisherR30._annotations(new, 8)
        with self.assertRaisesRegex(ValueError, 'marker/frame mismatch'):
            unpack_annotations(memoryview(payload), 8)
        damaged = bytearray(payload); damaged[-1] ^= 1
        with self.assertRaisesRegex(ValueError, 'CRC mismatch'):
            unpack_annotations(memoryview(damaged), 7)
        for length in (0, 4, len(payload)-1):
            with self.assertRaises(ValueError): unpack_annotations(memoryview(payload)[:length], 7)

    def test_process_submit_uses_existing_binary_receiver(self):
        dashboard = types.ModuleType('structured_light.scan3d.dashboard')
        dashboard.DashboardStatus = object
        with patch.dict(sys.modules, {'structured_light.scan3d.dashboard': dashboard}):
            module = importlib.import_module('candidate.display_process_r25')
        marks = annotations_from_frame(make_frame(), actual_pl_execution=True, compact=True)
        fake = types.SimpleNamespace(_drain=lambda:None, _test_only=False, archive_slot_bytes=65536,
            _condition=types.SimpleNamespace(acquire=lambda **kwargs:False), error=None)
        with patch('candidate.display_transport_r15.pack_annotations', wraps=pack_annotations) as pack:
            with patch.object(module, '_marker_json_payload', side_effect=AssertionError('legacy tuple JSON used')):
                with self.assertRaisesRegex(RuntimeError, 'shared control lock'):
                    module.ProcessSessionDisplayR25.submit(fake, np.zeros((480,752), np.uint8), np.empty((0,3)), 7, 0., marks)
            pack.assert_called_once_with(marks)
        payload = pack_annotations(marks)
        restored = module._annotations(bytearray(payload), dict(kind=3, size=len(payload), index=7))
        for left, right in zip(marks.groups, restored.groups): np.testing.assert_array_equal(left, right)

    @patch('candidate.display_projection_r14.optional_projection', return_value=None)
    @patch('candidate.display_marker_native15c.optional_markers', return_value=None)
    def test_renderer_pixels_identical(self, _host_reference_markers, _host_reference_projection):
        from structured_light.scan3d.dashboard import DashboardStatus
        gray = np.zeros((480,752), np.uint8)
        for count in (0, 4, 331, 12000):
            frame = make_frame(count)
            old = annotations_from_frame(frame, actual_pl_execution=True)
            new = annotations_from_frame(frame, actual_pl_execution=True, compact=True)
            status = DashboardStatus(mode='SIMULATION', source='R34 annotation comparison', frame_number=7)
            legacy = renderer_factory(dict(target='T05')).render(gray, np.empty((0,3)), status=status, annotations=old).copy()
            compact = renderer_factory(dict(target='T05')).render(gray, np.empty((0,3)), status=status, annotations=new).copy()
            np.testing.assert_array_equal(legacy, compact)

    def test_argument_and_invalid_center_contract_unchanged(self):
        with self.assertRaises(TypeError): annotations_from_frame(make_frame(), compact=1)
        frame = make_frame(); records = frame.records.copy(); records[0,13] &= np.uint32(0xffffffff ^ (1<<24))
        from .protocol import Frame
        payload = records.tobytes()
        invalid = Frame(frame.header, np.frombuffer(payload, '<u4').reshape(-1,16), frame.rows, payload)
        with self.assertRaisesRegex(ValueError, 'no center'):
            annotations_from_frame(invalid, compact=True)


if __name__ == '__main__': unittest.main()
