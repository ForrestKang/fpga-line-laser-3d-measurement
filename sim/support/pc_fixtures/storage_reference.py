"""Versioned R33 full-frame journal with bounded asynchronous ownership.

The journal embeds data and its authoritative index in one transaction. A live
acknowledgement is issued only after flush + fsync of that transaction. Reading
a complete transaction after a crash establishes readable contents, not proof
that an earlier fsync or acknowledgement happened. No old PFO3 ABI is reused.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import struct
import threading
import time
import zlib

import numpy as np

from .protocol import ARCHIVE_MAGIC, FINGERPRINT, HEADER_FIELDS, assemble, identity

FORMAT = 'R33_FULL_FRAME_JOURNAL_V1'
NAME = 'frames.r33j'
MAGIC = b'R33JNL1\0'
COMMIT_MAGIC = b'R33COM1\0'
HEADER = struct.Struct('<8sII6Q')
TRAILER = struct.Struct('<8sQQII32s')
MAX_FRAMES = 32
MAX_BYTES = 64 * 1024 * 1024
MAX_INDEX_BYTES = 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
COMPONENTS = ('raw', 'primary', 'observations_r33', 'measurement_xyz',
              'preview_xyz', 'receipt_json', 'metadata_json')


class StorageError(RuntimeError):
    """Accepted work remains owned by the writer on failure or timeout."""


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _json_load(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate R33 JSON key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(
                          ValueError('nonfinite R33 JSON')))


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _crc(data):
    return zlib.crc32(data) & 0xffffffff


def _payload_identity(payload):
    if len(payload) < 256 or len(payload) % 64:
        raise ValueError('R33 observations payload length')
    header = dict(zip(HEADER_FIELDS, struct.unpack_from('<64I', payload)))
    if (header['magic'], header['version'], header['total_bytes'],
            header['header_bytes'], header['record_bytes'],
            header['row_record_bytes']) != (ARCHIVE_MAGIC, 33, len(payload), 256, 64, 64):
        raise ValueError('R33 observations payload identity/version')
    if (header['width'], header['height'], header['row_count']) != (752, 480, 480):
        raise ValueError('R33 observations geometry')
    if (header['diagnostic_count'] > 180480 or
            header['qualified_count'] > header['diagnostic_count'] or
            header['measurement_count'] > header['preview_count'] or
            header['preview_count'] > header['qualified_count']):
        raise ValueError('R33 observations count capacity')
    if len(payload) != 256 + 64 * (header['diagnostic_count'] + header['row_count']):
        raise ValueError('R33 observations record count')
    if _crc(memoryview(payload)[256:]) != header['body_crc32']:
        raise ValueError('R33 observations body CRC')
    return identity(header), header


@dataclass(frozen=True)
class FrameResult:
    """Owned immutable bytes for exactly one computed R33 frame.

    ``create`` snapshots mutable arrays/maps once, before submission. Callers
    may then reuse their arrays. Byte inputs are retained only if immutable.
    Metadata may include target, angle, source_ready_ns and xyz_ready_ns; its
    frame_id must agree with the validated observations. Measurement and preview
    are independent little-endian float64 Nx3 arrays, never merged.
    """
    components: tuple
    descriptors: tuple
    identity_items: tuple
    charge_bytes: int

    @classmethod
    def create(cls, *, raw, primary, frame, measurement_xyz, preview_xyz,
               receipt, metadata):
        raw = bytes(raw)
        primary = bytes(primary)
        payload = bytes(frame.payload)
        if len(raw) != 752 * 480:
            raise ValueError('R33 Raw must be full 752x480')
        frame_identity, header = _payload_identity(payload)
        if identity(frame.header) != frame_identity:
            raise ValueError('Frame header does not bind payload')
        metadata = dict(metadata)
        if metadata.get('frame_id', frame_identity['frame_id']) != frame_identity['frame_id']:
            raise ValueError('metadata frame identity mismatch')
        metadata['frame_id'] = frame_identity['frame_id']
        metadata['coordinate_contract'] = 'R33_UV_Q12'
        xyz_bytes = []
        for mode, values in [('measurement', measurement_xyz), ('preview', preview_xyz)]:
            values = np.asarray(values, dtype='<f8')
            if values.ndim != 2 or values.shape[1] != 3 or not np.isfinite(values).all():
                raise ValueError(mode + ' XYZ must be finite Nx3')
            if len(values) > header[mode + '_count']:
                raise ValueError(mode + ' XYZ exceeds PL accepted observations')
            xyz_bytes.append(values.tobytes(order='C'))
        receipt = dict(receipt)
        known_sha = {'raw': _digest(raw), 'primary': _digest(primary),
                     'observations_r33': _digest(payload)}
        if 'input_sha256' in receipt and receipt['input_sha256'] != known_sha['raw']:
            raise ValueError('receipt input_sha256 mismatch')
        for key, actual in [('raw_sha256', known_sha['raw']),
                            ('primary_stream_sha256', known_sha['primary']),
                            ('assembled_sha256', known_sha['observations_r33'])]:
            if key in receipt and receipt[key] != actual:
                raise ValueError('receipt ' + key + ' mismatch')
            receipt[key] = actual
        if receipt.get('algorithm_fingerprint', FINGERPRINT) != FINGERPRINT:
            raise ValueError('receipt algorithm fingerprint mismatch')
        receipt['algorithm_fingerprint'] = FINGERPRINT
        receipt['storage_contract'] = FORMAT
        encoded_receipt = _json_bytes(receipt)
        encoded_metadata = _json_bytes(metadata)
        if max(len(encoded_receipt), len(encoded_metadata)) > MAX_JSON_BYTES:
            raise ValueError('R33 receipt/metadata capacity')
        blobs = (raw, primary, payload, *xyz_bytes, encoded_receipt, encoded_metadata)
        descriptors = tuple((name, len(blob), known_sha[name] if name in known_sha else _digest(blob), _crc(blob))
                            for name, blob in zip(COMPONENTS, blobs))
        # Account all owned bytes, authoritative per-frame index and framing.
        # Each index template includes conservative 20-digit sequence/time and
        # 20-digit offsets. No batch serialization duplicates the large blobs.
        template = _index_row(10**20-1, 10**20-1, frame_identity, descriptors,
                              data_offset=10**20-1)
        charge = sum(map(len, blobs)) + len(_json_bytes(template)) + 256
        return cls(tuple(zip(COMPONENTS, blobs)), descriptors,
                   tuple(sorted(frame_identity.items())), charge)

    @property
    def frame_id(self):
        return dict(self.identity_items)['frame_id']


def _index_row(sequence, accepted_ns, frame_identity, descriptors, data_offset):
    parts = []
    for name, size, sha, crc in descriptors:
        parts.append(dict(name=name, offset=data_offset, bytes=size,
                          sha256=sha, crc32=crc))
        data_offset += size
    return dict(sequence=sequence, accepted_ns=accepted_ns,
                identity=frame_identity, components=parts)


def _write_all(stream, data):
    view = memoryview(data).cast('B')
    while view:
        count = stream.write(view)
        if count is None or count <= 0:
            raise OSError('short R33 journal write')
        view = view[count:]


def _directory_sync(path):
    if os.name != 'posix':
        return False
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


class AsyncWriter:
    """Single storage owner; pending limits include active and queued frames.

    ``submit`` returns an accepted sequence, not a durability acknowledgement.
    Read ``commits`` or ``status()`` for live post-fsync acknowledgements.
    ``drain``/``close`` wait with a finite timeout. A stuck kernel fsync cannot
    be forcefully made durable: timeout leaves the worker and original pending
    bytes owned and reports the unacknowledged range explicitly.
    """
    def __init__(self, output, *, capacity_frames=MAX_FRAMES, capacity_bytes=MAX_BYTES,
                 group=8, max_delay_s=.180, fsync=None):
        if type(capacity_frames) is not int or not 1 <= capacity_frames <= MAX_FRAMES:
            raise ValueError('R33 writer frame budget')
        if type(capacity_bytes) is not int or not 1 <= capacity_bytes <= MAX_BYTES:
            raise ValueError('R33 writer byte budget')
        if type(group) is not int or not 1 <= group <= capacity_frames:
            raise ValueError('R33 writer group')
        if not 0 <= max_delay_s <= 5:
            raise ValueError('R33 writer delay')
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.capacity_frames = capacity_frames
        self.capacity_bytes = capacity_bytes
        self.group = group
        self.max_delay_s = float(max_delay_s)
        self._sync = fsync if fsync is not None else os.fsync
        self._cv = threading.Condition()
        self._queue = deque()
        self._owned = {}
        self._identities = set()
        self._pending_bytes = 0
        self._closing = False
        self._force = False
        self._error = None
        self._next_sequence = 1
        self._commits = []
        self._transactions = []
        self._peak_frames = self._peak_bytes = 0
        self._worker_stopped = False
        self._stream = (self.output / NAME).open('xb', buffering=0)
        try:
            with (self.output / 'FORMAT.json').open('xb') as f:
                _write_all(f, _json_bytes(dict(format=FORMAT, version=1,
                    components=list(COMPONENTS), xyz_dtype='<f8', xyz_columns=3,
                    durability='embedded_data_and_index_fsync_before_live_ack',
                    recovery='complete_transaction_does_not_prove_original_ack')))
                f.flush()
                os.fsync(f.fileno())
            self.directory_fsynced = _directory_sync(self.output)
            _directory_sync(self.output.parent)
        except BaseException:
            self._stream.close()
            raise
        self._thread = threading.Thread(target=self._run, name='r33-storage', daemon=True)
        self._thread.start()

    def _raise_error(self):
        if self._error is not None:
            raise StorageError('R33 storage failed: ' + repr(self._error)) from self._error

    def submit(self, result, *, timeout_s=5.0):
        if not isinstance(result, FrameResult):
            raise TypeError('submit requires FrameResult.create snapshot')
        if result.charge_bytes > self.capacity_bytes:
            raise BufferError('R33 frame exceeds writer byte budget; not accepted')
        if timeout_s < 0:
            raise ValueError('negative submit timeout')
        deadline = time.monotonic() + timeout_s
        with self._cv:
            while True:
                self._raise_error()
                if self._closing:
                    raise StorageError('R33 writer closing; not accepted')
                if result.identity_items in self._identities:
                    raise ValueError('duplicate R33 frame identity; not accepted')
                if (len(self._owned) < self.capacity_frames and
                        self._pending_bytes + result.charge_bytes <= self.capacity_bytes):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BufferError('R33 writer backpressure timeout; not accepted')
                self._cv.wait(remaining)
            seq = self._next_sequence
            self._next_sequence += 1
            item = (seq, time.perf_counter_ns(), result)
            self._queue.append(item)
            self._owned[seq] = item
            self._identities.add(result.identity_items)
            self._pending_bytes += result.charge_bytes
            self._peak_frames = max(self._peak_frames, len(self._owned))
            self._peak_bytes = max(self._peak_bytes, self._pending_bytes)
            self._cv.notify_all()
            return seq

    @property
    def commits(self):
        with self._cv:
            return tuple(dict(row) for row in self._commits)

    def commits_since(self, cursor=0):
        """Copy only ACKs not yet read by this consumer.

        The cursor is the number of persisted commit rows already consumed,
        initially zero. Each independent consumer owns its own cursor. Snapshot
        and next cursor are obtained under the same lock; returned dictionaries
        cannot mutate internal history. A future cursor is rejected, never
        silently clamped. No pending or merely written frame is exposed here.
        """
        if type(cursor) is not int or cursor < 0:
            raise ValueError('nonnegative integer commit cursor required')
        with self._cv:
            end = len(self._commits)
            if cursor > end:
                raise ValueError('commit cursor exceeds persisted history')
            return dict(cursor=end,
                        commits=tuple(dict(row) for row in self._commits[cursor:end]))

    @property
    def retained_on_failure(self):
        with self._cv:
            return tuple(self._owned[k][2] for k in sorted(self._owned))

    def _counters_locked(self):
        return dict(format=FORMAT, accepted=self._next_sequence-1,
            persisted=len(self._commits), pending_frames=len(self._owned),
            pending_bytes=self._pending_bytes, peak_frames=self._peak_frames,
            peak_bytes=self._peak_bytes, queued_frames=len(self._queue),
            error=None if self._error is None else repr(self._error),
            closing=self._closing, worker_stopped=self._worker_stopped,
            directory_fsynced=self.directory_fsynced)

    def counters(self):
        """Constant-size live state; does not walk owned frames or history.

        Use status() once at final export for detailed pending identities and
        transaction timing. This method changes neither ACK nor ownership state.
        """
        with self._cv:
            values = self._counters_locked()
            values['transaction_count'] = len(self._transactions)
            return values

    def status(self):
        with self._cv:
            values = self._counters_locked()
            values.update(unacknowledged_sequences=tuple(sorted(self._owned)),
                          transactions=tuple(dict(row) for row in self._transactions))
            return values

    def _take_batch(self):
        with self._cv:
            while not self._queue:
                if self._closing:
                    return None
                self._cv.wait()
            while len(self._queue) < self.group and not (self._force or self._closing):
                remaining = self.max_delay_s - (time.perf_counter_ns()-self._queue[0][1])/1e9
                if remaining <= 0:
                    break
                self._cv.wait(remaining)
            count = min(self.group, len(self._queue))
            return [self._queue.popleft() for _ in range(count)]

    def _write_batch(self, batch, txn):
        start_ns = time.perf_counter_ns()
        rows = []
        offset = 0
        for sequence, accepted_ns, result in batch:
            rows.append(_index_row(sequence, accepted_ns, dict(result.identity_items),
                                   result.descriptors, offset))
            offset += sum(len(blob) for _, blob in result.components)
        index = _json_bytes(dict(format=FORMAT, transaction=txn, frames=rows))
        if len(index) > MAX_INDEX_BYTES or offset > self.capacity_bytes:
            raise ValueError('R33 transaction capacity')
        charged = sum(item[2].charge_bytes for item in batch)
        if HEADER.size + offset + len(index) + TRAILER.size > charged:
            raise ValueError('R33 transaction exceeded reserved byte accounting')
        header = HEADER.pack(MAGIC, 1, HEADER.size, txn, batch[0][0], len(batch),
                             offset, len(index), 0)
        digest = hashlib.sha256()
        checksum = 0
        physical = self._stream.tell()
        for piece in (header,):
            _write_all(self._stream, piece)
            digest.update(piece)
            checksum = zlib.crc32(piece, checksum)
        for _, _, result in batch:
            for _, blob in result.components:
                _write_all(self._stream, blob)
                digest.update(blob)
                checksum = zlib.crc32(blob, checksum)
        _write_all(self._stream, index)
        digest.update(index)
        checksum = zlib.crc32(index, checksum)
        total = HEADER.size + offset + len(index) + TRAILER.size
        _write_all(self._stream, TRAILER.pack(COMMIT_MAGIC, txn, total,
                                             checksum & 0xffffffff, 0, digest.digest()))
        self._stream.flush()
        sync_start_ns = time.perf_counter_ns()
        self._sync(self._stream.fileno())
        persisted_ns = time.perf_counter_ns()
        return dict(transaction=txn, first_sequence=batch[0][0], frames=len(batch),
            physical_offset=physical, bytes=total, write_start_ns=start_ns,
            sync_start_ns=sync_start_ns, persisted_ns=persisted_ns,
            write_s=(sync_start_ns-start_ns)/1e9,
            fsync_s=(persisted_ns-sync_start_ns)/1e9, fsync_calls=1)

    def _run(self):
        try:
            txn = 0
            while True:
                batch = self._take_batch()
                if batch is None:
                    break
                txn += 1
                metrics = self._write_batch(batch, txn)
                with self._cv:
                    self._transactions.append(metrics)
                    for seq, accepted_ns, result in batch:
                        metadata = _json_load(result.components[-1][1])
                        self._commits.append(dict(sequence=seq, frame_id=result.frame_id,
                            accepted_ns=accepted_ns, persisted_ns=metrics['persisted_ns'],
                            write_start_ns=metrics['write_start_ns'],
                            sync_start_ns=metrics['sync_start_ns'],
                            source_ready_ns=metadata.get('source_ready_ns'),
                            xyz_ready_ns=metadata.get('xyz_ready_ns'), transaction=txn))
                        del self._owned[seq]
                        self._pending_bytes -= result.charge_bytes
                    # The local batch must not retain acknowledged large blobs
                    # while _take_batch waits and a fresh queue is admitted.
                    batch.clear()
                    del result
                    if not self._owned:
                        self._force = False
                    self._cv.notify_all()
        except BaseException as exc:
            with self._cv:
                self._error = exc
                self._cv.notify_all()
        finally:
            self._stream.close()
            with self._cv:
                self._worker_stopped = True
                self._cv.notify_all()

    def drain(self, *, timeout_s=30.0):
        if timeout_s < 0:
            raise ValueError('negative drain timeout')
        deadline = time.monotonic() + timeout_s
        with self._cv:
            self._force = True
            self._cv.notify_all()
            while self._owned:
                self._raise_error()
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise StorageError('R33 drain timeout; accepted bytes remain owned/unacknowledged')
                self._cv.wait(remaining)
            self._raise_error()
        return self.status()

    def close(self, *, timeout_s=30.0):
        if timeout_s < 0:
            raise ValueError('negative close timeout')
        deadline = time.monotonic()+timeout_s
        with self._cv:
            self._closing = True
            self._force = True
            self._cv.notify_all()
        self._thread.join(max(0, deadline-time.monotonic()))
        if self._thread.is_alive():
            raise StorageError('R33 close timeout; worker still owns accepted bytes')
        with self._cv:
            self._raise_error()
            if self._owned:
                raise StorageError('R33 close with unacknowledged accepted bytes')
        return self.status()


def _read_exact(stream, count):
    data = stream.read(count)
    if len(data) != count:
        raise ValueError('truncated R33 journal data')
    return data


def _validate_frame(stream, data_start, data_bytes, row, expected_offset, *,
                    strict_protocol, xyz_digests):
    if set(row) != {'sequence', 'accepted_ns', 'identity', 'components'}:
        raise ValueError('R33 frame index fields')
    if tuple(part.get('name') for part in row['components']) != COMPONENTS:
        raise ValueError('R33 frame component completeness/order')
    blobs = {}
    for part in row['components']:
        if set(part) != {'name', 'offset', 'bytes', 'sha256', 'crc32'}:
            raise ValueError('R33 component descriptor fields')
        size = part['bytes']
        if (type(size) is not int or size < 0 or size > MAX_BYTES or
                part['offset'] != expected_offset or expected_offset+size > data_bytes):
            raise ValueError('R33 component bounds/continuity')
        if part['name'] in ('receipt_json', 'metadata_json') and size > MAX_JSON_BYTES:
            raise ValueError('R33 archived JSON capacity')
        stream.seek(data_start + expected_offset)
        blob = _read_exact(stream, size)
        if _digest(blob) != part['sha256'] or _crc(blob) != part['crc32']:
            raise ValueError('R33 component SHA/CRC mismatch')
        blobs[part['name']] = blob
        expected_offset += size
    if len(blobs['raw']) != 752*480:
        raise ValueError('R33 archived Raw geometry')
    frame_identity, header = _payload_identity(blobs['observations_r33'])
    if frame_identity != row['identity']:
        raise ValueError('R33 archived frame identity mismatch')
    receipt = _json_load(blobs['receipt_json'])
    metadata = _json_load(blobs['metadata_json'])
    if metadata.get('frame_id') != frame_identity['frame_id']:
        raise ValueError('R33 archived metadata identity mismatch')
    if receipt.get('storage_contract') != FORMAT or receipt.get('algorithm_fingerprint') != FINGERPRINT:
        raise ValueError('R33 archived receipt contract')
    for name, key in [('raw', 'raw_sha256'), ('primary', 'primary_stream_sha256'),
                      ('observations_r33', 'assembled_sha256')]:
        if receipt.get(key) != _digest(blobs[name]):
            raise ValueError('R33 archived source receipt mismatch')
    counts = {}
    for mode in ('measurement', 'preview'):
        xyz = blobs[mode+'_xyz']
        if len(xyz) % 24 or not np.isfinite(np.frombuffer(xyz, '<f8')).all():
            raise ValueError('R33 archived XYZ geometry/finiteness')
        counts[mode] = len(xyz)//24
        if counts[mode] > header[mode+'_count']:
            raise ValueError('R33 archived XYZ exceeds PL accepted count')
        xyz_digests[mode].update(xyz)
    if strict_protocol:
        frame, _ = assemble(blobs['primary'], expected_identity=frame_identity)
        if frame.payload != blobs['observations_r33']:
            raise ValueError('R33 strict primary/payload mismatch')
    return expected_offset, counts


def readback(output, *, strict_protocol=True, recover_incomplete_tail=False):
    """Stream-check complete transactions without loading a whole run in RAM.

    Strict mode also reassembles every actual primary through the R33 decoder.
    Recovery may ignore only a physically incomplete final transaction. Complete
    corrupt transactions are rejected even in recovery mode. No live fsync ACK
    is inferred from file bytes.
    """
    output = Path(output)
    descriptor = _json_load((output/'FORMAT.json').read_bytes())
    if descriptor.get('format') != FORMAT or descriptor.get('version') != 1:
        raise ValueError('not an R33 full-frame journal')
    path = output / NAME
    size = path.stat().st_size
    frames = measurement = preview = transactions = 0
    position = 0
    identities = set()
    summaries = []
    incomplete = False
    xyz_digests = {mode: hashlib.sha256() for mode in ('measurement', 'preview')}
    with path.open('rb') as stream:
        while position < size:
            stream.seek(position)
            if size-position < HEADER.size:
                if recover_incomplete_tail:
                    incomplete = True
                    break
                raise ValueError('truncated R33 journal header')
            header_bytes = _read_exact(stream, HEADER.size)
            magic, version, hbytes, txn, first, count, data_bytes, index_bytes, reserved = HEADER.unpack(header_bytes)
            if (magic, version, hbytes, reserved) != (MAGIC, 1, HEADER.size, 0):
                raise ValueError('R33 journal magic/version/reserved')
            if (txn != transactions+1 or first != frames+1 or not 1 <= count <= MAX_FRAMES
                    or data_bytes > MAX_BYTES or not 1 <= index_bytes <= MAX_INDEX_BYTES):
                raise ValueError('R33 transaction sequence/capacity')
            total = HEADER.size+data_bytes+index_bytes+TRAILER.size
            if position+total > size:
                if recover_incomplete_tail:
                    incomplete = True
                    break
                raise ValueError('truncated R33 journal transaction')
            digest = hashlib.sha256(header_bytes)
            checksum = zlib.crc32(header_bytes)
            remaining = data_bytes+index_bytes
            while remaining:
                chunk = _read_exact(stream, min(256*1024, remaining))
                digest.update(chunk)
                checksum = zlib.crc32(chunk, checksum)
                remaining -= len(chunk)
            cmagic, ctxn, ctotal, ccrc, creserved, csha = TRAILER.unpack(_read_exact(stream, TRAILER.size))
            if (cmagic, ctxn, ctotal, creserved) != (COMMIT_MAGIC, txn, total, 0):
                raise ValueError('R33 commit framing mismatch')
            if ccrc != checksum & 0xffffffff or csha != digest.digest():
                raise ValueError('R33 transaction SHA/CRC mismatch')
            data_start = position+HEADER.size
            stream.seek(data_start+data_bytes)
            index = _json_load(_read_exact(stream, index_bytes))
            if (set(index) != {'format', 'transaction', 'frames'} or
                    index['format'] != FORMAT or index['transaction'] != txn or
                    len(index['frames']) != count):
                raise ValueError('R33 authoritative index mismatch')
            offset = 0
            for row in index['frames']:
                if row['sequence'] != frames+1:
                    raise ValueError('R33 frame sequence mismatch')
                offset, counts = _validate_frame(stream, data_start, data_bytes, row, offset,
                    strict_protocol=strict_protocol, xyz_digests=xyz_digests)
                key = tuple(sorted(row['identity'].items()))
                if key in identities:
                    raise ValueError('R33 duplicate archived frame identity')
                identities.add(key)
                summaries.append(dict(sequence=row['sequence'], frame_id=row['identity']['frame_id'],
                    measurement_points=counts['measurement'], preview_points=counts['preview'],
                    component_sha256={part['name']: part['sha256'] for part in row['components']}))
                frames += 1
                measurement += counts['measurement']
                preview += counts['preview']
            if offset != data_bytes:
                raise ValueError('R33 unindexed/missing data')
            position += total
            transactions += 1
    return dict(format=FORMAT, passed=True, frames=frames, transactions=transactions,
        measurement_points=measurement, preview_points=preview, frame_summaries=summaries,
        valid_bytes=position, file_bytes=size, incomplete_tail=incomplete,
        strict_protocol=bool(strict_protocol), original_live_durability_ack_inferred=False,
        measurement_xyz_sha256=xyz_digests['measurement'].hexdigest(),
        preview_xyz_sha256=xyz_digests['preview'].hexdigest())


# Public name used by the unified R33 replay entrypoint.
read_run = readback
