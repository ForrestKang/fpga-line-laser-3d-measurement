"""Optional checked display-only C sampler; missing library preserves NumPy."""
from functools import lru_cache
from pathlib import Path
import ctypes
import hashlib
import json
import os
import platform
import threading
import numpy as np


SOURCE = Path(__file__).with_name('native') / 'markers_r34.c'
DEFAULT_LIBRARY = SOURCE.with_name('markers_r34.dll' if os.name == 'nt' else 'libmarkers_r34.so')
_COUNTER_LOCK = threading.Lock()
_COUNTERS = dict(native_calls=0, optional_missing_fallbacks=0, optional_layout_fallbacks=0)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def call_gil_policy():
    # Keep release as the frozen default until ARM full-chain A/B proves a win.
    policy = os.environ.get('SCAN3D_R34_MARKERS_GIL_POLICY', 'release')
    if policy not in ('release', 'hold'):
        raise ValueError('R34 marker GIL policy must be release or hold')
    return policy


@lru_cache(maxsize=8)
def _load(name, policy):
    path = Path(name)
    if not path.is_file():
        return None
    receipt = json.loads(Path(str(path) + '.json').read_text(encoding='utf-8'))
    if (receipt.get('schema') != 'R34_NATIVE_MARKER_BUILD_V1'
            or receipt.get('source_sha256') != _sha(SOURCE)
            or receipt.get('output_sha256') != _sha(path)
            or receipt.get('output_bytes') != path.stat().st_size
            or receipt.get('machine') != platform.machine()):
        raise ValueError('R34 marker library build fingerprint/ABI mismatch')
    # Both loaders call the same pure-C, two-pass sampler with per-call arrays.
    # PyDLL keeps the GIL for this bounded call; it does not permit Python C API
    # calls or change ownership/checking of the immutable input and output.
    library = (ctypes.PyDLL if policy == 'hold' else ctypes.CDLL)(str(path.resolve()))
    fn = library.r34_markers
    fn.argtypes = (ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint32,
                   ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                   ctypes.POINTER(ctypes.c_uint32))
    fn.restype = ctypes.c_int
    library.r34_build_receipt = receipt
    return library


def optional_library():
    return _load(os.environ.get('SCAN3D_R34_MARKERS_LIBRARY', str(DEFAULT_LIBRARY)), call_gil_policy())


def backend_status():
    """Current-process actual invocation counters, not a file-presence claim."""
    library = optional_library()
    with _COUNTER_LOCK:
        counters = dict(_COUNTERS)
    return dict(schema='R34_NATIVE_MARKER_RUNTIME_V1', available=library is not None,
                library=os.environ.get('SCAN3D_R34_MARKERS_LIBRARY', str(DEFAULT_LIBRARY)),
                source_sha256=_sha(SOURCE), machine=platform.machine(),
                output_sha256=library.r34_build_receipt['output_sha256'] if library else None,
                counters=counters, call_GIL_policy=call_gil_policy(), display_only=True)


def _count(name):
    with _COUNTER_LOCK:
        _COUNTERS[name] += 1


def sample(records, decisions, limit, *, required=False):
    """Return three numeric groups, or None if optional backend is unavailable.

    Caller retains its existing Frame/header/immutable-storage validation. No
    input copy, global scratch workspace, identity cache or mutable output reuse.
    """
    if (not isinstance(records, np.ndarray) or records.dtype != np.dtype('<u4')
            or records.ndim != 2 or records.shape[1] != 16 or records.flags.writeable):
        raise ValueError('immutable R33 observation storage required')
    if type(decisions) is not int or not 0 <= decisions <= 0xffffffff:
        raise ValueError('R33 frame decisions required')
    if type(limit) is not int or not 1 <= limit <= 512:
        raise ValueError('R33 display marker limit must be 1..512')
    if len(records) > 180480:
        raise ValueError('R34 display marker record capacity exceeded')
    if not records.flags.c_contiguous or not records.flags.aligned:
        if required:
            raise ValueError('native markers require contiguous aligned records')
        _count('optional_layout_fallbacks')
        return None
    library = optional_library()
    if library is None:
        if required:
            raise RuntimeError('R34 native marker library is unavailable')
        _count('optional_missing_fallbacks')
        return None
    output = np.empty((limit, 3), np.int64)
    counts = (ctypes.c_uint32 * 3)()
    status = library.r34_markers(int(records.ctypes.data), len(records), decisions,
                                limit, int(output.ctypes.data), limit, counts)
    _count('native_calls')
    if status == 1:
        raise ValueError('accepted R33 marker has no center')
    if status == 2:
        raise ValueError('R33 marker outside display domain')
    if status:
        raise ValueError('R34 native marker argument/output contract failure')
    result = []
    offset = 0
    for size in counts:
        result.append(output[offset:offset + size])
        offset += size
    return result
