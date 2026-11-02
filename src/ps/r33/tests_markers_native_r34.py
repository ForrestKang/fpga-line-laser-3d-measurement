"""Optional C display sampler equivalence and ownership; no board operations."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from types import MappingProxyType
from unittest.mock import patch
import unittest
import numpy as np
from .display import annotations_from_frame
from .markers_native_r34 import backend_status, sample
from .protocol import Frame
from .tests_display import make_frame
from candidate.display_transport_r15 import pack_annotations


def modified(frame, edit):
    a = frame.records.copy()
    edit(a)
    payload = a.tobytes()
    return Frame(frame.header, np.frombuffer(payload, '<u4').reshape(-1, 16), frame.rows, payload)


class NativeMarkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not backend_status()['available']:
            raise RuntimeError('build optional marker library before native-specific tests')

    def compare(self, frame, cap=384):
        reference = annotations_from_frame(frame, max_points=cap, compact=True, marker_backend='numpy')
        result = annotations_from_frame(frame, max_points=cap, compact=True, marker_backend='native')
        self.assertEqual(asdict(result.metadata), asdict(reference.metadata))
        self.assertEqual(pack_annotations(result), pack_annotations(reference))
        for left, right in zip(result.groups, reference.groups):
            np.testing.assert_array_equal(left, right)
            self.assertFalse(left.flags.writeable)
            with self.assertRaises(ValueError):
                left.setflags(write=True)
        return result

    def test_all_gates_sizes_and_quotas(self):
        for n in (0, 1, 2, 4, 33, 12000, 180480):
            for decisions in range(4):
                for cap in (1, 2, 3, 25, 384, 512):
                    self.compare(make_frame(n, decisions), cap)

    def test_randomized_unequal_groups_rounding_and_bounds(self):
        rng = np.random.RandomState(3491)
        for n in (1, 71, 599, 5000):
            frame = make_frame(n)
            def edit(a):
                a[:, 1] = rng.randint(0, 480, n)
                a[:, 2] = rng.randint(0, 752, n)
                a[:, 4] = rng.randint(0, 752*4096, n)
                a[:, 5] = rng.randint(0, 480*4096, n)
                a[:, 13] = rng.randint(0, 8, n).astype(np.uint32) << 24
                a[:, 13] |= np.uint32(1 << 24)
            frame = modified(frame, edit)
            for cap in (1, 7, 128, 512):
                self.compare(frame, cap)
        edge = modified(make_frame(4), lambda a: (a.__setitem__((slice(None),4),752*4096-1),
                        a.__setitem__((slice(None),5),480*4096-1)))
        self.compare(edge)

    def test_negative_center_coordinate_and_outer_validation(self):
        cases = [modified(make_frame(), lambda a: a.__setitem__((0,13),2 << 24)),
                 modified(make_frame(), lambda a: a.__setitem__((0,4),0xffffffff)),
                 modified(make_frame(), lambda a: a.__setitem__((2,2),0xffffffff)),
                 modified(make_frame(), lambda a: a.__setitem__((0,5),0xffffffff))]
        for frame in cases:
            errors = []
            for backend in ('numpy','native'):
                with self.assertRaises(ValueError) as cm:
                    annotations_from_frame(frame, marker_backend=backend, compact=True)
                errors.append(str(cm.exception))
            self.assertEqual(errors[0], errors[1])
        f = make_frame()
        for backend in ('numpy','native'):
            for changes in ({'source':3},{'frame_decisions':-1},{'width':1}):
                with self.assertRaises(ValueError):
                    annotations_from_frame(Frame(MappingProxyType(dict(f.header,**changes)),f.records,f.rows,f.payload),
                                           actual_pl_execution=True,marker_backend=backend)
        # Invalid accepted centers outside the selected sample are still rejected.
        frame = modified(make_frame(12000),lambda a:a.__setitem__((11999,13),2 << 24))
        for backend in ('numpy','native'):
            with self.assertRaisesRegex(ValueError,'no center'):
                annotations_from_frame(frame,max_points=1,marker_backend=backend)

    def test_missing_library_auto_fallback_and_layout_fallback(self):
        f = make_frame(31)
        expected = annotations_from_frame(f,marker_backend='numpy',compact=True)
        with patch.dict('os.environ',{'SCAN3D_R34_MARKERS_LIBRARY':'D:/missing-r34-marker-library.dll'}):
            actual = annotations_from_frame(f,marker_backend='auto',compact=True)
            self.assertEqual(pack_annotations(actual),pack_annotations(expected))
            with self.assertRaisesRegex(RuntimeError,'unavailable'):
                annotations_from_frame(f,marker_backend='native')
        view = Frame(f.header,f.records[::2],f.rows,f.payload)
        self.assertEqual(pack_annotations(annotations_from_frame(view,marker_backend='auto',compact=True)),
                         pack_annotations(annotations_from_frame(view,marker_backend='numpy',compact=True)))

    def test_concurrent_calls_have_owned_outputs_and_actual_counter(self):
        frame = make_frame(5000)
        expected = pack_annotations(annotations_from_frame(frame,marker_backend='numpy',compact=True))
        before = backend_status()['counters']['native_calls']
        def run(_):
            return pack_annotations(annotations_from_frame(frame,marker_backend='native',compact=True))
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(item==expected for item in pool.map(run,range(100))))
        self.assertEqual(backend_status()['counters']['native_calls']-before,100)

    def test_gil_policies_are_explicit_and_loader_selection_is_not_cached_wrongly(self):
        from .markers_native_r34 import optional_library
        import ctypes
        for policy, loader in (('release', ctypes.CDLL), ('hold', ctypes.PyDLL), ('release', ctypes.CDLL)):
            with patch.dict('os.environ', {'SCAN3D_R34_MARKERS_GIL_POLICY':policy}):
                self.assertIs(type(optional_library()),loader)
                self.assertEqual(backend_status()['call_GIL_policy'],policy)
                self.compare(make_frame(5000))
        with patch.dict('os.environ', {'SCAN3D_R34_MARKERS_GIL_POLICY':'invalid'}):
            with self.assertRaisesRegex(ValueError,'GIL policy'):
                backend_status()

    def test_hold_gil_maximum_capacity_and_concurrent_outputs(self):
        with patch.dict('os.environ', {'SCAN3D_R34_MARKERS_GIL_POLICY':'hold'}):
            self.compare(make_frame(180480),512)
            with self.assertRaisesRegex(ValueError,'record capacity'):
                sample(make_frame(180481).records,3,512,required=True)
            self.test_concurrent_calls_have_owned_outputs_and_actual_counter()


if __name__ == '__main__':
    unittest.main()
