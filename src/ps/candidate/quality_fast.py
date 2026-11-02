"""Optional exact C quality reductions for this isolated R21 candidate.

Import never builds/loads hardware. Missing native library returns None, keeping
processor R21B as the explicit Python fallback. No thresholds are substituted.
"""
from collections import Counter
import ctypes
import os
from pathlib import Path
import numpy as np
from structured_light.scan3d.continuous_quality import FrameQuality

_LIBRARY_PATH=Path(__file__).resolve().parent/'native'/('quality_r21.dll' if os.name=='nt' else 'libquality_r21.so')
_LOAD_ERROR=None
try:_LIB=ctypes.CDLL(str(_LIBRARY_PATH))
except OSError as exc:_LIB=None;_LOAD_ERROR=str(exc)
if _LIB is not None:
    _U8=np.ctypeslib.ndpointer(dtype=np.uint8,flags='C_CONTIGUOUS')
    _I64=np.ctypeslib.ndpointer(dtype=np.int64,flags='C_CONTIGUOUS')
    _F64=np.ctypeslib.ndpointer(dtype=np.float64,flags='C_CONTIGUOUS')
    _LIB.quality_reduce.argtypes=[_U8]+[ctypes.c_int]*6+[_I64,_U8,_F64,_U8,ctypes.c_int,_F64,_F64]
    _LIB.quality_reduce.restype=ctypes.c_int
_STATUSES=('good','saturated','ambiguous','weak','incomplete','unvalidated')
_THRESHOLD_NAMES=('min_intensity','max_width_px','min_completeness','max_saturation_fraction',
                  'max_ambiguous_fraction','max_saturated_row_fraction')
_AMBIGUOUS=('candidate_overflow','merged_peak_ambiguous','ambiguous_branch_direction')

def native_available():return _LIB is not None

def native_library_path():return str(_LIBRARY_PATH)

def assess_multibranch_quality(image,observed,rows,accepted,reasons,stripe_config,thresholds,*,use_native=True):
    if not use_native or _LIB is None:return None
    thresholds.__post_init__()
    image=np.asarray(image)
    if image.ndim!=2 or image.dtype!=np.uint8:raise ValueError('native quality requires uint8 gray image')
    h,w=image.shape;x0,y0,x1,y1=stripe_config.roi or (0,0,w,h)
    rows=np.ascontiguousarray(rows,dtype=np.int64)
    accepted=np.ascontiguousarray(accepted,dtype=np.uint8)
    widths=np.ascontiguousarray(observed.quality['width_px'],dtype=np.float64)
    ambiguity=np.ascontiguousarray(np.isin(np.asarray(observed.reasons,object),_AMBIGUOUS),dtype=np.uint8)
    if rows.ndim!=1 or any(a.shape!=rows.shape for a in (accepted,widths,ambiguity)):
        raise ValueError('unaligned native quality observations')
    limits=np.asarray([getattr(thresholds,name) for name in _THRESHOLD_NAMES],dtype=np.float64)
    output=np.empty(8,np.float64)
    status=_LIB.quality_reduce(np.ascontiguousarray(image),h,w,x0,y0,x1,y1,
        rows,accepted,widths,ambiguity,len(rows),limits,output)
    if status<0:raise ValueError('native quality input/allocation error: '+str(status))
    count=int(output[5])
    return FrameQuality(_STATUSES[status],float(output[0]),float(output[1]) if count else None,
        float(output[2]) if count else None,float(output[3]),float(output[4]),count,int(output[6]),
        dict(Counter(r for r in reasons if r is not None)),True,int(output[7]))
