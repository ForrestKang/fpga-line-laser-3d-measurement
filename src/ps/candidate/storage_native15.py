"""Optional, hash-bound native storage comparison; strict Python fallback.

The library replaces only temporary array/reason reconstruction and XYZ gather
comparisons. Header/schema/layout/digest/source identity and completion checks
remain in Python. Disk readers always use the independent Python comparator.
"""
import ctypes
import hashlib
import json
from pathlib import Path
import platform
import sys
import numpy as np

ABI=0x150001
HERE=Path(__file__).with_name('native')
SOURCE=HERE/'storage15_validate.c'
MANIFEST=HERE/'storage15_manifest.json'
_LIB=None
_LOADED=False
_IDENTITY=None
_FIELDS=('rows','center_q12','stripe_accepted','branch_ids','width_px','contrast_dn',
         'edge_drop_dn','peak_count','start','end','source_candidate_id','observation_reasons')


def _manifest():
    data=MANIFEST.read_bytes()
    if len(data)>65536:raise ValueError('oversize storage15 native manifest')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate storage15 native manifest key')
            result[key]=value
        return result
    return json.loads(data,object_pairs_hook=unique)


def _load():
    global _LIB,_LOADED,_IDENTITY
    if _LOADED:return _LIB
    if not MANIFEST.is_file():return None
    doc=_manifest()
    name=doc['library']
    if (doc.get('schema')!='scan3d_storage15_native_v1' or type(name) is not str
            or Path(name).name!=name or doc.get('abi')!=ABI
            or doc.get('runtime')!=[sys.platform,platform.machine(),sys.byteorder]
            or doc.get('source_sha256')!=hashlib.sha256(SOURCE.read_bytes()).hexdigest()):
        raise ValueError('storage15 native manifest/source/runtime mismatch')
    path=HERE/name
    if doc.get('library_sha256')!=hashlib.sha256(path.read_bytes()).hexdigest():
        raise ValueError('storage15 native library SHA mismatch')
    lib=ctypes.CDLL(str(path));lib.storage15_abi.restype=ctypes.c_uint32
    if lib.storage15_abi()!=ABI:raise ValueError('storage15 native ABI mismatch')
    lib.storage15_observations.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_size_t,
        ctypes.c_int,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(ctypes.c_int64)]
    lib.storage15_observations.restype=ctypes.c_int
    lib.storage15_points.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,
        ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int]
    lib.storage15_points.restype=ctypes.c_int
    _LIB=lib;_IDENTITY=doc;_LOADED=True
    return lib


def available():return sys.byteorder=='little' and _load() is not None


def identity():
    """Release preflight evidence for the loaded, source-bound comparator."""
    if not available():raise RuntimeError('storage15 native comparator is not built')
    return dict(_IDENTITY)


def _array(value,dtype,shape):
    return (type(value) is np.ndarray and value.dtype==np.dtype(dtype)
            and value.shape==shape and value.flags.c_contiguous)


def compare_observations(profile,frame,mode):
    """True means all original source comparisons ran; False uses Python."""
    table=profile.get('_storage_reason_table15')
    if table is None or not available():return False
    from .pfo3_r25 import FinalFrame,RECORD_DTYPE,REASON_NAMES,PFO3Error
    if type(frame) is not FinalFrame or mode not in ('measurement','preview'):return False
    rows=profile['rows'];n=len(rows);q=profile['observation_quality']
    arrays=[profile[key] for key in _FIELDS[:4]]+[q.get(key) for key in _FIELDS[4:11]]+[profile['observation_reasons']]
    dtypes=['<i8','<i8','|b1','<i8','<i8','<f8','<f8','<i8','<i8','<i8','<i8','<i4']
    if n>7680 or any(not _array(a,d,(n,)) for a,d in zip(arrays,dtypes)):return False
    records=frame.records
    if (not _array(records,RECORD_DTYPE,(len(records),)) or records.flags.writeable
            or len(records)>180480):return False
    lookup={value:i for i,value in enumerate(table)}
    reasons=(ctypes.c_int64*8)(*(lookup.get(value,-1) for value in REASON_NAMES))
    pointers=(ctypes.c_void_p*12)(*(a.ctypes.data for a in arrays))
    code=_LIB.storage15_observations(records.ctypes.data,len(records),n,int(mode=='preview'),pointers,reasons)
    if code==0:return True
    if code==100:raise PFO3Error('geometry input differs from source observations: rows')
    if 1<=code<=4:raise PFO3Error('geometry input differs from source observations: '+_FIELDS[code-1])
    if 5<=code<=11:raise PFO3Error('geometry observation quality differs from source: '+_FIELDS[code-1])
    if code==12:raise PFO3Error('geometry observation reasons differ from source')
    raise PFO3Error('unknown storage15 observation comparison result')


def compare_points(profile,points,key):
    """Bit-exact selected XYZ check, or False for the generic dtype path."""
    if not available():return False
    source=profile[key];mask=profile['valid_points'];n=len(mask)
    if (n>7680 or not _array(source,'<f8',(n,3)) or not _array(mask,'|b1',(n,))
            or not _array(points,'<f8',(len(points),3))):return False
    code=_LIB.storage15_points(source.ctypes.data,mask.ctypes.data,n,
        points.ctypes.data,len(points),int(key=='points_axis_mm'))
    if code:raise ValueError('XYZ delta differs from MBR1 valid observations')
    return True


def expand_observation_reasons(profile):
    table=profile.get('_storage_reason_table15')
    if table is None:return profile['observation_reasons']
    return tuple(np.asarray(table,dtype=object)[profile['observation_reasons']].tolist())
