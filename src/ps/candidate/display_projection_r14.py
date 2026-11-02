"""Explicit source/runtime/QA-bound fused display projection and raster.

No build, FPGA access, or load occurs at import. Missing manifests preserve the
existing NumPy projection + native raster path. Measurement arrays are inputs.
"""
import ctypes
import json
from pathlib import Path
import numpy as np
from structured_light.scan3d.point_raster import (_RasterKernel,file_sha256,
                                                runtime_identity,SOURCE as RASTER_SOURCE)

SOURCE=Path(__file__).with_name('native')/'point_project_r14.c'
MANIFEST_NAME='point_project_r14_manifest.json'


class _ProjectionKernel(_RasterKernel):
    def __init__(self,library,*,max_points=250_000):
        super().__init__(library,max_points=max_points)
        abi=self._lib.sl_point_project_r14_abi
        abi.argtypes=[];abi.restype=ctypes.c_uint32
        if abi()!=1:raise ValueError('display projection ABI mismatch')
        self._project=self._lib.sl_point_project_r14
        p=ctypes.c_void_p
        self._project.argtypes=[p,ctypes.c_size_t,p,p,ctypes.c_int32,ctypes.c_int32,
            p,ctypes.c_size_t,p,p,p,p,p,ctypes.c_size_t]
        self._project.restype=ctypes.c_int
        self._project_scratch=None

    def try_project_raster(self,points,right,up,panel):
        with self._lock:
            self._counts['projection_attempts']+=1
            if self._closed:return self._decline('closed')
            arrays=(points,right,up,panel)
            if any(not isinstance(a,np.ndarray) for a in arrays):return self._decline('layout')
            count=len(points) if points.ndim else 0
            if (points.shape!=(count,3) or right.shape!=(3,) or up.shape!=(3,)
                    or panel.ndim!=3 or panel.shape[2]!=3
                    or any(a.dtype!=np.float64 for a in arrays[:3]) or panel.dtype!=np.uint8
                    or any(not a.flags.c_contiguous or not a.flags.aligned for a in arrays[:3])
                    or not panel.flags.aligned or panel.strides[1:]!=(3,1)
                    or panel.strides[0]<panel.shape[1]*3
                    or not panel.flags.writeable):return self._decline('layout')
            if not count:return self._decline('empty')
            if count>self.max_points:return self._decline('capacity')
            height,width=panel.shape[:2]
            if min(height,width)<7 or max(height,width)>np.iinfo(np.int32).max:
                return self._decline('dimensions')
            if any(np.shares_memory(panel,a) for a in arrays[:3]):return self._decline('alias')
            if count>self._capacity:
                self._capacity=min(self.max_points,max(count,2*self._capacity,256))
                self._scratch=(np.empty(self._capacity,np.int32),np.empty(self._capacity,np.int32),
                               np.empty((self._capacity,3),np.uint8))
                self._project_scratch=(np.empty(self._capacity,np.float64),np.empty(self._capacity,np.float64))
            sx,sy,colors=self._scratch
            px,py=self._project_scratch
            status=self._project(points.ctypes.data,count,right.ctypes.data,up.ctypes.data,
                width,height,panel.ctypes.data,panel.strides[0],px.ctypes.data,py.ctypes.data,
                sx.ctypes.data,sy.ctypes.data,colors.ctypes.data,self._capacity)
            if status:return self._decline({1:'rounding_mode',2:'nonfinite_or_int32_range',
                3:'abi_arguments',4:'projection_rounding_boundary'}.get(status,'native_status_'+str(status)))
            self._counts['projection_used']+=1
            return dict(used=True,reason=None)

    def close(self):
        super().close()
        self._project_scratch=None


class NativeProjectedRaster(_ProjectionKernel):
    def __init__(self,manifest):
        path=Path(manifest).resolve()
        record=json.loads(path.read_text(encoding='utf-8'))
        identity=runtime_identity()
        if record.get('schema')!='DISPLAY_PROJECTION_R14_V1' or record.get('runtime')!=identity:
            raise ValueError('display projection manifest/runtime mismatch')
        library=(path.parent/record['library']).resolve()
        if library.parent!=path.parent or not library.is_file():
            raise ValueError('projection library must be beside manifest')
        digest=file_sha256(library)
        if (record.get('library_sha256')!=digest or record.get('source_sha256')!=file_sha256(SOURCE)
                or record.get('raster_source_sha256')!=file_sha256(RASTER_SOURCE)):
            raise ValueError('projection source/library SHA mismatch')
        qa=record.get('validation',{})
        if (qa.get('passed') is not True or qa.get('full_panel_pixels_equal') is not True
                or qa.get('runtime')!=identity or qa.get('library_sha256')!=digest):
            raise ValueError('projection lacks matching-runtime pixel validation')
        self.identity=record
        super().__init__(library)


def optional_projection(manifest=None):
    """Explicit path if supplied; otherwise discover only this version's helper.

    The general native builder creates candidate/native/libpoint_project_r14.so.
    Validation must create its manifest before render construction can use it.
    An invalid existing manifest is an error, never a silent backend change.
    """
    path=Path(manifest) if manifest else SOURCE.with_name(MANIFEST_NAME)
    if manifest and not path.is_file():
        raise FileNotFoundError('explicit display projection manifest is missing: '+str(path))
    return NativeProjectedRaster(path) if path.is_file() else None
