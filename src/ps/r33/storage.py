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
import ctypes
import errno
import json
import math
import os
from pathlib import Path
import struct
import threading
import time
import zlib

import numpy as np

from .protocol import (ARCHIVE_MAGIC, FINGERPRINT, BIN_FINGERPRINT,
                       SEED_RETRY_FINGERPRINT, HEADER_FIELDS, assemble,
                       assemble_r33, identity)

FORMAT_V1 = 'R33_FULL_FRAME_JOURNAL_V1'
# R38: V2 keeps the V1 transaction framing and logical SHA/CRC contract, but a
# component may be stored zlib-encoded (Raw only by default). Descriptors keep
# the decoded size/SHA/CRC, so every downstream digest is unchanged.
FORMAT = 'R38_FULL_FRAME_JOURNAL_V2'
NAME = 'frames.r33j'
MAGIC_V1 = b'R33JNL1\0'
MAGIC = b'R38JNL2\0'
VERSION = 2
FORMATS = {FORMAT_V1: (MAGIC_V1, 1), FORMAT: (MAGIC, VERSION)}
ENCODINGS = ('identity', 'zlib')
RAW_ENCODING = 'zlib'
COMMIT_MAGIC = b'R33COM1\0'
HEADER = struct.Struct('<8sII6Q')
TRAILER = struct.Struct('<8sQQII32s')
MAX_FRAMES = 128
DEFAULT_FRAMES = 32
MAX_TRANSACTION_FRAMES = 32
MAX_BYTES = 64 * 1024 * 1024
MAX_INDEX_BYTES = 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
# Source sealing runs beside Python DMA/geometry management. Bulk hashlib and
# zlib calls release the GIL and can pay another scheduling interval per call.
# The bounded-call experiment remains reproducible, but the actual ARM v6
# short run did not improve full-chain speed or CPU. Keep the proven bulk
# path as the production default; 2047 is retained for controlled A/B tests.
SOURCE_CHECKSUM_CHUNK_BYTES = 0
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


def _source_checksums(data):
    chunk_bytes = SOURCE_CHECKSUM_CHUNK_BYTES
    if not chunk_bytes:
        return _digest(data), _crc(data)
    digest, crc = hashlib.sha256(), 0
    view = memoryview(data).cast('B')
    for start in range(0, len(view), chunk_bytes):
        chunk = view[start:start + chunk_bytes]
        digest.update(chunk)
        crc = zlib.crc32(chunk, crc)
    return digest.hexdigest(), crc & 0xffffffff


def _source_crc(data):
    chunk_bytes = SOURCE_CHECKSUM_CHUNK_BYTES
    if not chunk_bytes:
        return _crc(data)
    crc, view = 0, memoryview(data).cast('B')
    for start in range(0, len(view), chunk_bytes):
        crc = zlib.crc32(view[start:start + chunk_bytes], crc)
    return crc & 0xffffffff


def _payload_identity(payload, *, crc_function=_crc):
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
    if crc_function(memoryview(payload)[256:]) != header['body_crc32']:
        raise ValueError('R33 observations body CRC')
    return identity(header), header


def _archive_fingerprint(header):
    revision=(header['algorithm_revision'],header['fixed_revision'],header['policy_revision'])
    expected={(33,1,1):FINGERPRINT,(33,1,2):BIN_FINGERPRINT,
              (33,1,3):SEED_RETRY_FINGERPRINT}.get(revision)
    actual=struct.pack('<8I',*(header['fingerprint'+str(i)] for i in range(8))).hex()
    if expected is None or actual!=expected:
        raise ValueError('R33 archived algorithm identity mismatch')
    return expected


def _decode_archived_primary(primary, frame_identity, algorithm_fp, header):
    """Dispatch known envelopes without retrying a failed strict decoder."""
    if len(primary) < 8:
        raise ValueError('truncated archived primary envelope')
    envelope = struct.unpack_from('<2I', primary)
    if envelope == (0x33544850, 33):
        decoder = assemble_r33
    elif envelope == (0x35544850, 35):
        decoder = assemble
    else:
        raise ValueError('unknown archived primary envelope')
    return decoder(primary, expected_identity=frame_identity,
                   expected_fingerprint=algorithm_fp,
                   binning=header['policy_revision'] == 2)


@dataclass(frozen=True, init=False)
class SourceSnapshot:
    """Validated immutable source owned before geometry/display processing.

    Creation performs all source checks and hashes exactly once. The returned
    object contains only immutable bytes/tuples and can cross an in-process
    queue without retaining a mutable DMA array. It is not a receipt supplied
    by an external caller: public construction must use ``create``.
    """
    components: tuple
    descriptors: tuple
    identity_items: tuple
    measurement_count: int
    preview_count: int
    encodings: tuple = ()

    @classmethod
    def create(cls, *, raw, primary, frame, receipt, raw_encoding=None, raw_zlib_stream=None):
        raw_encoding = RAW_ENCODING if raw_encoding is None else raw_encoding
        if raw_encoding not in ENCODINGS:
            raise ValueError('R38 Raw storage encoding')
        # raw_zlib_stream: the single zlib stream replay.read_prepared_input
        # already decoded and SHA-checked into exactly this Raw. Reusing it saves
        # a second compression on the ARM; readback decodes and checks the SHA.
        verified_stream = raw_zlib_stream
        raw, primary, payload = bytes(raw), bytes(primary), bytes(frame.payload)
        if len(raw) != 752 * 480:
            raise ValueError('R33 Raw must be full 752x480')
        frame_identity, header = _payload_identity(payload, crc_function=_source_crc)
        if identity(frame.header) != frame_identity:
            raise ValueError('Frame header does not bind payload')
        receipt = dict(receipt)
        checksums = {name: _source_checksums(blob) for name, blob in
                     [('raw', raw), ('primary', primary), ('observations_r33', payload)]}
        known_sha = {name: pair[0] for name, pair in checksums.items()}
        if 'input_sha256' in receipt and receipt['input_sha256'] != known_sha['raw']:
            raise ValueError('receipt input_sha256 mismatch')
        for key, actual in [('raw_sha256', known_sha['raw']),
                            ('primary_stream_sha256', known_sha['primary']),
                            ('assembled_sha256', known_sha['observations_r33'])]:
            if key in receipt and receipt[key] != actual:
                raise ValueError('receipt ' + key + ' mismatch')
            receipt[key] = actual
        algorithm_fp=_archive_fingerprint(header)
        if receipt.get('algorithm_fingerprint', algorithm_fp) != algorithm_fp:
            raise ValueError('receipt algorithm fingerprint mismatch')
        receipt['algorithm_fingerprint'] = algorithm_fp
        receipt['storage_contract'] = FORMAT
        encoded_receipt = _json_bytes(receipt)
        if len(encoded_receipt) > MAX_JSON_BYTES:
            raise ValueError('R33 receipt/metadata capacity')
        blobs = (raw, primary, payload, encoded_receipt)
        names = ('raw', 'primary', 'observations_r33', 'receipt_json')
        checksums['receipt_json'] = _source_checksums(encoded_receipt)
        descriptors = tuple((name, len(blob), *checksums[name])
                            for name, blob in zip(names, blobs))
        if raw_encoding == 'zlib':
            stored_raw = bytes(verified_stream) if verified_stream is not None else zlib.compress(raw, 1)
        else:
            stored_raw = raw
        stored = (stored_raw,) + blobs[1:]
        encodings = ((names[0], raw_encoding, len(stored_raw)),) + tuple(
            (name, 'identity', len(blob)) for name, blob in zip(names[1:], blobs[1:]))
        result = object.__new__(cls)
        for key, value in dict(components=tuple(zip(names, stored)),
                descriptors=descriptors, identity_items=tuple(sorted(frame_identity.items())),
                measurement_count=header['measurement_count'],
                preview_count=header['preview_count'], encodings=encodings).items():
            object.__setattr__(result, key, value)
        return result


@dataclass(frozen=True)
class FrameResult:
    """Owned immutable complete frame; sources can be prepared in parallel.

    ``create`` retains the original strict API. ``from_source`` consumes an
    immutable SourceSnapshot produced by the same validation code earlier in
    the input worker; it does not trust or skip checks based on receipt hashes.
    Measurement/preview remain independent little-endian float64 Nx3 arrays.
    """
    components: tuple
    descriptors: tuple
    identity_items: tuple
    charge_bytes: int
    encodings: tuple = ()

    @classmethod
    def create(cls, *, raw, primary, frame, measurement_xyz, preview_xyz,
               receipt, metadata):
        return cls.from_source(SourceSnapshot.create(raw=raw, primary=primary,
            frame=frame, receipt=receipt), measurement_xyz=measurement_xyz,
            preview_xyz=preview_xyz, metadata=metadata)

    @classmethod
    def from_source(cls, source, *, measurement_xyz, preview_xyz, metadata):
        if not isinstance(source, SourceSnapshot):
            raise TypeError('requires SourceSnapshot.create validated sources')
        frame_identity = dict(source.identity_items)
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
            if len(values) > getattr(source, mode + '_count'):
                raise ValueError(mode + ' XYZ exceeds PL accepted observations')
            xyz_bytes.append(values.tobytes(order='C'))
        encoded_metadata = _json_bytes(metadata)
        if len(encoded_metadata) > MAX_JSON_BYTES:
            raise ValueError('R33 receipt/metadata capacity')
        components = source.components[:3] + tuple(zip(COMPONENTS[3:5], xyz_bytes)) + (
            source.components[3], ('metadata_json', encoded_metadata))
        descriptors = source.descriptors[:3] + tuple(
            (name, len(blob), _digest(blob), _crc(blob))
            for name, blob in zip(COMPONENTS[3:5], xyz_bytes)) + (
                source.descriptors[3], ('metadata_json', len(encoded_metadata),
                                      _digest(encoded_metadata), _crc(encoded_metadata)))
        encodings = source.encodings[:3] + tuple(
            (name, 'identity', len(blob)) for name, blob in zip(COMPONENTS[3:5], xyz_bytes)) + (
                source.encodings[3], ('metadata_json', 'identity', len(encoded_metadata)))
        template = _index_row(10**20-1, 10**20-1, frame_identity, descriptors,
                              data_offset=10**20-1, encodings=encodings)
        charge = sum(len(blob) for _, blob in components) + len(_json_bytes(template)) + 256
        return cls(components, descriptors, source.identity_items, charge, encodings)

    @property
    def frame_id(self):
        return dict(self.identity_items)['frame_id']


def _index_row(sequence, accepted_ns, frame_identity, descriptors, data_offset,
               encodings=None):
    """V2 row: logical size/SHA/CRC plus storage encoding and stored length."""
    if encodings is None:
        encodings = tuple((name, 'identity', size) for name, size, _, _ in descriptors)
    parts = []
    for (name, size, sha, crc), (stored_name, encoding, stored) in zip(descriptors, encodings):
        if stored_name != name or encoding not in ENCODINGS:
            raise ValueError('R38 component encoding descriptor')
        parts.append(dict(name=name, offset=data_offset, bytes=size,
                          sha256=sha, crc32=crc, encoding=encoding, stored_bytes=stored))
        data_offset += stored
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


def _keep_size_preallocate(fd, offset, length):
    """Reserve filesystem extents without changing V1 logical journal size.

    Linux KEEP_SIZE is essential: ordinary posix_fallocate grows the file and
    leaves an ambiguous zero tail after a crash. Unsupported filesystems return
    False; real IO/capacity errors remain fatal and cannot create an ACK.
    """
    if os.name != 'posix' or not hasattr(os, 'uname') or os.uname().sysname != 'Linux':
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, 'fallocate64', None)
    if function is None and ctypes.sizeof(ctypes.c_long) == 8:
        function = getattr(libc, 'fallocate', None)
    if function is None:
        return False
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]
    function.restype = ctypes.c_int
    if function(fd, 1, offset, length) == 0:  # FALLOC_FL_KEEP_SIZE
        return True
    error = ctypes.get_errno()
    if error in (errno.ENOSYS, errno.EOPNOTSUPP, errno.EINVAL):
        return False
    raise OSError(error, os.strerror(error))


class AsyncWriter:
    """Single storage owner; pending limits include active and queued frames.

    ``try_submit`` returns None on capacity pressure without taking ownership;
    ``submit`` waits for capacity with a finite timeout. Both return an accepted
    sequence, not a durability acknowledgement.
    Read ``commits`` or ``status()`` for live post-fsync acknowledgements.
    ``drain``/``close`` wait with a finite timeout. A stuck kernel fsync cannot
    be forcefully made durable: timeout leaves the worker and original pending
    bytes owned and reports the unacknowledged range explicitly.
    """
    def __init__(self, output, *, capacity_frames=DEFAULT_FRAMES, capacity_bytes=MAX_BYTES,
                 group=8, max_delay_s=.180, fsync=None, preallocate_bytes=0):
        if type(capacity_frames) is not int or not 1 <= capacity_frames <= MAX_FRAMES:
            raise ValueError('R33 writer frame budget')
        if type(capacity_bytes) is not int or not 1 <= capacity_bytes <= MAX_BYTES:
            raise ValueError('R33 writer byte budget')
        if type(group) is not int or not 1 <= group <= min(capacity_frames, MAX_TRANSACTION_FRAMES):
            raise ValueError('R33 writer group')
        if not 0 <= max_delay_s <= 5:
            raise ValueError('R33 writer delay')
        if type(preallocate_bytes) is not int or not 0 <= preallocate_bytes <= MAX_BYTES:
            raise ValueError('R34 preallocation extent budget')
        self.preallocate_bytes = preallocate_bytes
        self._preallocated_end = 0
        self._preallocation_supported = None
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
                _write_all(f, _json_bytes(dict(format=FORMAT, version=VERSION,
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

    def _validate_submission(self, result):
        if not isinstance(result, FrameResult):
            raise TypeError('submit requires FrameResult.create snapshot')
        if result.charge_bytes > self.capacity_bytes:
            raise BufferError('R33 frame exceeds writer byte budget; not accepted')

    def _try_submit_locked(self, result):
        self._raise_error()
        if self._closing:
            raise StorageError('R33 writer closing; not accepted')
        if result.identity_items in self._identities:
            raise ValueError('duplicate R33 frame identity; not accepted')
        if (len(self._owned) >= self.capacity_frames or
                self._pending_bytes + result.charge_bytes > self.capacity_bytes):
            return None
        seq = self._next_sequence
        item = (seq, time.perf_counter_ns(), result)
        self._queue.append(item)
        self._owned[seq] = item
        self._identities.add(result.identity_items)
        self._next_sequence += 1
        self._pending_bytes += result.charge_bytes
        self._peak_frames = max(self._peak_frames, len(self._owned))
        self._peak_bytes = max(self._peak_bytes, self._pending_bytes)
        self._cv.notify_all()
        return seq

    def try_submit(self, result):
        """Atomically accept an immutable result, or return None if full.

        A capacity refusal consumes no sequence and retains no result reference.
        The caller still owns that result and may retry it unchanged. Accepted
        results remain owned through the transaction's fsync and live commit.
        Invalid, duplicate, closed and failed submissions remain errors even
        when the writer is full; they are never reported as capacity pressure.
        """
        self._validate_submission(result)
        with self._cv:
            return self._try_submit_locked(result)

    def submit(self, result, *, timeout_s=5.0):
        """Wait at most timeout_s for the same atomic admission as try_submit."""
        self._validate_submission(result)
        if not math.isfinite(timeout_s) or timeout_s < 0:
            raise ValueError('submit timeout must be finite and nonnegative')
        deadline = time.monotonic() + timeout_s
        waited = False
        with self._cv:
            while True:
                if waited and time.monotonic() >= deadline:
                    raise BufferError('R33 writer backpressure timeout; not accepted')
                seq = self._try_submit_locked(result)
                if seq is not None:
                    return seq
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BufferError('R33 writer backpressure timeout; not accepted')
                self._cv.wait(remaining)
                waited = True

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
            directory_fsynced=self.directory_fsynced,
            preallocate_bytes=self.preallocate_bytes,
            preallocated_end=self._preallocated_end,
            preallocation_supported=self._preallocation_supported)

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
                                   result.descriptors, offset, result.encodings or None))
            offset += sum(len(blob) for _, blob in result.components)
        index = _json_bytes(dict(format=FORMAT, transaction=txn, frames=rows))
        if len(index) > MAX_INDEX_BYTES or offset > self.capacity_bytes:
            raise ValueError('R33 transaction capacity')
        charged = sum(item[2].charge_bytes for item in batch)
        if HEADER.size + offset + len(index) + TRAILER.size > charged:
            raise ValueError('R33 transaction exceeded reserved byte accounting')
        header = HEADER.pack(MAGIC, VERSION, HEADER.size, txn, batch[0][0], len(batch),
                             offset, len(index), 0)
        digest = hashlib.sha256()
        checksum = 0
        physical = self._stream.tell()
        preallocation_start_ns = time.perf_counter_ns()
        total = HEADER.size + offset + len(index) + TRAILER.size
        if (self.preallocate_bytes and self._preallocation_supported is not False
                and physical + total > self._preallocated_end):
            end = ((physical + total + self.preallocate_bytes - 1) //
                   self.preallocate_bytes) * self.preallocate_bytes
            supported = _keep_size_preallocate(self._stream.fileno(),
                self._preallocated_end, end - self._preallocated_end)
            self._preallocation_supported = supported
            if supported:
                self._preallocated_end = end
        preallocation_s = (time.perf_counter_ns() - preallocation_start_ns) / 1e9
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
            fsync_s=(persisted_ns-sync_start_ns)/1e9, fsync_calls=1,
            preallocation_s=preallocation_s, preallocated_end=self._preallocated_end)

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


def _decode_component(stored, encoding, size):
    if encoding == 'identity':
        return stored
    if encoding != 'zlib':
        raise ValueError('R38 component encoding')
    decoder = zlib.decompressobj()
    blob = decoder.decompress(stored, size + 1)
    if len(blob) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError('R38 encoded component length/stream mismatch')
    return blob


def _validate_frame(stream, data_start, data_bytes, row, expected_offset, *,
                    strict_protocol, xyz_digests, journal_format=FORMAT_V1):
    if set(row) != {'sequence', 'accepted_ns', 'identity', 'components'}:
        raise ValueError('R33 frame index fields')
    if tuple(part.get('name') for part in row['components']) != COMPONENTS:
        raise ValueError('R33 frame component completeness/order')
    blobs = {}
    for part in row['components']:
        fields = {'name', 'offset', 'bytes', 'sha256', 'crc32'}
        if journal_format != FORMAT_V1:
            fields |= {'encoding', 'stored_bytes'}
        if set(part) != fields:
            raise ValueError('R33 component descriptor fields')
        size = part['bytes']
        stored = part.get('stored_bytes', size)
        encoding = part.get('encoding', 'identity')
        if (type(size) is not int or size < 0 or size > MAX_BYTES or
                type(stored) is not int or stored < 0 or stored > MAX_BYTES or
                (encoding == 'identity' and stored != size) or
                part['offset'] != expected_offset or expected_offset+stored > data_bytes):
            raise ValueError('R33 component bounds/continuity')
        if part['name'] in ('receipt_json', 'metadata_json') and size > MAX_JSON_BYTES:
            raise ValueError('R33 archived JSON capacity')
        stream.seek(data_start + expected_offset)
        blob = _decode_component(_read_exact(stream, stored), encoding, size)
        if _digest(blob) != part['sha256'] or _crc(blob) != part['crc32']:
            raise ValueError('R33 component SHA/CRC mismatch')
        blobs[part['name']] = blob
        expected_offset += stored
    if len(blobs['raw']) != 752*480:
        raise ValueError('R33 archived Raw geometry')
    frame_identity, header = _payload_identity(blobs['observations_r33'])
    if frame_identity != row['identity']:
        raise ValueError('R33 archived frame identity mismatch')
    receipt = _json_load(blobs['receipt_json'])
    metadata = _json_load(blobs['metadata_json'])
    if metadata.get('frame_id') != frame_identity['frame_id']:
        raise ValueError('R33 archived metadata identity mismatch')
    algorithm_fp=_archive_fingerprint(header)
    if receipt.get('storage_contract') != journal_format or receipt.get('algorithm_fingerprint') != algorithm_fp:
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
        frame, _ = _decode_archived_primary(blobs['primary'], frame_identity,
                                            algorithm_fp, header)
        if frame.payload != blobs['observations_r33']:
            raise ValueError('R33 strict primary/payload mismatch')
        if hasattr(frame, 'geometry_records'):
            binding=receipt.get('geometry_binding',{})
            if binding.get('epoch')!=frame.geometry_epoch or binding.get('sha256')!=frame.geometry_sha256:
                raise ValueError('R35 archived geometry configuration binding mismatch')
            parameters=binding.get('parameters')
            if not isinstance(parameters,list) or len(parameters)!=36:
                raise ValueError('R35 archived geometry parameter count')
            try: packed=struct.pack('<36q',*parameters)
            except (struct.error,TypeError) as e:raise ValueError('R35 archived geometry parameter type') from e
            if hashlib.sha256(packed).hexdigest()!=binding['sha256']:
                raise ValueError('R35 archived geometry parameter digest mismatch')
            if not isinstance(binding.get('calibration_sha256'),str) or len(binding['calibration_sha256'])!=64 or not math.isfinite(binding.get('angle_rad',float('nan'))):
                raise ValueError('R35 archived geometry calibration/angle missing')
            points=frame.geometry_records
            flags=points[:,7]
            axis=points[:,4:7].astype('<u4').view('<i4').astype('<f8')/65536.
            for mode,bit in [('measurement',1),('preview',2)]:
                selected=((flags&bit)!=0)&((flags&256)!=0)
                if axis[selected].tobytes()!=blobs[mode+'_xyz']:
                    raise ValueError('R35 archived XYZ differs from actual PL output')
    return expected_offset, counts


def readback(output, *, strict_protocol=True, recover_incomplete_tail=False):
    """Stream-check complete transactions without loading a whole run in RAM.

    Strict mode selects a strict decoder by the explicit primary envelope
    version; unknown envelopes and failed validations are never retried.
    Recovery may ignore only a physically incomplete final transaction. Complete
    corrupt transactions are rejected even in recovery mode. No live fsync ACK
    is inferred from file bytes.
    """
    output = Path(output)
    descriptor = _json_load((output/'FORMAT.json').read_bytes())
    journal_format = descriptor.get('format')
    if journal_format not in FORMATS or descriptor.get('version') != FORMATS[journal_format][1]:
        raise ValueError('not an R33/R38 full-frame journal')
    journal_magic, journal_version = FORMATS[journal_format]
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
            if (magic, version, hbytes, reserved) != (journal_magic, journal_version, HEADER.size, 0):
                raise ValueError('R33 journal magic/version/reserved')
            if (txn != transactions+1 or first != frames+1 or not 1 <= count <= MAX_TRANSACTION_FRAMES
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
                    index['format'] != journal_format or index['transaction'] != txn or
                    len(index['frames']) != count):
                raise ValueError('R33 authoritative index mismatch')
            offset = 0
            for row in index['frames']:
                if row['sequence'] != frames+1:
                    raise ValueError('R33 frame sequence mismatch')
                offset, counts = _validate_frame(stream, data_start, data_bytes, row, offset,
                    strict_protocol=strict_protocol, xyz_digests=xyz_digests,
                    journal_format=journal_format)
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
    return dict(format=journal_format, passed=True, frames=frames, transactions=transactions,
        measurement_points=measurement, preview_points=preview, frame_summaries=summaries,
        valid_bytes=position, file_bytes=size, incomplete_tail=incomplete,
        strict_protocol=bool(strict_protocol), original_live_durability_ack_inferred=False,
        measurement_xyz_sha256=xyz_digests['measurement'].hexdigest(),
        preview_xyz_sha256=xyz_digests['preview'].hexdigest())


# Public name used by the unified R33 replay entrypoint.
read_run = readback
