"""Strict native-Raw v1/v2 chunk reader, with bounded zlib expansion.

Structural commit markers alone do not prove fsync success. Pass a trusted
receipt's written_watermark to reject markers above confirmed durable work.
No reader can establish power-loss durability from these files alone.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import zlib

import numpy as np

from .stream_writer import _validate_json_value


_RAW_BYTES = 752*480
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_RAW_V2 = frozenset(('raw_codec', 'raw_stored_bytes', 'raw_stored_sha256'))
_POINT_KEYS = ('points_camera_mm', 'points_axis_mm')


def _integer(value, name, maximum=None):
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise ValueError(f'{name} must be a bounded nonnegative integer')
    return value


def _hash(data, expected, name):
    if type(expected) is not str or not _HASH.fullmatch(expected) or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f'{name} SHA-256 mismatch')


def _raw_size(row):
    if _integer(row.get('raw_bytes'), 'raw_bytes') != _RAW_BYTES:
        raise ValueError('native Raw must retain exactly 752x480 bytes')
    if row.get('storage_format') == 'scan3d_chunk_v1':
        if _RAW_V2.intersection(row):
            raise ValueError('v1 cannot carry compressed Raw fields')
        return _RAW_BYTES
    if row.get('storage_format') != 'scan3d_chunk_zlib_v2' or row.get('raw_codec') != 'zlib':
        raise ValueError('unknown storage format or Raw codec')
    size = _integer(row.get('raw_stored_bytes'), 'raw_stored_bytes', _RAW_BYTES+4096)
    if size == 0:
        raise ValueError('compressed Raw cannot be empty')
    return size


def decode_raw_block(row, stored):
    """Verify stored/original hashes and reject truncated, extra or huge output."""
    if type(stored) is not bytes or len(stored) != _raw_size(row):
        raise ValueError('Raw stored byte length mismatch')
    if row['storage_format'] == 'scan3d_chunk_v1':
        raw = stored
    else:
        _hash(stored, row.get('raw_stored_sha256'), 'stored Raw')
        decoder = zlib.decompressobj()
        try:
            # Never call unbounded flush(). One extra byte detects expansion
            # beyond native geometry before allocating the advertised output.
            raw = decoder.decompress(stored, _RAW_BYTES+1)
        except zlib.error as exc:
            raise ValueError('invalid zlib Raw stream') from exc
        if len(raw) != _RAW_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('Raw zlib stream is oversized, truncated or has trailing data')
    _hash(raw, row.get('raw_sha256'), 'original Raw')
    return raw


def _block(stream, offset, size, maximum, file_bytes):
    offset = _integer(offset, 'payload offset', file_bytes)
    size = _integer(size, 'payload bytes', maximum)
    if offset+size > file_bytes:
        raise ValueError('payload block extends beyond file')
    stream.seek(offset)
    data = stream.read(size)
    if len(data) != size:
        raise ValueError('truncated payload block')
    return data


def _points(data):
    stream = io.BytesIO(data)
    try:
        version = np.lib.format.read_magic(stream)
        if version == (1,0):
            shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream)
        elif version == (2,0):
            shape, fortran, dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            raise ValueError('unsupported numeric NPY version')
        if (len(shape) != 2 or shape[1] != 3 or type(shape[0]) is not int or not 0 <= shape[0] <= 480
                or fortran or dtype.hasobject or dtype.kind not in 'iuf'
                or stream.tell()+shape[0]*3*dtype.itemsize != len(data)):
            raise ValueError('invalid or unbounded XYZ NPY layout')
        points = np.load(io.BytesIO(data), allow_pickle=False)
        if not np.isfinite(points).all():
            raise ValueError('nonfinite XYZ')
        points.setflags(write=False)
        return points
    except (EOFError, TypeError, OSError) as exc:
        raise ValueError('invalid XYZ NPY block') from exc


def _read_frame(directory, row, expected_offset=None):
    if type(row) is not dict or row.get('kind') != 'frame':
        raise ValueError('frame index object required')
    _validate_json_value(row, path='frame index')
    _integer(row.get('sequence'), 'sequence')
    _integer(row.get('request_id'), 'request_id', (1<<32)-1)
    _integer(row.get('capture_epoch'), 'capture_epoch', (1<<16)-1)
    if row.get('raw_file') != 'payloads.bin' or row.get('profile_file') != 'payloads.bin':
        raise ValueError('only the local payloads.bin file is allowed')
    payload = Path(directory)/'payloads.bin'
    if payload.is_symlink():
        raise ValueError('payload file must not be a symlink')
    file_bytes = payload.stat().st_size
    cursor = _integer(row.get('raw_offset'), 'raw_offset', file_bytes)
    if expected_offset is not None and cursor != expected_offset:
        raise ValueError('noncontiguous frame payload')
    size = _raw_size(row)
    with payload.open('rb') as stream:
        raw = decode_raw_block(row, _block(stream, cursor, size, _RAW_BYTES+4096, file_bytes))
        cursor += size
        if row.get('profile_offset') != cursor or type(row.get('profile_offset')) is not int:
            raise ValueError('profile offset must immediately follow Raw')
        profile_size = _integer(row.get('profile_bytes'), 'profile_bytes', 128+480*24)
        profile = _block(stream, cursor, profile_size, 128+480*24, file_bytes)
        _hash(profile, row.get('profile_sha256'), 'profile')
        cursor += profile_size
        refs = row.get('point_files')
        if type(refs) is not dict or set(refs) != set(_POINT_KEYS):
            raise ValueError('both XYZ references required')
        arrays = {}
        for key in _POINT_KEYS:
            ref = refs[key]
            arrays[key] = None
            if ref is None:
                continue
            if (type(ref) is not dict or ref.get('file') != 'payloads.bin' or ref.get('format') != 'npy'
                    or type(ref.get('offset')) is not int or ref['offset'] != cursor):
                raise ValueError('XYZ reference layout mismatch')
            block = _block(stream, cursor, ref.get('bytes'), 480*3*16+10000, file_bytes)
            _hash(block, ref.get('sha256'), key)
            arrays[key] = _points(block)
            cursor += len(block)
    return dict(row, raw=raw, profile_payload=profile, **arrays), cursor


def read_chunk_frame(directory, row):
    """Read one indexed measurement; commit/watermark checks require iterator."""
    return _read_frame(directory, row)[0]


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _index(path):
    with path.open('rb') as stream:
        while True:
            line = stream.readline(1024*1024+1)
            if not line:
                return
            if len(line) > 1024*1024 or not line.endswith(b'\n'):
                raise ValueError('oversized or incomplete index line')
            value = json.loads(line, object_pairs_hook=_unique)
            if type(value) is not dict:
                raise ValueError('index line must be an object')
            _validate_json_value(value, path='index')
            yield value


def iter_chunk_frames(directory, *, written_watermark=None):
    """Yield structurally committed frames, optionally bounded by a receipt.

    Reject incomplete groups, unmatched events, malformed counts, sequence gaps,
    noncontiguous offsets and trailing payload. Without a watermark this verifies
    only file structure and hashes, not a successful durable writer receipt.
    """
    directory = Path(directory)
    if written_watermark is not None:
        _integer(written_watermark, 'written_watermark')
    events, last_event_sequence = {}, 0
    for row in _index(directory/'events.jsonl'):
        sequence = _integer(row.get('sequence'), 'event sequence')
        if sequence <= last_event_sequence or 'event' not in row:
            raise ValueError('unordered, duplicate or missing event')
        events[sequence] = row
        last_event_sequence = sequence
    pending, last_sequence, cursor = [], 0, 0
    last_request = epoch = None
    for row in _index(directory/'frames.jsonl'):
        if row.get('kind') == 'frame':
            pending.append(row)
            continue
        if row.get('kind') != 'batch_commit':
            raise ValueError('unknown frame-index record kind')
        first = _integer(row.get('first_sequence'), 'first_sequence')
        last = _integer(row.get('last_sequence'), 'last_sequence')
        if first != last_sequence+1 or last < first or (written_watermark is not None and last > written_watermark):
            raise ValueError('commit exceeds confirmed watermark or has a sequence gap')
        group_events = [events[s] for s in sorted(events) if first <= s <= last]
        sequences = [r.get('sequence') for r in pending]+[r['sequence'] for r in group_events]
        if (last-first+1 != len(sequences) or any(type(s) is not int for s in sequences)
                or [r['sequence'] for r in pending] != sorted(r['sequence'] for r in pending)
                or sorted(sequences) != list(range(first,last+1))
                or _integer(row.get('item_count'), 'item_count') != last-first+1
                or _integer(row.get('frame_count'), 'frame_count') != len(pending)
                or _integer(row.get('event_count'), 'event_count') != len(group_events)):
            raise ValueError('commit item/frame/event counts mismatch')
        for value in pending+group_events:
            if value.get('batch_first_sequence') != first or value.get('batch_last_sequence') != last:
                raise ValueError('item belongs to another batch')
        restored = []
        for value in pending:
            frame, cursor = _read_frame(directory, value, expected_offset=cursor)
            request, current_epoch = frame['request_id'], frame['capture_epoch']
            if (last_request is not None and request <= last_request) or (epoch is not None and current_epoch != epoch):
                raise ValueError('measurement identity regressed or epoch changed')
            last_request, epoch = request, current_epoch
            restored.append(frame)
        if _integer(row.get('payload_end'), 'payload_end') != cursor:
            raise ValueError('commit payload end mismatch')
        for value in group_events:
            del events[value['sequence']]
        pending, last_sequence = [], last
        yield from restored
    if pending or events or (directory/'payloads.bin').stat().st_size != cursor:
        raise ValueError('uncommitted records or trailing payload')
    if written_watermark is not None and written_watermark != last_sequence:
        raise ValueError('confirmed watermark does not match completed index')


__all__ = ['decode_raw_block', 'read_chunk_frame', 'iter_chunk_frames']
