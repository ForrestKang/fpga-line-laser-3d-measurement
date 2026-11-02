"""Exact-policy native double geometry with frozen R19 boundary fallback."""
from __future__ import annotations

import ctypes
from functools import lru_cache
import os
import time
from pathlib import Path
import numpy as np

from sl_geometry.section_fast_r19 import reconstruct_section_fast as _frozen
from sl_geometry.section_fast_r19 import _calibration

_PATH=Path(__file__).resolve().parent/'native'/('geometry_r21.dll' if os.name=='nt' else 'libgeometry_r21.so')
try:
    _LIB=ctypes.CDLL(str(_PATH))
except OSError:
    _LIB=None
if _LIB is not None:
    _F64=np.ctypeslib.ndpointer(dtype=np.float64,flags='C_CONTIGUOUS')
    _U8=np.ctypeslib.ndpointer(dtype=np.uint8,flags='C_CONTIGUOUS')
    _I32=np.ctypeslib.ndpointer(dtype=np.int32,flags='C_CONTIGUOUS')
    _LIB.geometry_r21.argtypes=[_F64,_F64,_U8,ctypes.c_size_t,_F64,ctypes.c_int,_F64,_I32,_U8,_U8]
    _LIB.geometry_r21.restype=None
_REASONS=np.asarray(['invalid_stripe',None,'parallel','behind','out_of_range'],dtype=object)
_LAST={}


def native_available():
    return _LIB is not None


def native_library_path():
    return str(_PATH)


def last_call_info():
    """Diagnostic counts only; never used to admit geometry or measurements."""
    return dict(_LAST)


@lru_cache(maxsize=16)
def _parameters(k,d,p,limits,bounds):
    kval,dist,plane,normal,offset,box=_calibration(k,d,p,limits,bounds)
    supported=(kval[0,1]==0. and kval[1,0]==0. and
               np.array_equal(kval[2],[0.,0.,1.]) and
               (dist is None or np.all(dist==0.)))
    if not supported:return None
    parameters=np.r_[1./kval[0,0],1./kval[1,1],kval[0,2],kval[1,2],normal,offset,
                     [0.,0.] if limits is None else limits,box.ravel()].astype(np.float64)
    if not np.isfinite(parameters).all():return None
    return parameters


@lru_cache(maxsize=16)
def _parameters_bytes16(k,d,p,limits,bounds):
    """The same parameter validation keyed by immutable numeric contents.

    Creating dozens of numpy scalar objects for the tuple cache key used to
    happen on every frame. Small byte keys bind all float64 calibration bits
    without retaining mutable arrays; only a cache miss materializes scalars.
    Unsupported calibration still returns None and takes the frozen backend.
    """
    def values(blob):
        return None if blob is None else tuple(np.frombuffer(blob,np.float64))
    return _parameters(values(k),values(d),values(p),values(limits),values(bounds))


def reconstruct_section_fast(rows,xc_fixed,stripe_valid,K,distortion,plane,range_limits_mm,training_bounds_mm):
    started=time.perf_counter();cpu=time.thread_time()
    result=_reconstruct_section_fast(rows,xc_fixed,stripe_valid,K,distortion,plane,range_limits_mm,training_bounds_mm)
    _LAST['timing']=dict(wall_s=time.perf_counter()-started,thread_cpu_s=time.thread_time()-cpu)
    return result


def _reconstruct_section_fast(rows,xc_fixed,stripe_valid,K,distortion,plane,range_limits_mm,training_bounds_mm):
    rows=np.asarray(rows);centers=np.asarray(xc_fixed);accepted=np.asarray(stripe_valid,bool)
    if rows.ndim!=1 or centers.shape!=rows.shape or accepted.shape!=rows.shape:
        raise ValueError('row arrays must have identical one-dimensional shape')
    if not np.isfinite(rows).all() or not np.isfinite(centers).all():
        raise ValueError('nonfinite pixel coordinates')
    ka=np.asarray(K,float);pa=np.asarray(plane,float);ba=np.asarray(training_bounds_mm,float)
    da=None if distortion is None else np.asarray(distortion,float).reshape(-1)
    la=None if range_limits_mm is None else np.asarray(range_limits_mm,float)
    parameters=None
    if (_LIB is not None and rows.dtype.kind in 'iuf' and centers.dtype.kind in 'iuf'
            and ka.shape==(3,3) and pa.shape==(4,) and ba.shape==(2,3)
            and (la is None or la.shape==(2,))):
        parameters=_parameters_bytes16(ka.tobytes(),None if da is None else da.tobytes(),
            pa.tobytes(),None if la is None else la.tobytes(),ba.tobytes())
    if parameters is None:
        _LAST.update(backend='frozen',count=len(rows),fallback_rows=len(rows))
        return _frozen(rows,centers,accepted,K,distortion,plane,range_limits_mm,training_bounds_mm)
    n=len(rows);points=np.empty((n,3),np.float64);codes=np.empty(n,np.int32)
    outside=np.empty(n,np.uint8);fallback=np.empty(n,np.uint8)
    _LIB.geometry_r21(np.ascontiguousarray(rows,dtype=np.float64),
        np.ascontiguousarray(centers,dtype=np.float64),np.ascontiguousarray(accepted,dtype=np.uint8),
        n,parameters,la is not None,points,codes,outside,fallback)
    result=dict(points_mm=points,valid=codes==1,reasons=_REASONS[codes],
                outside_training_bounds=outside.astype(bool))
    ids=np.flatnonzero(fallback)
    if len(ids):
        reference=_frozen(rows[ids],centers[ids],accepted[ids],K,distortion,plane,range_limits_mm,training_bounds_mm)
        for key in result:result[key][ids]=reference[key]
    _LAST.update(backend='native',count=n,fallback_rows=len(ids))
    return result
