"""Bounded FIFO processing behind a caller-owned hardware capture thread.

Only the caller captures and checks hardware. This worker receives detached
immutable payloads; it never binds a generation token or owns a CMA allocation.
Failed and queued frames remain strongly referenced until explicitly recovered.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import threading
import time
from typing import Any, Callable


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('timeout must be finite and positive')
    return value


@dataclass(frozen=True)
class CapturedWork:
    index: int
    frame: Any
    capture_started: float
    captured_at: float
    capture_timing: dict
    queued_at: float


class BoundedFrameProcessor:
    """One FIFO worker, bounded active + queued frames, no measurement drops.

    A single producer calls ``wait_for_capacity`` *before* hardware capture,
    then ``submit``. A worker failure observed after capture rejects submit;
    the caller must preserve that current frame. ``close`` stops admission and
    drains on success. On failure it retains the failed frame and queued tail.
    Never close downstream resources while ``worker_joined`` is false.
    """
    def __init__(self, process: Callable, *, capacity=3):
        if type(capacity) is not int or not 1 <= capacity <= 64:
            raise ValueError('processing capacity must be in 1..64')
        self.process = process
        self.capacity = capacity
        self._condition = threading.Condition()
        self._queue = deque()
        self._unfinished = {}
        self._closing = False
        self._error = None
        self._accepted = self._completed = self._peak = 0
        self._capacity_wait_s = 0.
        self._records = []
        self._thread = threading.Thread(target=self._run, name='scan3d-processing', daemon=True)
        self._thread.start()

    def _check(self):
        if self._error is not None:
            raise RuntimeError('processing worker failed: '+type(self._error).__name__+': '+str(self._error)) from self._error
        if self._closing:
            raise RuntimeError('processing pipeline is closed')

    def wait_for_capacity(self, timeout_s):
        deadline = time.perf_counter()+_timeout(timeout_s)
        started = time.perf_counter()
        with self._condition:
            try:
                while True:
                    self._check()
                    if len(self._unfinished) < self.capacity:
                        return
                    remaining = deadline-time.perf_counter()
                    if remaining <= 0:
                        raise TimeoutError('processing capacity wait timed out; no capture admitted')
                    self._condition.wait(remaining)
            finally:
                self._capacity_wait_s += time.perf_counter()-started

    def submit(self, index, frame, *, capture_started, captured_at, capture_timing):
        if not isinstance(frame.raw, bytes) or not isinstance(frame.profile_payload, bytes):
            raise TypeError('processing handoff requires detached immutable Raw/Profile bytes')
        with self._condition:
            self._check()
            if len(self._unfinished) >= self.capacity:
                raise BufferError('processing capacity full; captured frame remains caller owned')
            if index in self._unfinished:
                raise ValueError('duplicate processing index')
            item = CapturedWork(index, frame, capture_started, captured_at,
                                dict(capture_timing), time.perf_counter())
            self._unfinished[index] = item
            self._queue.append(item)
            self._accepted += 1
            self._peak = max(self._peak, len(self._unfinished))
            self._condition.notify_all()

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._closing)
                if not self._queue:
                    return
                item = self._queue.popleft()
            started = time.perf_counter()
            try:
                self.process(item)
            except BaseException as exc:
                with self._condition:
                    self._error = exc
                    self._closing = True
                    self._condition.notify_all()
                return
            with self._condition:
                self._records.append(dict(index=item.index, request_id=item.frame.request_id,
                    queue_wait_s=started-item.queued_at,
                    processing_s=time.perf_counter()-started,
                    capture_started_monotonic_s=item.capture_started,
                    captured_monotonic_s=item.captured_at,
                    processing_started_monotonic_s=started,
                    processing_finished_monotonic_s=time.perf_counter()))
                self._unfinished.pop(item.index)
                self._completed += 1
                self._condition.notify_all()

    def drain(self, timeout_s):
        deadline = time.perf_counter()+_timeout(timeout_s)
        with self._condition:
            while self._unfinished and self._error is None:
                remaining = deadline-time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError('processing drain timed out')
                self._condition.wait(remaining)
            if self._error is not None:
                raise RuntimeError('processing worker failed: '+str(self._error)) from self._error

    def close(self, timeout_s):
        _timeout(timeout_s)
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._thread.join(timeout_s)
        return self.summary()

    def unprocessed(self):
        with self._condition:
            if self._thread.is_alive():
                raise RuntimeError('join processing worker before recovering frames')
            return tuple(self._unfinished.values())

    def summary(self):
        with self._condition:
            return dict(capacity=self.capacity, accepted=self._accepted, completed=self._completed,
                pending=len(self._unfinished), peak_pending=self._peak,
                capacity_wait_s=self._capacity_wait_s, worker_joined=not self._thread.is_alive(),
                error=None if self._error is None else type(self._error).__name__+': '+str(self._error),
                measurement_dropped=0, records=list(self._records))
