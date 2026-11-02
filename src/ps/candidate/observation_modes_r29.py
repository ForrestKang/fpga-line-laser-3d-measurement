"""Opt-in full-diagnostic wire conversion with source/library/runtime binding.

No compilation occurs on import. An explicit fused request requires a matching
QA manifest; it never silently selects a different backend. The original R28
converter remains unchanged and is selected by baseline.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import sys

import numpy as np

SOURCE = Path(__file__).with_name('native') / 'observation_modes_r29.c'
MANIFEST = SOURCE.with_name('observation_modes_r29_manifest.json')
_BACKEND = os.environ.get('SCAN3D_R29_OBSERVATIONS', 'baseline')
if _BACKEND not in ('baseline', 'fused'):
    raise ValueError('unknown R29 observation backend')
_LIB = None
_IDENTITY = None


def runtime_identity():
    return [platform.system().lower(), platform.machine().lower(), sys.byteorder,
            platform.python_version(), np.__version__]


def load(manifest=MANIFEST):
    global _LIB, _IDENTITY
    if Path(manifest).is_symlink():
        raise ValueError('symlink R29 observation manifest')
    path = Path(manifest).resolve()
    doc = json.loads(path.read_text(encoding='utf-8'))
    name = doc.get('library')
    if not isinstance(name, str) or Path(name).name != name or '/' in name or '\\' in name:
        raise ValueError('unsafe R29 observation library path')
    library = path.parent / name
    if (doc.get('schema') != 'R29_OBSERVATION_MODES_V1' or doc.get('abi') != 0x290001
            or doc.get('runtime') != runtime_identity() or path.is_symlink() or library.is_symlink()
            or hashlib.sha256(SOURCE.read_bytes()).hexdigest() != doc.get('source_sha256')
            or hashlib.sha256(library.read_bytes()).hexdigest() != doc.get('library_sha256')
            or doc.get('differential_qa_passed') is not True):
        raise ValueError('R29 observation helper identity/QA mismatch')
    lib = ctypes.CDLL(str(library))
    lib.observation_modes_r29_abi.restype = ctypes.c_uint32
    if lib.observation_modes_r29_abi() != 0x290001:
        raise ValueError('R29 observation ABI mismatch')
    lib.observation_modes_r29.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.observation_modes_r29.restype = ctypes.c_int
    _LIB = lib
    _IDENTITY = doc
    return True


def identity():
    return None if _IDENTITY is None else dict(_IDENTITY)


def convert(records, qualified):
    if _BACKEND == 'baseline':
        return None
    if _LIB is None:
        raise ValueError('R29 fused observations require a bound QA library')
    from .pfo3_r25 import RECORD_DTYPE
    if (type(records) is not np.ndarray or records.dtype != RECORD_DTYPE or records.ndim != 1
            or not records.flags.c_contiguous or len(records) > 180480
            or type(qualified) is not int or not 0 <= qualified <= min(7680, len(records))):
        raise ValueError('exact diagnostic record layout and qualified bounds required')
    integer = np.empty((9, qualified), np.int64)
    floating = np.empty((2, qualified), np.float64)
    modes = np.empty((4, qualified), np.uint8)
    counts = np.empty((2, 8), np.uint64)
    accepted = np.empty(2, np.uint32)
    status = _LIB.observation_modes_r29(records.ctypes.data, len(records), qualified,
        integer.ctypes.data, floating.ctypes.data, modes.ctypes.data,
        counts.ctypes.data, accepted.ctypes.data)
    if status:
        raise ValueError('R29 observation conversion bounds/count/code failure: '+str(status))
    return integer, floating, modes, counts, accepted


if _BACKEND == 'fused':
    load(os.environ.get('SCAN3D_R29_OBSERVATIONS_MANIFEST', MANIFEST))
