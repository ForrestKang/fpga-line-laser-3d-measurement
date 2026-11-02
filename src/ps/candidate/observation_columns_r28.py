"""Optional fused wire-column conversion, bound to source/library/runtime SHA.

This helper does not validate/admit a frame or compute any image decision. Its
caller still requires the strict decoded, immutable PFO3 frame. No compilation
or hardware access occurs at import. Each call owns its separate output arrays.
"""
import ctypes
import hashlib
import json
import os
import platform
from pathlib import Path
import sys
import numpy as np

SOURCE=Path(__file__).with_name('native')/'observation_columns_r28.c'
MANIFEST=SOURCE.with_name('observation_columns_r28_manifest.json')
_LIB=None
_IDENTITY=None

def runtime_identity():
    return [platform.system().lower(),platform.machine().lower(),sys.byteorder,
            platform.python_version(),np.__version__]

def load(manifest=MANIFEST):
    global _LIB,_IDENTITY
    path=Path(manifest)
    if not path.exists():return False
    doc=json.loads(path.read_text())
    library=(path.parent/doc['library']).resolve()
    if (library.parent!=path.parent.resolve() or doc.get('abi')!=0x280001
            or doc.get('runtime')!=runtime_identity()
            or hashlib.sha256(SOURCE.read_bytes()).hexdigest()!=doc.get('source_sha256')
            or hashlib.sha256(library.read_bytes()).hexdigest()!=doc.get('library_sha256')
            or doc.get('differential_qa_passed') is not True):
        raise ValueError('observation column helper identity/QA mismatch')
    lib=ctypes.CDLL(str(library))
    lib.observation_columns_r28_abi.restype=ctypes.c_uint32
    if lib.observation_columns_r28_abi()!=0x280001:raise ValueError('observation column ABI mismatch')
    lib.observation_columns_r28.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_void_p]
    lib.observation_columns_r28.restype=ctypes.c_int
    _LIB=lib;_IDENTITY=doc
    return True

def identity():return None if _IDENTITY is None else dict(_IDENTITY)

def convert(records):
    if _LIB is None:return None
    from .pfo3_r25 import RECORD_DTYPE
    if (type(records) is not np.ndarray or records.dtype!=RECORD_DTYPE
            or records.ndim!=1 or not records.flags.c_contiguous or len(records)>7680):
        raise ValueError('exact contiguous qualified observation layout required')
    n=len(records)
    integer=np.empty((7,n),np.int64);floating=np.empty((2,n),np.float64)
    status=_LIB.observation_columns_r28(records.ctypes.data,n,integer.ctypes.data,floating.ctypes.data)
    if status:raise ValueError('native observation column bounds failure')
    return integer,floating

# An explicitly disabled backend is retained for same-input single-factor A/B.
if os.environ.get('SCAN3D_R28_OBSERVATIONS','native') not in ('native','numpy'):
    raise ValueError('unknown R28 observation backend')
if os.environ.get('SCAN3D_R28_OBSERVATIONS','native')=='native':load()
