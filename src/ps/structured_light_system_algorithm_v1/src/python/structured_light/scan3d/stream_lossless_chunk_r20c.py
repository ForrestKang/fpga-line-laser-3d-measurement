"""Explicit v2 chunk storage: level-1 zlib Raw, unchanged packet/XYZ bytes.

Acceptance, frozen snapshots, capacity, commit watermark and worker lifecycle
remain CapacityWaitWriter's. Compression executes only in the existing worker.
"""
from __future__ import annotations

import hashlib
import io
import time
import zlib

import numpy as np

from .stream_wait_r20 import CapacityWaitWriter
from .stream_writer import _json_line


class LosslessChunkWriter(CapacityWaitWriter):
    """Preserve every native 752x480 Raw byte with an explicit new schema."""

    def _snapshot_record(self, record):
        snapshot = super()._snapshot_record(record)
        if len(snapshot['raw']) != 752*480:
            raise ValueError('lossless Raw storage requires native 752x480 bytes')
        for key in ('points_camera_mm', 'points_axis_mm'):
            if snapshot[key] is not None and len(snapshot[key]) > 480:
                raise ValueError('a native frame cannot contain more than 480 XYZ points')
        return snapshot

    def _persist_group(self, group):
        payload, frames, events = self._streams
        payload_start = payload.tell()
        frame_lines, event_lines = [], []
        raw_compress_s = raw_compress_thread_cpu_s = 0.0
        raw_input_bytes = raw_stored_bytes = 0
        for item in group:
            common = dict(batch_first_sequence=group[0].sequence,
                          batch_last_sequence=group[-1].sequence)
            if item.kind == 'event':
                event_lines.append(_json_line(dict(sequence=item.sequence, event=item.payload, **common)))
                continue
            record = item.payload
            started, cpu_started = time.perf_counter(), time.thread_time()
            compressed = zlib.compress(record['raw'], level=1)
            raw_compress_s += time.perf_counter()-started
            raw_compress_thread_cpu_s += time.thread_time()-cpu_started
            raw_input_bytes += len(record['raw'])
            raw_stored_bytes += len(compressed)
            raw_offset = payload.tell()
            payload.write(compressed)
            profile_offset = payload.tell()
            payload.write(record['profile_payload'])
            point_files = {}
            for key in ('points_camera_mm', 'points_axis_mm'):
                points = record[key]
                point_files[key] = None
                if points is not None:
                    buffer = io.BytesIO()
                    np.save(buffer, points, allow_pickle=False)
                    data = buffer.getvalue()
                    point_files[key] = dict(file='payloads.bin', offset=payload.tell(), bytes=len(data),
                                            sha256=hashlib.sha256(data).hexdigest(), format='npy')
                    payload.write(data)
            metadata = dict(kind='frame', sequence=item.sequence,
                            storage_format='scan3d_chunk_zlib_v2', request_id=record['request_id'],
                            capture_epoch=record['capture_epoch'], raw_sha256=record['raw_sha256'],
                            raw_codec='zlib', raw_stored_bytes=len(compressed),
                            raw_stored_sha256=hashlib.sha256(compressed).hexdigest(),
                            profile_sha256=hashlib.sha256(record['profile_payload']).hexdigest(),
                            raw_bytes=len(record['raw']), profile_bytes=len(record['profile_payload']),
                            raw_file='payloads.bin', profile_file='payloads.bin',
                            raw_offset=raw_offset, profile_offset=profile_offset,
                            point_files=point_files, summary=record['summary'], motion=record['motion'],
                            exposure=record['exposure'], **common)
            frame_lines.append(_json_line(metadata))

        # Same durable order as v1; a failed index fsync can leave visible bytes
        # above the unchanged confirmed-written watermark, never a durable claim.
        payload_fsync_s = self._sync(payload)
        index_fsync_s = 0.0
        if event_lines:
            events.write(b''.join(event_lines))
            index_fsync_s += self._sync(events)
        commit = dict(kind='batch_commit', first_sequence=group[0].sequence,
                      last_sequence=group[-1].sequence, item_count=len(group),
                      frame_count=len(frame_lines), event_count=len(event_lines), payload_end=payload.tell())
        frames.write(b''.join(frame_lines) + _json_line(commit))
        index_fsync_s += self._sync(frames)
        sync_done_perf_s = time.perf_counter()
        # This precedes the inherited worker's _written update/notification.
        return dict(payload_fsync_s=payload_fsync_s, index_fsync_s=index_fsync_s,
                    payload_bytes=payload.tell()-payload_start,
                    raw_compress_s=raw_compress_s, raw_compress_thread_cpu_s=raw_compress_thread_cpu_s,
                    raw_input_bytes=raw_input_bytes, raw_stored_bytes=raw_stored_bytes,
                    sync_done_perf_s=sync_done_perf_s)


__all__ = ['LosslessChunkWriter']
