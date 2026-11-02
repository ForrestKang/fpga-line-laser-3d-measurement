"""R20 optional stateless native cold admission and fixed segment selection.

The frozen decoders and R19 selector remain the fallback specification. No
runtime compiler, device access, payload/result cache or future-frame knowledge.
An absent/unloadable library keeps the Python reference path usable. Explicit
SCAN3D_CPU_R20_LIBRARY selects the platform build; see native_status().
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import struct

import numpy as np

from ..p2.errors import ProtocolError
from ..stripe_v1.stripe_fast import StripeRows
from .local_segments_r16 import _supported
from .selection_fast_r19 import select_selected_run_candidates_fast as _reference_select
from .stream_features import FeatureConfiguration
from .stream_ridge import RidgePacket, SLC2_MAGIC, RPG1
from .stream_selected_runs_r17 import SelectedRunsPacket, decode_slc4
from .stream_segments_r15 import SEGMENT_REASONS


def _load_native():
    name = 'cpu_fast_r20.dll' if os.name == 'nt' else 'cpu_fast_r20.so'
    path = Path(os.environ.get('SCAN3D_CPU_R20_LIBRARY', str(Path(__file__).with_name('native')/name)))
    try:
        library = ctypes.CDLL(str(path))
        library.r20_validate.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int]
        library.r20_validate.restype = ctypes.c_int
        library.r20_select.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]+[ctypes.c_void_p]*8
        library.r20_select.restype = ctypes.c_int
        return library, str(path), None
    except (OSError, AttributeError) as exc:
        return None, str(path), str(exc)


_NATIVE, _NATIVE_PATH, _NATIVE_ERROR = _load_native()
_REASONS = SEGMENT_REASONS+('candidate_overflow', 'saturated_competitor', 'ambiguous_candidates', 'roi_excluded')
_REASON_TABLE = np.asarray(_REASONS, dtype=object)


def native_status():
    return dict(available=_NATIVE is not None, library_path=_NATIVE_PATH, load_error=_NATIVE_ERROR,
                payload_cache=False, runtime_compilation=False)


def decode_slc4_fast(payload, *, frame_width, frame_height, request_id=None, capture_epoch=None):
    """Same admission/packet interface as decode_slc4; no result cache.

    The C validator fuses all SLC4 and nested SLC2/SLC1 checks. Its group error
    text differs from reference, while rejection parity is regression-tested.
    Re-encoding a small SLC2 view happens only after successful full admission
    to preserve the exact public RidgePacket header/vectors/payload interface.
    """
    if _NATIVE is None:
        return decode_slc4(payload, frame_width=frame_width, frame_height=frame_height,
                           request_id=request_id, capture_epoch=capture_epoch)
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ProtocolError('SLC4 payload must be bytes-like')
    if (type(frame_width) is not int or type(frame_height) is not int
            or any(v is not None and type(v) is not int for v in (request_id, capture_epoch))):
        raise ProtocolError('SLC4 identity and geometry must use exact integers')
    data = bytes(payload)
    if not 1 <= frame_width <= 752 or not 1 <= frame_height <= 480:
        raise ProtocolError('SLC4 frame geometry mismatch')
    result = _NATIVE.r20_validate(data, len(data), frame_width, frame_height)
    if result:
        raise ProtocolError(f'SLC4 native admission check {result} rejected packet')
    h = struct.unpack_from('<32I', data)
    if ((request_id is not None and h[3] != request_id)
            or (capture_epoch is not None and h[4] != capture_epoch)):
        raise ProtocolError('SLC4 capture identity/epoch mismatch')
    x0, x1 = h[14] & 1023, h[14] >> 10
    y0, y1 = h[15] & 1023, h[15] >> 10
    cfg = FeatureConfiguration(frame_width, frame_height, h[13], (x0, y0, x1, y1),
                               h[16] & 1023, h[16] >> 10, h[17], h[18] & 255, 255)
    rows = np.frombuffer(data, '<u4', offset=128).reshape(-1, 6)
    # Small compatibility object only; no nested decode or repeated bitfields.
    normalized = np.empty(32+3*len(rows), '<u4')
    normalized[:32] = h
    normalized[0], normalized[2], normalized[30] = SLC2_MAGIC, len(normalized), RPG1
    normalized[32:] = rows[:, :3].ravel()
    ridge = RidgePacket(normalized.tobytes(), h[3], h[4], cfg)
    return SelectedRunsPacket(data, ridge)


def select_selected_run_candidates_fast(packet, gray, config, selection, *, stats=None):
    """Return exact StripeRows and PL fact mask; unsupported policy uses R19."""
    if (_NATIVE is None or not isinstance(packet, SelectedRunsPacket)
            or not packet.whole_frame_fallback or not _supported(packet, config, selection)
            or config.max_candidates_per_row != 8):
        return _reference_select(packet, gray, config, selection, stats=stats)
    if (not isinstance(gray, np.ndarray) or gray.dtype != np.uint8
            or gray.shape != (packet.configuration.height, packet.configuration.width)):
        raise ValueError('SLC3/raw uint8 geometry mismatch')
    # Native scan walks only the ROI and admitted fallback rows. Copy only if
    # the input has unusual strides; unchanged semantics include these arrays.
    pixels = np.ascontiguousarray(gray)
    height, width = gray.shape
    centers, starts, ends = (np.empty(height, np.int64) for _ in range(3))
    accepted, retained, primary, reasons = (np.empty(height, np.uint8) for _ in range(4))
    counts = np.empty(4, np.int32)
    arrays = (centers, starts, ends, accepted, retained, primary, reasons, counts)
    result = _NATIVE.r20_select(packet.payload, len(packet.payload), pixels.ctypes.data, width, height,
                                *(a.ctypes.data for a in arrays))
    if result:
        raise ValueError('R20 native selector geometry mismatch')
    if stats is not None:
        _, y0, _, y1 = config.roi
        stats.update(path='PS_local_Raw_and_exact_global_segments', pl_final_decision_rows=0,
            pl_fact_rows=int(counts[0]), raw_extracted_rows=int(counts[1]),
            raw_source_rows=(np.flatnonzero(primary[y0:y1] == 0)+y0).tolist(),
            cpu_segment_rows=int(counts[2]), ps_segment_count=int(counts[3]))
    return StripeRows(centers, starts, ends, accepted.view(np.bool_), retained.view(np.bool_),
                      tuple(_REASON_TABLE[reasons].tolist())), primary.view(np.bool_)
