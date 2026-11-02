"""Bounded FIFO group commit with lossless offsets into one payload file.

The retained writer supplies validation, snapshotting, ordering and receipts.
This sink syncs payload first, events next, and frame index plus commit marker
last. A visible marker above a failed receipt's written watermark is NOT a
confirmed commit. This is not a power-loss atomic filesystem transaction.
"""
from __future__ import annotations

from collections import deque
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
import queue
import time
from typing import Any, Callable, Mapping

import numpy as np

from .stream_writer import (BoundedAsyncWriter, WriteItem, WriterReceipt,
                            _json_line, _timeout)


class ChunkedAsyncWriter(BoundedAsyncWriter):
    """Commit at most ``group_size`` FIFO items after a finite collection delay.

    Capacity includes in-progress groups. Custom sinks receive the unchanged
    WriteItem contract, once per item; the group is accounted only after every
    callback succeeds. Default mode exclusively creates payloads.bin and both
    indexes, without per-frame directories or per-frame fsync operations.
    """

    def __init__(self, output_dir: str | os.PathLike[str], capacity: int = 16,
                 sink: Callable[[Path, WriteItem], None] | None = None, *,
                 group_size: int = 4, max_delay_s: float = 0.05) -> None:
        if type(group_size) is not int or group_size <= 0:
            raise ValueError('group_size must be a positive integer')
        self._group_size = group_size
        self._max_delay_s = _timeout(max_delay_s)
        self._custom_sink = sink
        self._flush_target = 0
        self._pending_ids: dict[int, int] = {}
        # Diagnostics are bounded independently of measurement storage.
        self._batch_stats: deque[dict[str, Any]] = deque(maxlen=1024)
        self._batch_count = 0
        self._streams = None
        super().__init__(output_dir, capacity=capacity, sink=sink)

    @property
    def pending_request_ids(self) -> tuple[int, ...]:
        with self._condition:
            return tuple(self._pending_ids[key] for key in sorted(self._pending_ids))

    @property
    def batch_stats(self) -> tuple[Mapping[str, Any], ...]:
        """Copies of the last 1024 successfully committed batch timing records."""
        with self._condition:
            return tuple(dict(stats) for stats in self._batch_stats)

    @property
    def batch_timings(self) -> tuple[Mapping[str, Any], ...]:
        return self.batch_stats

    @property
    def batch_count(self) -> int:
        with self._condition:
            return self._batch_count

    def _enqueue(self, kind: str, payload: Mapping[str, Any]) -> int:
        # Condition's RLock keeps the base acceptance ticket and mirror update
        # indivisible relative to successful batch accounting.
        with self._condition:
            sequence = super()._enqueue(kind, payload)
            if kind == 'frame':
                self._pending_ids[sequence] = payload['request_id']
            return sequence

    def flush(self, timeout_s: float = 5.0) -> WriterReceipt:
        _timeout(timeout_s)
        with self._condition:
            self._flush_target = max(self._flush_target, self._accepted)
            self._condition.notify_all()
        return super().flush(timeout_s)

    def _receipt_locked(self) -> WriterReceipt:
        receipt = super()._receipt_locked()
        # A close deadline also bounds stream closure; an unjoined closing
        # worker cannot be described as a completed durable shutdown.
        if self._closing.is_set() and self._thread.is_alive() and not receipt.worker_joined:
            return replace(receipt, durable=False, worker_unconfirmed=True)
        return receipt

    def _collect(self, first: WriteItem) -> list[WriteItem]:
        group = [first]
        deadline = time.monotonic() + self._max_delay_s
        while len(group) < self._group_size:
            try:
                group.append(self._queue.get_nowait())
                continue
            except queue.Empty:
                pass
            with self._condition:
                if self._closing.is_set() or self._flush_target > self._written:
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                group.append(self._queue.get(timeout=min(remaining, 0.01)))
            except queue.Empty:
                continue
        return group

    def _run(self) -> None:
        group = []
        try:
            with ExitStack() as stack:
                if self._custom_sink is None:
                    # The directory itself was exclusively created by the base.
                    opener = getattr(self, '_open_storage', None)
                    self._streams = (opener(stack) if opener is not None else
                                     tuple(stack.enter_context((self.output_dir / name).open('xb'))
                                           for name in ('payloads.bin', 'frames.jsonl', 'events.jsonl')))
                    prepare=getattr(self,'_prepare_storage',None)
                    if prepare is not None:prepare()
                while True:
                    try:
                        first = self._queue.get(timeout=0.01)
                    except queue.Empty:
                        with self._condition:
                            if self._closing.is_set() and self._queue.empty():
                                return
                        continue
                    self._persistence_phase([first], 'collect')
                    group = self._collect(first)
                    self._persistence_phase(group, 'worker')
                    started, cpu_started = time.perf_counter(), time.thread_time()
                    try:
                        if self._custom_sink is None:
                            stats = self._persist_group(group)
                        else:
                            for item in group:
                                self._custom_sink(self.output_dir, item)
                            stats = dict(payload_fsync_s=0.0, index_fsync_s=0.0, payload_bytes=0)
                        elapsed = time.perf_counter() - started
                        stats.update(first_sequence=group[0].sequence, last_sequence=group[-1].sequence,
                                     item_count=len(group), frame_count=sum(item.kind == 'frame' for item in group),
                                     event_count=sum(item.kind == 'event' for item in group),
                                     io_s=elapsed, batch_s=elapsed,
                                     thread_cpu_s=time.thread_time() - cpu_started)
                        with self._condition:
                            observed = [self._persistence_pending[item.sequence] for item in group]
                            now = time.perf_counter()
                            stats.update(
                                accepted_to_worker_max_s=max(
                                    row.get('accepted_to_worker_s', 0.) for row in observed),
                                accepted_to_durable_max_s=max(
                                    now-row['accepted_perf_s'] for row in observed))
                            self._persistence_complete(group)
                            self._written += len(group)
                            for item in group:
                                self._pending_ids.pop(item.sequence, None)
                            self._batch_count += 1
                            self._batch_stats.append(stats)
                            self._condition.notify_all()
                    except BaseException as exc:
                        error = RuntimeError(f'batch {group[0].sequence}..{group[-1].sequence} failed: '
                                             f'{type(exc).__name__}: {exc}')
                        self._mark_failed(group[0], error)
                        return
                    finally:
                        for item in group:
                            self._queue.task_done()
        except BaseException as exc:
            # Startup/file-close failures must also wake waiters and invalidate
            # durable receipts, including a session with no submitted item.
            item = group[0] if group else WriteItem(0, 'event', {})
            self._mark_failed(item, exc)
        finally:
            self._streams = None
            with self._condition:
                self._worker_stopped = True
                self._condition.notify_all()

    @staticmethod
    def _sync(stream) -> float:
        started = time.perf_counter()
        stream.flush()
        os.fsync(stream.fileno())
        return time.perf_counter() - started

    def _persist_group(self, group: list[WriteItem]) -> dict[str, Any]:
        self._persistence_phase(group, 'build_write')
        payload, frames, events = self._streams
        payload_start = payload.tell()
        frame_lines, event_lines = [], []
        for item in group:
            common = dict(batch_first_sequence=group[0].sequence,
                          batch_last_sequence=group[-1].sequence)
            if item.kind == 'event':
                event_lines.append(_json_line(dict(sequence=item.sequence, event=item.payload, **common)))
                continue
            record = item.payload
            raw_offset = payload.tell()
            payload.write(record['raw'])
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
                            storage_format='scan3d_chunk_v1', request_id=record['request_id'],
                            capture_epoch=record['capture_epoch'], raw_sha256=record['raw_sha256'],
                            profile_sha256=hashlib.sha256(record['profile_payload']).hexdigest(),
                            raw_bytes=len(record['raw']), profile_bytes=len(record['profile_payload']),
                            raw_file='payloads.bin', profile_file='payloads.bin',
                            raw_offset=raw_offset, profile_offset=profile_offset,
                            point_files=point_files, summary=record['summary'], motion=record['motion'],
                            exposure=record['exposure'], **common)
            frame_lines.append(_json_line(metadata))

        # Never publish indexes before the complete batch's payload sync.
        self._persistence_phase(group, 'payload_sync')
        payload_fsync_s = self._sync(payload)
        self._persistence_phase(group, 'index_write_sync')
        index_fsync_s = 0.0
        if event_lines:
            events.write(b''.join(event_lines))
            index_fsync_s += self._sync(events)
        commit = dict(kind='batch_commit', first_sequence=group[0].sequence,
                      last_sequence=group[-1].sequence, item_count=len(group),
                      frame_count=len(frame_lines), event_count=len(event_lines), payload_end=payload.tell())
        frames.write(b''.join(frame_lines) + _json_line(commit))
        index_fsync_s += self._sync(frames)
        trace=getattr(self,'performance_trace',None)
        if trace is not None:trace.durable_group(group,time.perf_counter())
        return dict(payload_fsync_s=payload_fsync_s, index_fsync_s=index_fsync_s,
                    payload_bytes=payload.tell() - payload_start)


__all__ = ['ChunkedAsyncWriter']
