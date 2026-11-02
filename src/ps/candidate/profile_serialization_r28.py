"""Opt-in MBR1 layout serialization reuse; all frame validation remains strict.

Only numeric layouts and reason vocabularies are cached. No frame, array,
metadata, measurement bytes, hashes or validation receipts enter the caches.
The default entry point delegates to the frozen encoder unless explicitly
enabled with SCAN3D_R28_SERIALIZATION=1.
"""
from dataclasses import asdict, fields, is_dataclass
from functools import lru_cache
import hashlib
import os
import struct
from types import MappingProxyType

import numpy as np

from . import storage_r21 as reference
from .storage_encode_fast_r26 import build_encoded_views_once

_EXCLUDED = frozenset(reference._ARRAYS) | {'observation_quality', 'observation_reasons', 'reasons'}


@lru_cache(maxsize=128)
def _layout_document(layout):
    descriptors = {}
    rows = []
    offset = 0
    for name, dtype, shape, nbytes in layout:
        descriptors[name] = dict(dtype=dtype, shape=list(shape), offset=offset, bytes=nbytes)
        rows.append((name, dtype, shape, offset, nbytes))
        offset += nbytes
    # Cache values are deeply immutable. Every validation call receives its
    # own ordinary dict/list document, including the shape lists.
    return tuple(rows), reference._json_line(descriptors)[:-1], offset


@lru_cache(maxsize=64)
def _reason_document(table):
    return reference._json_line(table)[:-1]


def encode_profile(profile, summary=None):
    if os.environ.get('SCAN3D_R28_SERIALIZATION') != '1':
        return reference.encode_profile(profile, summary)
    return encode_profile_layout(profile, summary)


def encode_profile_layout(profile, summary=None):
    """Assemble identical canonical bytes, then run the same strict validation."""
    arrays = {key: np.asarray(getattr(profile, key)) for key in reference._ARRAYS}
    arrays.update({'observation_quality/' + key: np.asarray(value)
                   for key, value in profile.observation_quality.items()})
    table = [None]
    lookup = {None: 0}
    obs_reasons = getattr(profile, 'observation_reasons')
    fin_reasons = getattr(profile, 'reasons')
    for key, reasons in (('observation_reasons', obs_reasons), ('reasons', fin_reasons)):
        n_r = len(reasons)
        if key == 'reasons' and reasons == obs_reasons:
            arrays['reasons_ids'] = arrays['observation_reasons_ids']
            continue
        if reasons == (None,) * n_r:
            arrays[key + '_ids'] = np.zeros(n_r, dtype='<i4')
            continue
        for reason in dict.fromkeys(reasons):
            if reason is not None and not isinstance(reason, str):
                raise ValueError('invalid reason')
            if reason not in lookup:
                lookup[reason] = len(table)
                table.append(reason)
        arrays[key + '_ids'] = np.fromiter(map(lookup.__getitem__, reasons), dtype='<i4', count=n_r)
    names = [f.name for f in fields(profile)] if is_dataclass(profile) else list(vars(profile))
    metadata = {key: getattr(profile, key) for key in names if key not in _EXCLUDED}
    summary = profile.summary() if summary is None else summary
    metadata = {key: ({f.name: getattr(value, f.name) for f in fields(value)} if key == 'quality'
                     else asdict(value)) if is_dataclass(value) else value
                for key, value in metadata.items()}
    metadata['summary'] = summary
    contiguous = {}
    layout = []
    for key, value in arrays.items():
        if value.dtype.hasobject or value.dtype.kind not in 'biuf':
            raise ValueError('unsupported MBR1 dtype')
        data = np.ascontiguousarray(value)
        contiguous[key] = data
        layout.append((key, data.dtype.str, tuple(data.shape), data.size * data.dtype.itemsize))
    rows, arrays_json, size = _layout_document(tuple(layout))
    descriptors = {name: dict(dtype=dtype, shape=list(shape), offset=offset, bytes=nbytes)
                   for name, dtype, shape, offset, nbytes in rows}
    header_doc = dict(schema='MBR1', contract='PS_IMAGE_MULTIBRANCH_Q12_V1',
                      PL_wire=False, arrays=descriptors, reason_table=table, metadata=metadata)
    # Match sort_keys=True's exact top-level order and scalar spelling. Each
    # variable subtree still goes through the original canonical JSON encoder.
    header = (b'{"PL_wire":false,"arrays":' + arrays_json +
              b',"contract":"PS_IMAGE_MULTIBRANCH_Q12_V1","metadata":' +
              reference._json_line(metadata)[:-1] + b',"reason_table":' +
              (_reason_document(tuple(table)) if len(table) <= 64 and
               all(reason is None or type(reason) is str and len(reason) <= 256 for reason in table)
               else reference._json_line(table)[:-1]) + b',"schema":"MBR1"}\n')
    header_end = 8 + len(header)
    buffer = bytearray(header_end + size)
    view = memoryview(buffer)
    view[:4] = b'MBR1'
    struct.pack_into('<I', buffer, 4, len(header))
    view[8:header_end] = header
    at = header_end
    for data in contiguous.values():
        data_view = memoryview(data).cast('B') if data.size else memoryview(b'')
        view[at:at + len(data_view)] = data_view
        at += len(data_view)
    digest = hashlib.sha256(view)
    payload = reference._new_mbr1(buffer)
    if len(payload) != len(buffer):
        raise ValueError('MBR1 assembly length changed')
    views = build_encoded_views_once(payload, header_doc, len(header))
    return reference._attach_mbr1(payload, sha256=digest.hexdigest(), header_length=len(header),
                                  header_bytes=header, arrays=MappingProxyType(views))
