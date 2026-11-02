"""ctypes access to the R33 strict body validator; no silent fallback."""
from pathlib import Path
import ctypes
import hashlib
import os
import struct
import sys
from candidate.pfo3_r25 import PFO3Error

ERRORS = {
    1: 'unknown native record tag', 2: 'native output count/capacity mismatch',
    3: 'diagnostic/row IDs not complete ordered', 4: 'reserved row fields',
    5: 'candidate geometry/order invalid', 6: 'two dimensional center outside contract',
    7: 'qualified count mismatch', 8: 'missing/foreign/repeated qualified candidate patch',
    9: 'patch changes local rejection', 10: 'incomplete/reserved frame flags',
    11: 'invalid PL acceptance', 12: 'reserved candidate decision bits',
    13: 'accepted candidate has rejection reason', 14: 'final acceptance summary mismatch',
    15: 'row diagnostic quality summary mismatch', 16: 'per-row candidate count mismatch',
    17: 'per-row qualified count mismatch', 18: 'per-row acceptance count mismatch',
    19: 'overflow/completion flags inconsistent', 20: 'frame quality decision inconsistent',
    21: 'frame quality flags inconsistent', 90: 'truncated hardware record',
    91: 'native R33 output buffer capacity mismatch', 92: 'native R33 allocation failed',
}
_CACHE = None


def library_path():
    default = Path(__file__).with_name('native') / ('assemble_r33_v3.dll' if os.name == 'nt' else 'libassemble_r33.so')
    return Path(os.environ.get('R33_NATIVE_ASSEMBLER', default))


def load_native():
    global _CACHE
    path = library_path().resolve()
    if not path.is_file():
        raise PFO3Error('R33 native assembler required; build native library or explicitly select reference backend')
    key = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
    if _CACHE is None or _CACHE[0] != key:
        if sys.byteorder != 'little':
            raise PFO3Error('R33 native assembler requires little endian host')
        lib = ctypes.CDLL(str(path))
        lib.r33_body_abi.argtypes = []
        lib.r33_body_abi.restype = ctypes.c_uint32
        if lib.r33_body_abi() != 0x330001:
            raise PFO3Error('R33 native assembler ABI mismatch')
        lib.r33_body.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                 ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint32)]
        lib.r33_body.restype = ctypes.c_int
        _CACHE = (key, lib, hashlib.sha256(path.read_bytes()).hexdigest())
    return _CACHE[1], _CACHE[2]


def assemble_body(data):
    """Caller has already checked the fixed header and record divisibility."""
    lib, sha = load_native()
    n = struct.unpack_from('<I', data, len(data)-128+8)[0]
    # Header count is not trusted for acceptance; C validates before writing.
    n = n if n <= 180480 else 0
    output = (ctypes.c_uint32 * ((n+480)*16))()
    stats = (ctypes.c_uint32 * 3)()
    rc = lib.r33_body(ctypes.c_char_p(data), len(data), output, ctypes.sizeof(output), stats)
    if rc:
        raise PFO3Error(ERRORS.get(rc, 'unknown native R33 error '+str(rc)))
    size = (int(stats[0])+480)*64
    return ctypes.string_at(output, size), tuple(map(int, stats)), sha
