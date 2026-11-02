"""R34 pre-seal geometry failure evidence and ownership tests; no board IO."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
import weakref
import gc

import numpy as np

from . import replay
from .storage import FrameResult, SourceSnapshot
from .tests_storage import fixture


class GeometryFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = fixture()
        cls.source = SourceSnapshot.create(**{
            key: cls.values[key] for key in ('raw', 'primary', 'frame', 'receipt')})

    def setUp(self):
        base = Path(os.environ.get('R33_TEST_TEMP', 'D:/Workspace/CodexTemp/r34_storage_tests'))
        base.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='geometry_failure_', dir=base))
        self.ledger = replay.AdmissionLedger(2**30)
        self.worst = replay.capacity_budgets(8)[0]
        self.ledger.reserve(0, self.worst, threading.Event())
        self.ledger.update(0, state='geometry_current', raw=self.values['raw'],
            primary=self.values['primary'], observations_r33=self.values['frame'].payload,
            receipt=self.values['receipt'], PL_started=True, PL_returned=True)

    def preserve(self, geometry):
        self.ledger.update(0, geometry_result=geometry)
        return replay.preserve_pending(self.folder, self.ledger.snapshot())[0]

    def assert_component(self, row, name, expected):
        path = self.folder/'failure_evidence/frame_0000'/(name+'.bin')
        actual = path.read_bytes()
        self.assertEqual(actual, expected)
        self.assertEqual(row['components'][name], {
            'bytes': len(expected), 'sha256': hashlib.sha256(expected).hexdigest()})

    def test_from_source_identity_failure_retains_sources_and_both_geometry_modes(self):
        geometry = types.SimpleNamespace(
            measurement_axis_mm=self.values['measurement_xyz'],
            preview_axis_mm=self.values['preview_xyz'])
        self.ledger.update(0, geometry_result=geometry)
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            FrameResult.from_source(self.source,
                measurement_xyz=geometry.measurement_axis_mm,
                preview_xyz=geometry.preview_axis_mm, metadata={'frame_id': 999})
        row = replay.preserve_pending(self.folder, self.ledger.snapshot())[0]
        for name, content in (('raw', self.values['raw']), ('primary', self.values['primary']),
                              ('observations_r33', self.values['frame'].payload),
                              ('measurement_xyz', geometry.measurement_axis_mm.astype('<f8').tobytes()),
                              ('preview_xyz', geometry.preview_axis_mm.astype('<f8').tobytes())):
            self.assert_component(row, name, content)
        self.assertTrue(row['PL_started'] and row['PL_returned'])
        self.assertNotIn('result', self.ledger.snapshot()[0])
        self.assertEqual(self.ledger.status()['pending_bytes'], self.worst)

    def test_noncontiguous_finite_and_empty_geometry_use_explicit_float64_contract(self):
        measurement = np.arange(24, dtype=np.float32).reshape(4, 6)[:, ::2]
        self.assertFalse(measurement.flags.c_contiguous)
        row = self.preserve(types.SimpleNamespace(measurement_axis_mm=measurement,
            preview_axis_mm=np.empty((0, 3), dtype=np.float32)))
        self.assert_component(row, 'measurement_xyz', measurement.astype('<f8').tobytes())
        self.assert_component(row, 'preview_xyz', b'')
        loaded = np.fromfile(self.folder/'failure_evidence/frame_0000/measurement_xyz.bin', '<f8').reshape(-1, 3)
        np.testing.assert_array_equal(loaded, measurement)

    def test_invalid_geometry_is_not_published_as_valid_xyz(self):
        row = self.preserve(types.SimpleNamespace(measurement_axis_mm=np.ones((2, 2)),
            preview_axis_mm=np.array([[1., np.nan, 3.]])))
        self.assertNotIn('measurement_xyz', row['components'])
        self.assertNotIn('preview_xyz', row['components'])
        self.assert_component(row, 'raw', self.values['raw'])
        self.assert_component(row, 'primary', self.values['primary'])

    def test_seal_releases_geometry_reference_and_preserves_immutable_result(self):
        class Geometry:
            pass
        geometry = Geometry()
        geometry.measurement_axis_mm = self.values['measurement_xyz'].copy()
        geometry.preview_axis_mm = self.values['preview_xyz'].copy()
        self.ledger.update(0, geometry_result=geometry)
        reference = weakref.ref(geometry)
        result = FrameResult.from_source(self.source,
            measurement_xyz=geometry.measurement_axis_mm,
            preview_xyz=geometry.preview_axis_mm, metadata=self.values['metadata'])
        self.ledger.seal(0, result)
        self.assertNotIn('geometry_result', self.ledger.snapshot()[0])
        geometry.measurement_axis_mm[:] = -999
        del geometry
        gc.collect()
        self.assertIsNone(reference())
        row = replay.preserve_pending(self.folder, self.ledger.snapshot())[0]
        for name, blob in result.components:
            self.assert_component(row, name, blob)
        self.assertEqual(self.ledger.status()['pending_bytes'], result.charge_bytes)

    def test_preparation_handoff_and_shared_source_bytes_remain_bounded(self):
        ledger = replay.AdmissionLedger(2**30, max_inflight_frames=128)
        stop = threading.Event()
        ledger.prepare(0, stop)
        ledger.prepared_ready(0, self.values['raw'])
        self.assertEqual(ledger.status()['prepared_frames'], 1)
        ledger.reserve(0, self.worst, stop, transfer_prepared=True)
        self.assertEqual(ledger.status()['prepared_bytes'], 0)
        result = FrameResult.from_source(self.source,
            measurement_xyz=self.values['measurement_xyz'],
            preview_xyz=self.values['preview_xyz'], metadata=self.values['metadata'])
        self.assertIs(dict(self.source.components)['raw'], dict(result.components)['raw'])
        ledger.seal(0, result)
        self.assertEqual(ledger.status()['total_owned_bytes'], result.charge_bytes)
        self.assertLessEqual(ledger.status()['peak_total_owned_bytes'], 64*1024**2)
        with self.assertRaisesRegex(BufferError, 'pending ownership capacity'):
            ledger.reserve(1, 64*1024**2+1, stop)
        ledger.refresh([{'frame_id': 0}])
        self.assertEqual(ledger.status()['total_owned_bytes'], 0)


if __name__ == '__main__':
    unittest.main()
