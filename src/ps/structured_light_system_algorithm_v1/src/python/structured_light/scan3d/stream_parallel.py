"""Bounded daemon workers for camera-only capture and ordered processing.

The submitter fixes the generation token before admission. Only the capture
worker binds it; processing receives immutable camera payloads and no MMIO.
An unjoined daemon is explicit evidence to quarantine the hardware allocation.
"""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
import queue
import threading
import time
from typing import Any, Callable


@dataclass
class FrameTicket:
    index: int
    generation: int
    metadata: dict
    admitted_at: float = field(default_factory=time.monotonic)
    captured: Future = field(default_factory=Future)
    processed: Future = field(default_factory=Future)


class BoundedParallelFrames:
    """One capture owner, one FIFO processor; bound all unfinished tickets.

    A full queue is never overwritten. ``submit`` raises ``BufferError``;
    callers may explicitly wait for an older ticket before attempting submit.
    ``close`` stops admission, drains accepted tickets, and has a finite join.
    """
    def __init__(self, capture: Callable, process: Callable, gate: Any, *, capacity: int = 4):
        if type(capacity) is not int or not 2 <= capacity <= 4:
            raise ValueError("capacity must be in 2..4")
        self.capture, self.process, self.gate = capture, process, gate
        self.capacity = capacity
        self.tickets: list[FrameTicket] = []
        self.admitted_count = 0
        self._captures = queue.Queue()
        self._processing = queue.Queue()
        self._lock = threading.Lock()
        self._closing = False
        self.peak_pending = 0
        self._capture_thread = threading.Thread(target=self._capture_loop,
                                               name="scan3d-capture", daemon=True)
        self._process_thread = threading.Thread(target=self._process_loop,
                                               name="scan3d-quality", daemon=True)
        self._capture_thread.start()
        self._process_thread.start()

    def submit(self, index: int, metadata: dict) -> FrameTicket:
        with self._lock:
            if self._closing:
                raise RuntimeError("parallel pipeline is closed")
            # Futures own full immutable payloads. Release completed tickets
            # after checking their outcomes so retained frame memory stays
            # bounded by capacity rather than growing with the whole run.
            for ticket in self.tickets:
                if ticket.processed.done():
                    ticket.processed.result()
            self.tickets[:] = [ticket for ticket in self.tickets if not ticket.processed.done()]
            pending = sum(not ticket.processed.done() for ticket in self.tickets)
            if pending >= self.capacity:
                raise BufferError("capture/processing capacity full; frame was not admitted")
            token = self.gate.generation
            self.gate.check(token)
            ticket = FrameTicket(index, token, dict(metadata))
            self.tickets.append(ticket)
            self.admitted_count += 1
            self.peak_pending = max(self.peak_pending, pending + 1)
            self._captures.put_nowait(ticket)
            return ticket

    def _capture_loop(self):
        while True:
            ticket = self._captures.get()
            if ticket is None:
                self._processing.put(None)
                return
            bound = False
            try:
                bind = getattr(self.gate, "bind_worker", None)
                if bind is not None:
                    bind(ticket.generation)
                    bound = True
                self.gate.check(ticket.generation)
                frame = self.capture(ticket)
                self.gate.check(ticket.generation)
                ticket.captured.set_result(frame)
                self._processing.put_nowait(ticket)
            except BaseException as exc:
                ticket.captured.set_exception(exc)
                ticket.processed.set_exception(exc)
            finally:
                if bound:
                    self.gate.unbind_worker()

    def _process_loop(self):
        while True:
            ticket = self._processing.get()
            if ticket is None:
                return
            try:
                ticket.processed.set_result(self.process(ticket, ticket.captured.result()))
            except BaseException as exc:
                ticket.processed.set_exception(exc)

    def drain(self, timeout_s: float):
        deadline = time.monotonic() + timeout_s
        for ticket in self.tickets:
            ticket.processed.result(timeout=max(0.0, deadline - time.monotonic()))

    def close(self, timeout_s: float) -> dict:
        with self._lock:
            if not self._closing:
                self._closing = True
                self._captures.put_nowait(None)
        deadline = time.monotonic() + timeout_s
        self._capture_thread.join(max(0.0, deadline - time.monotonic()))
        self._process_thread.join(max(0.0, deadline - time.monotonic()))
        return {"capture_joined": not self._capture_thread.is_alive(),
                "processing_joined": not self._process_thread.is_alive(),
                "admitted": self.admitted_count, "peak_pending": self.peak_pending,
                "capacity": self.capacity}
