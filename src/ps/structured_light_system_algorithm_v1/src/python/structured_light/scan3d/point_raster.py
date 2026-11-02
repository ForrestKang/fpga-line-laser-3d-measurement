"""Explicit manifest-bound native pixel raster; import never builds or loads C."""
from collections import Counter
import ctypes
import hashlib
import importlib
import json
from pathlib import Path
import platform
import sys
import threading

import numpy as np

ABI = 1
MAX_POINTS = 250_000
SOURCE = Path(__file__).with_name('native')/'point_raster.c'
SCHEMA = 'r13.point_raster_native.v1'


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runtime_identity():
    try:
        module = importlib.import_module('numpy._core._multiarray_umath')
    except ImportError:
        module = importlib.import_module('numpy.core._multiarray_umath')
    binary = Path(module.__file__).resolve()
    if not (binary.name.endswith('.pyd') or '.so' in binary.name):
        raise RuntimeError('actual NumPy multiarray binary required')
    return dict(system=platform.system(),machine=platform.machine(),
        pointer_bits=ctypes.sizeof(ctypes.c_void_p)*8,byteorder=sys.byteorder,
        python_implementation=platform.python_implementation(),
        python_version=platform.python_version(),numpy_version=np.__version__,
        multiarray_sha256=file_sha256(binary))


class _RasterKernel:
    """Internal explicit-build QA bridge; production uses NativePointRaster."""
    def __init__(self, library, *, max_points=MAX_POINTS):
        if type(max_points) is not int or not 1 <= max_points <= MAX_POINTS:
            raise ValueError('raster capacity must be 1..250000')
        self.library = Path(library).resolve()
        self._lib = ctypes.CDLL(str(self.library))
        self._lib.sl_point_raster_abi.argtypes=[]
        self._lib.sl_point_raster_abi.restype=ctypes.c_uint32
        if self._lib.sl_point_raster_abi() != ABI:
            raise ValueError('native raster ABI mismatch')
        pointer=ctypes.c_void_p
        self._call=self._lib.sl_point_raster_v1
        self._call.argtypes=[pointer,pointer,pointer,ctypes.c_size_t,
            ctypes.c_double,ctypes.c_double,ctypes.c_double,
            ctypes.c_int32,ctypes.c_int32,pointer,ctypes.c_size_t,
            pointer,pointer,pointer,ctypes.c_size_t]
        self._call.restype=ctypes.c_int
        self.max_points=max_points
        self._capacity=0
        self._scratch=None
        self._lock=threading.Lock()
        self._closed=False
        self._counts=Counter()

    def _decline(self, reason):
        self._counts['fallback']+=1
        self._counts['fallback:'+reason]+=1
        return dict(used=False,reason=reason)

    def try_raster(self, points, projected_x, projected_y, panel, scale, z_min, z_span):
        # All checks and strong refs remain alive across CDLL's GIL-free call.
        with self._lock:
            self._counts['attempts']+=1
            if self._closed:
                return self._decline('closed')
            arrays=(points,projected_x,projected_y,panel)
            if any(not isinstance(a,np.ndarray) for a in arrays):
                return self._decline('layout')
            count=len(points) if points.ndim else 0
            if (points.shape != (count,3) or projected_x.shape != (count,)
                    or projected_y.shape != (count,) or panel.ndim != 3
                    or panel.shape[2] != 3 or points.dtype != np.dtype('float64')
                    or projected_x.dtype != np.dtype('float64')
                    or projected_y.dtype != np.dtype('float64') or panel.dtype != np.uint8
                    or any(not a.flags.c_contiguous or not a.flags.aligned for a in arrays[:3])
                    or not panel.flags.aligned or panel.strides[1:] != (3,1)
                    or panel.strides[0] < panel.shape[1]*3
                    or not panel.flags.writeable):
                return self._decline('layout')
            if count > self.max_points:
                return self._decline('capacity')
            height,width=panel.shape[:2]
            if not 7 <= min(height,width) or max(height,width)>np.iinfo(np.int32).max:
                return self._decline('dimensions')
            if any(np.shares_memory(panel,a) for a in arrays[:3]):
                return self._decline('alias')
            if not count:
                return self._decline('empty')
            if count > self._capacity:
                self._capacity=min(self.max_points,max(count,2*self._capacity,256))
                self._scratch=(np.empty(self._capacity,np.int32),
                    np.empty(self._capacity,np.int32),np.empty((self._capacity,3),np.uint8))
            sx,sy,colors=self._scratch
            status=self._call(points.ctypes.data,projected_x.ctypes.data,projected_y.ctypes.data,
                count,scale,z_min,z_span,width,height,panel.ctypes.data,panel.strides[0],
                sx.ctypes.data,sy.ctypes.data,colors.ctypes.data,self._capacity)
            if status:
                return self._decline({1:'rounding_mode',2:'nonfinite_or_int32_range',
                    3:'abi_arguments'}.get(status,'native_status_'+str(status)))
            self._counts['used']+=1
            return dict(used=True,reason=None)

    def summary(self):
        with self._lock:
            return dict(counts=dict(self._counts),scratch_capacity=self._capacity,
                scratch_max_points=self.max_points,closed=self._closed)

    def close(self):
        with self._lock:
            self._closed=True
            self._scratch=None
            self._capacity=0


class NativePointRaster(_RasterKernel):
    def __init__(self, manifest, *, max_points=MAX_POINTS):
        path=Path(manifest).resolve()
        record=json.loads(path.read_text(encoding='utf-8'))
        if record.get('schema') != SCHEMA or record.get('abi') != ABI:
            raise ValueError('native raster manifest schema/ABI mismatch')
        runtime=runtime_identity()
        if record.get('runtime') != runtime:
            raise ValueError('native raster runtime mismatch')
        library=(path.parent/record['library']).resolve()
        if library.parent != path.parent or not library.is_file():
            raise ValueError('native raster library must be beside manifest')
        digest=file_sha256(library)
        if record.get('library_sha256') != digest:
            raise ValueError('native raster library SHA mismatch')
        if record.get('source_sha256') != file_sha256(SOURCE):
            raise ValueError('native raster source SHA mismatch')
        qa=record.get('validation',{})
        if (qa.get('passed') is not True or qa.get('runtime') != runtime
                or qa.get('library_sha256') != digest
                or qa.get('full_panel_pixels_equal') is not True
                or qa.get('overlap_order_equal') is not True):
            raise ValueError('native raster lacks matching-runtime pixel validation')
        self.identity=dict(manifest_sha256=file_sha256(path),library_sha256=digest,
            abi=ABI,runtime=runtime,validation=qa)
        super().__init__(library,max_points=max_points)

    def summary(self):
        return dict(super().summary(),identity=self.identity)
