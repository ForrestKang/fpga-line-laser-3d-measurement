"""Independent closed-stream readback and bounded preview failure cleanup.

No device operations occur here. A writer callback is not a readback receipt.
Partial preview preservation never confirms measurement durability.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np

from .storage_r21 import MAX_POINTS, _json, iter_records


def close_preview_evidence(stream, rows, output, *, partial=False, budget=None):
    """Attempt every close stage, retain errors, and write an exclusive index.

    The partial index describes only writes already acknowledged by the Python
    stream. It may name fewer bytes than a failed partial binary write left.
    """
    receipt = dict(enabled=stream is not None, partial=bool(partial),
                   binary_synced=False, binary_closed=stream is None,
                   index_synced=False, indexed_frames=len(rows),
                   indexed_bytes=sum(row['bytes'] for row in rows), errors=[],
                   measurement_durability_confirmed=False)
    if stream is None:
        return receipt
    if not stream.closed:
        try:
            stream.flush()
            os.fsync(stream.fileno())
            receipt['binary_synced'] = True
        except BaseException as exc:
            receipt['errors'].append('binary_sync: '+type(exc).__name__+': '+str(exc))
        finally:
            try:
                stream.close()
            except BaseException as exc:
                receipt['errors'].append('binary_close: '+type(exc).__name__+': '+str(exc))
    receipt['binary_closed'] = bool(stream.closed)
    name = 'preview_index.partial.jsonl' if partial else 'preview_index.jsonl'
    receipt['index_file'] = name
    token = None
    try:
        # The run is bounded; encode one row at a time without duplicating the
        # complete preview stream or accumulating a second index in memory.
        estimate = sum(len((json.dumps(row, sort_keys=True, allow_nan=False)+'\n').encode('utf8'))
                       for row in rows)
        if budget is not None:
            token = budget.reserve(max(1, estimate))
        with (Path(output)/name).open('xb') as index:
            for row in rows:
                index.write((json.dumps(row, sort_keys=True, allow_nan=False)+'\n').encode('utf8'))
            index.flush()
            os.fsync(index.fileno())
        receipt['index_synced'] = True
        if token is not None:
            budget.commit(token, estimate)
    except BaseException as exc:
        receipt['errors'].append('index_preserve: '+type(exc).__name__+': '+str(exc))
        # A failed write may have left bytes: never release its reservation as
        # if nothing was written. The run is terminating with evidence retained.
    receipt['complete'] = (receipt['binary_synced'] and receipt['binary_closed']
                           and receipt['index_synced'] and not receipt['errors'])
    return receipt


def _open_cloud(path, maximum_points):
    """Check a small numeric NPY header and exact EOF before mapping its data."""
    with Path(path).open('rb') as stream:
        version = np.lib.format.read_magic(stream)
        if version != (1, 0):
            raise ValueError('unsupported cloud NPY version')
        shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream)
        if (fortran or dtype != np.dtype('<f8') or len(shape) != 2
                or shape[1] != 3 or not 0 <= shape[0] <= maximum_points):
            raise ValueError('invalid cloud shape/type/capacity')
        offset = stream.tell()
        if Path(path).stat().st_size != offset+shape[0]*24:
            raise ValueError('cloud NPY length/trailing bytes mismatch')
    if not shape[0]:
        return np.empty((0, 3), dtype='<f8')
    return np.memmap(path, dtype='<f8', mode='r', offset=offset, shape=shape)


def verify_replay_saved(output, *, expected_frames, expect_preview,
                        expected_cloud_points=None, expected_preview_points=None):
    """Exhaust strict record reader, compare cloud deltas, and audit preview EOF."""
    if type(expected_frames) is not int or not 0 <= expected_frames <= 32768:
        raise ValueError('bounded expected frame count required')
    root = Path(output)
    cloud = _open_cloud(root/'cloud.npy', expected_frames*MAX_POINTS)
    frame_ids = []
    count = 0
    try:
        # Exhaustion matters: the generator checks commit/payload EOF only after
        # its last yield. Do not stop once expected_frames has been reached.
        for record in iter_records(root/'frames'):
            if record['kind'] != 'frame':
                continue
            frame_ids.append(record['metadata']['request_id'])
            if len(frame_ids) > expected_frames:
                raise ValueError('unexpected extra saved frame')
            delta = record['points_axis_mm']
            if delta is None:
                delta = np.empty((0, 3), dtype='<f8')
            size = len(delta)
            part = cloud[count:count+size]
            if (part.shape != delta.shape or part.dtype != delta.dtype
                    or not np.isfinite(part).all() or part.tobytes() != delta.tobytes()):
                raise ValueError('exported cloud differs from committed XYZ delta')
            count += size
        if len(frame_ids) != expected_frames or count != len(cloud):
            raise ValueError('saved frame/cloud count mismatch')
        if expected_cloud_points is not None and count != expected_cloud_points:
            raise ValueError('cloud logical/readback count mismatch')
    finally:
        mapping = getattr(cloud, '_mmap', None)
        if mapping is not None:
            mapping.close()
    preview_count = preview_bytes = 0
    if expect_preview:
        with (root/'preview.float64.bin').open('rb') as data, (root/'preview_index.jsonl').open('rb') as index:
            for frame_id in frame_ids:
                line = index.readline(4097)
                if not line.endswith(b'\n') or len(line) > 4096:
                    raise ValueError('missing/oversized preview index row')
                row = _json(line)
                if (set(row) != {'frame', 'offset', 'bytes', 'count', 'sha256'}
                        or any(type(row[key]) is not int for key in ('frame', 'offset', 'bytes', 'count'))
                        or row['frame'] != frame_id or row['offset'] != preview_bytes
                        or not 0 <= row['count'] <= MAX_POINTS or row['bytes'] != row['count']*24):
                    raise ValueError('preview identity/offset/count mismatch')
                blob = data.read(row['bytes'])
                if (len(blob) != row['bytes'] or hashlib.sha256(blob).hexdigest() != row['sha256']
                        or not np.isfinite(np.frombuffer(blob, dtype='<f8')).all()):
                    raise ValueError('preview length/hash/nonfinite mismatch')
                preview_bytes += len(blob)
                preview_count += row['count']
            if index.read(1) or data.read(1):
                raise ValueError('preview trailing index/data bytes')
        if expected_preview_points is not None and preview_count != expected_preview_points:
            raise ValueError('preview logical/readback count mismatch')
    return dict(schema='R26_STRICT_REPLAY_READBACK_V1', verified=True,
                scope='SAVED_PAYLOAD_INDEX_SOURCE_XYZ_CLOUD_AND_ENABLED_PREVIEW',
                strict_iterator_exhausted=True, frames=len(frame_ids), cloud_points=count,
                preview_verified=bool(expect_preview), preview_frames=len(frame_ids) if expect_preview else 0,
                preview_points=preview_count, preview_bytes=preview_bytes,
                physical_accuracy_verified=False)
