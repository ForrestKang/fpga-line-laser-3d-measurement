"""Independent scalar geometry comparison; no hardware operations."""
import copy
import json
import os
from pathlib import Path
import unittest
from types import MappingProxyType
import numpy as np
from sl_geometry.geometry import pixel_rays, intersect_plane, unrotate_points
from .protocol import Frame
from .geometry import load_calibration, parse_calibration, reconstruct_pair

_LOCAL_CALIBRATION = 'D:/Workspace/CodexWork/scan3d_r30_pipeline_20261024_01/bundle/ARM_SOFTWARE16/inputs/T05/calibration.json'
CALIBRATION_PATH = Path(os.environ.get('R33_TEST_CALIBRATION',
    Path(os.environ['R33_TEST_FIXTURES'])/'calibration.json'
    if 'R33_TEST_FIXTURES' in os.environ else _LOCAL_CALIBRATION))


def frame_of(uv, m=None, p=None):
    uv = np.asarray(uv).reshape(-1, 2)
    n = len(uv); a = np.zeros((n, 16), dtype='<u4')
    a[:, 0] = np.arange(n); a[:, 1] = uv[:, 1].astype(int)
    a[:, 4:6] = np.rint(uv*4096).astype(np.uint32)
    if m is None: m = np.ones(n, bool)
    if p is None: p = np.ones(n, bool)
    a[:, 13] = (1 << 24) | (np.asarray(m, np.uint32) << 25) | (np.asarray(p, np.uint32) << 26)
    payload = a.tobytes()
    return Frame(MappingProxyType({'algorithm_revision': 33, 'frame_decisions': 3}),
                 np.frombuffer(payload, '<u4').reshape(-1, 16), np.zeros((480, 16), '<u4'), payload)


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.cal = load_calibration(CALIBRATION_PATH)
        self.raw_cal = json.loads(CALIBRATION_PATH.read_text())

    def test_same_uv_high_precision_scalar_reference(self):
        rng = np.random.default_rng(3301)
        uv = np.column_stack((rng.uniform(220, 440, 1400), rng.uniform(30, 450, 1400)))
        f = frame_of(uv, np.arange(len(uv)) % 3 != 0)
        out = reconstruct_pair(f, self.cal, angle_rad=1.234)
        exact_uv = f.records[:, 4:6].astype(float)/4096
        # Independent scalar reference derives each ray using the inverse K,
        # then delegates plane intersection / rotation to the frozen helpers.
        invk = np.linalg.inv(self.cal.camera_matrix)
        ref_rays = np.asarray([invk @ np.r_[pixel, 1.] for pixel in exact_uv])
        ref = intersect_plane(ref_rays, self.cal.plane)
        inverse = unrotate_points(ref['points_mm'][ref['valid']], self.cal.axis_point_mm,
                                  self.cal.axis_direction, 1.234)
        expected_axis = (inverse-self.cal.axis_point_mm) @ self.cal.basis_camera
        np.testing.assert_allclose(out.preview_camera_mm, ref['points_mm'][ref['valid']], rtol=0, atol=2e-12)
        np.testing.assert_allclose(out.preview_axis_mm, expected_axis, rtol=0, atol=2e-12)
        mi = f.accepted('measurement') & ref['valid']
        np.testing.assert_allclose(out.measurement_camera_mm, ref['points_mm'][mi], rtol=0, atol=2e-12)
        self.assertEqual(out.evidence_type, 'SIMULATION_ONLY')
        self.assertEqual(len(out.calibration_sha256), 64)
        self.assertTrue(all(x >= 0 for x in out.timing_ms.values()))

    def test_fractional_v_is_not_integer_row(self):
        a = reconstruct_pair(frame_of([[350, 170.25]]), self.cal)
        b = reconstruct_pair(frame_of([[350, 170.75]]), self.cal)
        self.assertGreater(np.linalg.norm(a.measurement_camera_mm-b.measurement_camera_mm), .01)

    def test_modes_are_independent(self):
        f = frame_of([[350, 160.1], [360, 180.2], [370, 220.3]], [True, False, True])
        out = reconstruct_pair(f, self.cal)
        self.assertEqual(len(out.measurement_axis_mm), 2)
        self.assertEqual(len(out.preview_axis_mm), 3)
        before = out.preview_axis_mm.copy()
        out.measurement_axis_mm[:] = 0
        np.testing.assert_array_equal(out.preview_axis_mm, before)
        self.assertFalse(np.shares_memory(out.preview_ids, out.measurement_ids))

    def test_empty(self):
        out = reconstruct_pair(frame_of([]), self.cal)
        self.assertEqual(out.measurement_camera_mm.shape, (0, 3))
        self.assertEqual(out.preview_axis_mm.shape, (0, 3))
        self.assertEqual(out.rejection_counts, {'measurement': {}, 'preview': {}})

    def test_frame_quality_gate_is_independent_for_each_mode(self):
        f = frame_of([[350, 200], [360, 220]])
        for decisions, counts in [(0, (0, 0)), (2, (0, 2)), (3, (2, 2))]:
            gated = Frame(MappingProxyType(dict(f.header, frame_decisions=decisions)),
                          f.records, f.rows, f.payload)
            out = reconstruct_pair(gated, self.cal)
            self.assertEqual((len(out.measurement_ids), len(out.preview_ids)), counts)
            if decisions == 2:
                self.assertEqual(out.rejection_counts['measurement'], {'frame_quality': 2})
        bad = Frame(MappingProxyType({'algorithm_revision': 33}), f.records, f.rows, f.payload)
        with self.assertRaisesRegex(ValueError, 'frame decisions'):
            reconstruct_pair(bad, self.cal)

    def test_range_and_bounds_rejections(self):
        d = copy.deepcopy(self.raw_cal); d['ray_range_mm'] = [0, 1]
        out = reconstruct_pair(frame_of([[350, 200]]), parse_calibration(d))
        self.assertEqual(out.rejection_counts['measurement'], {'out_of_range': 1})
        d.pop('ray_range_mm'); d['training_bounds_mm'] = [[0, 0, 0], [1, 1, 1]]
        out = reconstruct_pair(frame_of([[350, 200]]), parse_calibration(d))
        self.assertEqual(out.rejection_counts['preview'], {'outside_training_bounds': 1})

    def test_parallel_and_behind_rejections(self):
        d = copy.deepcopy(self.raw_cal); d['laser_plane_camera'] = [1, 0, 0, -1]
        out = reconstruct_pair(frame_of([[375.5, 200], [350, 200], [400, 200]]), parse_calibration(d))
        self.assertEqual(out.rejection_counts['preview'], {'parallel': 1, 'behind': 1})
        np.testing.assert_array_equal(out.preview_ids, [2])

    def test_invalid_calibrations(self):
        cases = [('simulation_only', False), ('units', 'm'), ('image_size', [480, 752]),
                 ('evidence_type', 'PHYSICAL'), ('rotation_angle_sign', 0),
                 ('laser_plane_camera', [0, 0, 0, 1]), ('axis_direction_camera', [0, 0, 0]),
                 ('radial_reference_camera', [0, -1, 0]), ('distortion', [0, 0]),
                 ('camera_matrix', [[0,0,0],[0,0,0],[0,0,1]]),
                 ('axis_point_camera_mm', [0, float('nan'), 0]),
                 ('laser_plane_camera', [1e-320, 0, 0, 1e300])]
        for name, value in cases:
            with self.subTest(name=name):
                d = copy.deepcopy(self.raw_cal); d[name] = value
                with self.assertRaises(ValueError): parse_calibration(d)
        d = copy.deepcopy(self.raw_cal); del d['camera_matrix']
        with self.assertRaises(ValueError): parse_calibration(d)

    def test_invalid_center_and_angle(self):
        f = frame_of([[350, 200]])
        a = f.records.copy(); a[0, 13] &= np.uint32(0xfeffffff)
        bad = Frame(f.header, a, f.rows, f.payload)
        with self.assertRaises(ValueError): reconstruct_pair(bad, self.cal)
        for angle in [True, float('nan'), float('inf'), '1.0', None]:
            with self.assertRaises(ValueError): reconstruct_pair(f, self.cal, angle_rad=angle)


if __name__ == '__main__':
    unittest.main()
