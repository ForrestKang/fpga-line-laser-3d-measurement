"""R33 display-only ownership, UV classification and existing IPC tests."""
from dataclasses import asdict
import json
import importlib
import sys
import types
from types import MappingProxyType
import unittest
from unittest.mock import patch
import numpy as np
from .protocol import Frame
from .display import annotations_from_frame, renderer_factory


def make_frame(count=4, decisions=3):
    a = np.zeros((count, 16), '<u4')
    a[:, 0] = np.arange(count); a[:, 1] = (np.arange(count)%300)+100
    a[:, 2] = (np.arange(count)%300)+20; a[:, 3] = a[:, 2]+3
    a[:, 4] = (np.arange(count)%50)*40960+100*4096+2458
    a[:, 5] = a[:, 1]*4096+2458
    a[:, 13] = (1 << 24) | (1 << 26)
    if count: a[0, 13] |= 1 << 25
    if count > 2: a[2, 13] = 0
    if count > 3: a[3, 13] = (1 << 24) | (1 << 8)
    payload = a.tobytes()
    h = dict(algorithm_revision=33, width=752, height=480, source=2,
             frame_id=7, frame_decisions=decisions, qualified_count=count,
             measurement_count=int(np.count_nonzero((a[:, 13] >> 25) & 1)),
             preview_count=int(np.count_nonzero((a[:, 13] >> 26) & 1)),
             processing_cycles_lo=123, processing_cycles_hi=2)
    return Frame(MappingProxyType(h), np.frombuffer(payload, '<u4').reshape(-1, 16),
                 np.zeros((480, 16), '<u4'), payload)


class DisplayTests(unittest.TestCase):
    def test_uv_uses_fractional_v_for_raster_only(self):
        frame = make_frame(); before = frame.payload
        out = annotations_from_frame(frame, actual_pl_execution=True)
        self.assertEqual(out.measurement, ((101*4096, 101, 0),))
        self.assertEqual(out.preview, ((111*4096, 102, 0),))
        self.assertEqual(out.rejected, ((22*4096, 102, 1), (131*4096, 104, 0)))
        self.assertEqual(frame.payload, before)
        self.assertEqual(out.processing_cycles, (2 << 32)+123)
        self.assertEqual(out.backend, 'ACTUAL_PL_R33_DDR_REPLAY')

    def test_frame_quality_gate_changes_only_marker_classes(self):
        out = annotations_from_frame(make_frame(decisions=2))
        self.assertEqual(out.measurement, ())
        self.assertEqual(len(out.preview), 2)
        out = annotations_from_frame(make_frame(decisions=0))
        self.assertEqual((len(out.measurement), len(out.preview), len(out.rejected)), (0, 0, 4))

    def test_bounded_fair_sample_and_immutable_tuple(self):
        out = annotations_from_frame(make_frame(12000), max_points=25)
        self.assertEqual(out.point_count, 25)
        self.assertEqual(len(out.measurement), 1)
        self.assertGreater(len(out.preview), 0)
        self.assertGreater(len(out.rejected), 0)
        self.assertIsInstance(out.preview[0][0], int)
        with self.assertRaises(ValueError): annotations_from_frame(make_frame(), max_points=513)

    def test_empty_and_evidence_validation(self):
        self.assertEqual(annotations_from_frame(make_frame(0)).point_count, 0)
        f = make_frame(); h = dict(f.header, source=3)
        with self.assertRaisesRegex(ValueError, 'fixture'):
            annotations_from_frame(Frame(h, f.records, f.rows, f.payload), actual_pl_execution=True)
        with self.assertRaises(TypeError): annotations_from_frame(object())
        with self.assertRaises(TypeError): annotations_from_frame(f, actual_pl_execution=1)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            annotations_from_frame(Frame(f.header, f.records.copy(), f.rows, f.payload))

    def test_existing_json_marker_ipc_never_decodes_old_archive(self):
        from candidate.display_publisher_r30 import DisplayPublisherR30
        # The IPC receiver does not draw anything. A dashboard import stub is
        # sufficient on the offline host with no cv2; the real renderer test
        # below still requires real cv2 and must run on the board separately.
        dashboard = types.ModuleType('structured_light.scan3d.dashboard')
        dashboard.DashboardStatus = object
        with patch.dict(sys.modules, {'structured_light.scan3d.dashboard': dashboard}):
            _annotations = importlib.import_module('candidate.display_process_r25')._annotations
        out = annotations_from_frame(make_frame(), actual_pl_execution=True)
        with patch('candidate.pfo3_r25.decode', side_effect=AssertionError('old decode called')):
            snapshot, charge = DisplayPublisherR30._annotations(out, 7)
            encoded = json.dumps(asdict(snapshot), separators=(',', ':')).encode('ascii')
            restored = _annotations(bytearray(encoded), dict(kind=2, size=len(encoded), index=7))
            self.assertEqual(restored, out)
            self.assertGreater(charge, len(encoded))
            self.assertLess(len(encoded), 4096)

    def test_batched_sampling_matches_frozen_scalar_contract(self):
        # Independent scalar classification and fair sampling reproduces the
        # original three-group path, including valid rejected centers.
        from .display import _quotas
        for count in (0, 1, 4, 33, 12000):
            for decisions in range(4):
                for cap in (1, 2, 25, 384, 512):
                    frame = make_frame(count, decisions)
                    a = frame.records; groups = [[], [], []]
                    for record in a:
                        flags = int(record[13]); valid = bool(flags & (1 << 24))
                        m = bool(flags & (1 << 25)) and bool(decisions & 1)
                        p = bool(flags & (1 << 26)) and bool(decisions & 2) and not m
                        u = (int(record[4])+2048)//4096 if valid else int(record[2])
                        v = (int(record[5])+2048)//4096 if valid else int(record[1])
                        groups[0 if m else 1 if p else 2].append((min(u,751)*4096,min(v,479),int(not valid)))
                    expected=[]
                    for group, quota in zip(groups, _quotas(list(map(len,groups)),cap)):
                        selected = group if quota == len(group) else [group[k*(len(group)-1)//max(1,quota-1)] for k in range(quota)]
                        expected.append(tuple(selected))
                    out = annotations_from_frame(frame,max_points=cap)
                    self.assertEqual((out.measurement,out.preview,out.rejected),tuple(expected))

    def test_json_shallow_serializer_exact_bytes_and_legacy_receiver(self):
        from dataclasses import replace
        dashboard = types.ModuleType('structured_light.scan3d.dashboard')
        dashboard.DashboardStatus = object
        with patch.dict(sys.modules, {'structured_light.scan3d.dashboard': dashboard}):
            module = importlib.import_module('candidate.display_process_r25')
        for count in (0, 4, 331, 12000):
            out = annotations_from_frame(make_frame(count),actual_pl_execution=True,max_points=512)
            # Include a historical marker envelope at its complete 2400 cap.
            cases = [out,replace(out,measurement=(),preview=(),rejected=((0,0,1),)*2400)]
            for marks in cases:
                before = asdict(marks)
                expected = json.dumps(before,separators=(',',':'),allow_nan=False).encode('ascii')
                actual = module._marker_json_payload(marks)
                self.assertEqual(actual,expected)
                self.assertEqual(asdict(marks),before)
                restored = module._annotations(bytearray(actual),dict(kind=2,size=len(actual),index=7))
                self.assertEqual(restored,marks)
        with self.assertRaises(TypeError): module._marker_json_payload(object())

    def test_process_submit_uses_shallow_path_after_identity_validation(self):
        dashboard = types.ModuleType('structured_light.scan3d.dashboard')
        dashboard.DashboardStatus = object
        with patch.dict(sys.modules, {'structured_light.scan3d.dashboard': dashboard}):
            module = importlib.import_module('candidate.display_process_r25')
        marks=annotations_from_frame(make_frame(),actual_pl_execution=True)
        fake=types.SimpleNamespace(_drain=lambda:None,_test_only=False,archive_slot_bytes=65536,
            _condition=types.SimpleNamespace(acquire=lambda **kwargs:False),error=None)
        with patch.object(module,'_marker_json_payload',wraps=module._marker_json_payload) as encode:
            with patch('dataclasses.asdict',side_effect=AssertionError('deepcopy serializer called')):
                with self.assertRaisesRegex(RuntimeError,'shared control lock'):
                    module.ProcessSessionDisplayR25.submit(fake,np.zeros((480,752),np.uint8),np.empty((0,3)),7,0.,marks)
            encode.assert_called_once_with(marks)
        with patch.object(module,'_marker_json_payload') as encode:
            with self.assertRaisesRegex(ValueError,'frame identity'):
                module.ProcessSessionDisplayR25.submit(fake,np.zeros((480,752),np.uint8),np.empty((0,3)),8,0.,marks)
            encode.assert_not_called()
        data=module._marker_json_payload(marks)
        with self.assertRaisesRegex(ValueError,'marker/frame mismatch'):
            module._annotations(bytearray(data),dict(kind=2,size=len(data),index=8))

    def test_rounding_edge_and_accepted_center_validation(self):
        f=make_frame(); a=f.records.copy()
        a[0,4]=752*4096-1; a[0,5]=480*4096-1
        payload=a.tobytes(); f=Frame(f.header,np.frombuffer(payload,'<u4').reshape(-1,16),f.rows,payload)
        self.assertEqual(annotations_from_frame(f).measurement,((751*4096,479,0),))
        a=a.copy(); a[0,13] &= np.uint32(0xffffffff ^ (1 << 24))
        payload=a.tobytes(); f=Frame(f.header,np.frombuffer(payload,'<u4').reshape(-1,16),f.rows,payload)
        with self.assertRaisesRegex(ValueError,'no center'): annotations_from_frame(f)

    @patch('candidate.display_projection_r14.optional_projection', return_value=None)
    def test_real_renderer_and_marker_colors(self, _host_reference_projection):
        from structured_light.scan3d.dashboard import DashboardStatus, CAMERA_IMAGE
        from candidate.display_pfo3_r25 import MEASUREMENT_BGR, PREVIEW_BGR, REJECTED_BGR
        renderer = renderer_factory(dict(target='T05'))
        status = DashboardStatus(mode='SIMULATION', source='R33 test', frame_number=7)
        marks = annotations_from_frame(make_frame(), actual_pl_execution=True)
        with patch('candidate.pfo3_r25.decode', side_effect=AssertionError('old decode called')):
            canvas = renderer.render(np.zeros((480, 752), np.uint8), np.empty((0, 3)), status=status, annotations=marks)
        self.assertEqual((canvas.shape, canvas.dtype), ((720, 1280, 3), np.dtype('uint8')))
        cx, cy, _, _ = CAMERA_IMAGE
        for x, y, color in [(101, 101, MEASUREMENT_BGR), (111, 102, PREVIEW_BGR), (131, 104, REJECTED_BGR)]:
            np.testing.assert_array_equal(canvas[cy+y, cx+x], color)
        self.assertEqual(renderer.timings['annotation_points'], 4)
        self.assertEqual(renderer.timings['renderer'], 'R33_UV_OBSERVATIONS_NATIVE_RAW_XYZ')
        self.assertEqual(renderer.timings['marker_coordinate_contract'], 'R33_UV_Q12_DISPLAY_ROUND_ONLY')

    @patch('candidate.display_projection_r14.optional_projection', return_value=None)
    def test_real_renderer_no_snapshot_labels_and_status_canvas(self, _host_reference_projection):
        from structured_light.scan3d.dashboard import DashboardStatus
        renderer = renderer_factory(dict(target='T06'))
        status = DashboardStatus(mode='SIMULATION', source='R33 | computed 7 | saved 6', frame_number=7)
        lines = []
        original = renderer._draw_line
        def record_line(canvas, value, position, *args, **kwargs):
            lines.append((value, position))
            return original(canvas, value, position, *args, **kwargs)
        renderer._draw_line = record_line
        canvas = renderer.render(np.zeros((480, 752), np.uint8), np.empty((0, 3)), status=status)
        self.assertEqual([text for text, where in lines if where == (12, 17)][-1], 'R33 / T06')
        self.assertIn(('R33 / NO OBSERVATION SNAPSHOT FOR THIS DISPLAY FRAME', (250, 49)), lines)
        np.testing.assert_array_equal(renderer._status_canvas, canvas)
        self.assertIsNotNone(renderer.render_status_only('R33 saving'))


if __name__ == '__main__': unittest.main()
