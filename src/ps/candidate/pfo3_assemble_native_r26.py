"""ctypes binding for the native assemble path (validation + patch reassembly).

The native call performs the same framing, tag, count, ID, patch-set, capacity
and CRC work as the Python reference; the returned payload is still passed
through pfo3_r25.decode() by the caller, so no check is dropped.
"""
from __future__ import annotations
import ctypes
import threading
from pathlib import Path
import numpy as np

_HERE = Path(__file__).resolve().parent
_LIB_PATH = _HERE / 'native' / 'libassemble_r26.so'
_LIB = None
if _LIB_PATH.is_file():
    try:
        _LIB = ctypes.CDLL(str(_LIB_PATH))
        _LIB.assemble_r26.argtypes = [ctypes.c_char_p, ctypes.c_uint32,
                                      ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        _LIB.assemble_r26.restype = ctypes.c_int
    except OSError:
        _LIB = None

ERRORS = {0: None, 1: 'truncated hardware stream', 2: 'not a native hardware stream',
          3: 'stream sequence/length mismatch', 4: 'native hardware error',
          5: 'truncated hardware record', 6: 'unknown native record tag',
          7: 'reserved native record kind', 8: 'native output count mismatch',
          9: 'native diagnostic ID mismatch', 10: 'invalid/repeated hardware patch ID',
          11: 'missing/foreign qualified candidate patch', 12: 'missing/foreign qualified candidate patch',
          13: 'native assemble capacity exceeded'}
# Contract bound: 256 + 180480*64 (diagnostics) + 480*64 (rows) = 11,581,696 B.
# 16 MiB leaves headroom; a smaller buffer would reject legal maximum frames.
CAPACITY_BYTES = 16 * 1024 * 1024
_OUTPUT = ctypes.create_string_buffer(CAPACITY_BYTES)
# process-wide C workspace: one call at a time; the returned payload is a fresh
# immutable bytes object, so later calls cannot mutate it.
_CALL_LOCK = threading.Lock()


def available():
    return _LIB is not None


def library_path():
    return str(_LIB_PATH)


def assemble_native(primary_stream):
    """Return the assembled payload bytes; raise ValueError with the reference text."""
    if _LIB is None:
        raise RuntimeError('native assemble library not built')
    data = bytes(primary_stream)
    if len(data) == 0 or len(data) > 16 * 1024 * 1024:
        raise ValueError('native assemble input out of bounds')
    with _CALL_LOCK:
        length = ctypes.c_uint32(0)
        code = _LIB.assemble_r26(data, len(data), _OUTPUT, CAPACITY_BYTES, ctypes.byref(length))
        raw = ctypes.string_at(_OUTPUT, length.value) if code == 0 else b''
    if code != 0:
        from .pfo3_r25 import PFO3Error
        raise PFO3Error(ERRORS.get(code, 'native assemble failed') + (' ' if code == 4 else ''))
    return raw
