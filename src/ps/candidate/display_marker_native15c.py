"""Explicit manifest-bound display marker kernel; frozen NumPy is fallback.

No build or hardware action occurs here. A present invalid manifest is an error,
never a silent backend change. The production entry verifies required manifests.
"""
import ctypes
import json
from pathlib import Path
import threading
import numpy as np
from structured_light.scan3d.point_raster import file_sha256, runtime_identity

SOURCE=Path(__file__).with_name('native')/'marker_r15c.c'
MANIFEST=SOURCE.with_name('marker_r15c_manifest.json')
_UNSET=object()
_default=_UNSET
_load_lock=threading.Lock()


class NativeMarkers15C:
    def __init__(self,manifest):
        path=Path(manifest).resolve()
        record=json.loads(path.read_text(encoding='utf8'))
        identity=runtime_identity()
        if record.get('schema')!='DISPLAY_MARKER_15C_V1' or record.get('runtime')!=identity:
            raise ValueError('marker manifest/runtime mismatch')
        library=(path.parent/record['library']).resolve()
        if library.parent!=path.parent or not library.is_file():
            raise ValueError('marker library must be beside manifest')
        digest=file_sha256(library)
        if record.get('library_sha256')!=digest or record.get('source_sha256')!=file_sha256(SOURCE):
            raise ValueError('marker source/library SHA mismatch')
        qa=record.get('validation',{})
        if (qa.get('passed') is not True or qa.get('full_canvas_pixels_equal') is not True
                or qa.get('runtime')!=identity or qa.get('library_sha256')!=digest):
            raise ValueError('marker lacks matching-runtime pixel validation')
        self.identity=record
        self._lib=ctypes.CDLL(str(library))
        abi=self._lib.sl_marker_r15c_abi
        abi.argtypes=[];abi.restype=ctypes.c_uint32
        if abi()!=1:raise ValueError('marker ABI mismatch')
        self._paint=self._lib.sl_marker_r15c
        p=ctypes.c_void_p;n=ctypes.c_size_t
        self._paint.argtypes=[p,n,p,n,p,n,p,n,ctypes.c_int32,ctypes.c_int32,p]
        self._paint.restype=ctypes.c_int

    def draw(self,canvas,groups,origin,colors):
        if (not isinstance(canvas,np.ndarray) or canvas.dtype!=np.uint8
                or canvas.shape!=(720,1280,3) or not canvas.flags.c_contiguous
                or not canvas.flags.writeable):
            raise ValueError('native marker contiguous writable canvas required')
        if len(groups)!=3:raise ValueError('three marker groups required')
        for group in groups:
            if (not isinstance(group,np.ndarray) or group.dtype!=np.int64
                    or group.ndim!=2 or group.shape[1]!=3 or not group.flags.c_contiguous
                    or not group.flags.aligned or np.shares_memory(canvas,group)):
                raise ValueError('native marker aligned int64 nonalias group required')
        bgr=np.asarray(colors,dtype=np.uint8)
        if bgr.shape!=(3,3):raise ValueError('three BGR colors required')
        bgr=np.ascontiguousarray(bgr)
        args=[]
        for group in groups:args.extend((group.ctypes.data,len(group)))
        status=self._paint(canvas.ctypes.data,canvas.nbytes,*args,*origin,bgr.ctypes.data)
        if status:raise ValueError('native marker arguments/coordinates invalid: '+str(status))
        return 'NATIVE_EXACT_LINE8_COMPACT_15C'


def optional_markers():
    global _default
    if _default is _UNSET:
        with _load_lock:
            if _default is _UNSET:
                _default=NativeMarkers15C(MANIFEST) if MANIFEST.is_file() else None
    return _default
