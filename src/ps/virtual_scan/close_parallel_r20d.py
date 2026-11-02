"""Overlap writer drain with display close under one finite deadline.

Only writer.close runs on a background thread. Display/Pipe close remains on
the caller, with no process termination, lock operation or automatic retry.
"""
from __future__ import annotations

import math
import threading
import time


class ParallelCloseError(RuntimeError):
    """Both close outcomes, including references for an unconfirmed thread.

    writer_close_alive is latched at the deadline, never a later success claim.
    When it is true, do not concurrently retry writer.close. The retained daemon
    and state keep any later receipt/error inspectable; they do not confirm a
    durable shutdown or authorize hardware-lock release.
    """

    def __init__(self, *, writer_receipt, display_receipt, writer_error, display_error,
                 timings, writer_close_alive, writer_close_thread, writer_close_state):
        details = []
        if writer_close_alive:
            details.append('writer close thread remains unconfirmed')
        if writer_error is not None:
            details.append(f'writer: {type(writer_error).__name__}: {writer_error}')
        if display_error is not None:
            details.append(f'display: {type(display_error).__name__}: {display_error}')
        super().__init__('parallel close failed; '+'; '.join(details))
        self.writer_receipt = writer_receipt
        self.display_receipt = display_receipt
        self.writer_error = writer_error
        self.display_error = display_error
        self.timings = timings
        self.writer_close_alive = writer_close_alive
        self.writer_close_thread = writer_close_thread
        self.writer_close_state = writer_close_state


class ParallelCloseTimeout(ParallelCloseError):
    """The finite join deadline expired while writer.close was still running."""


def _writer_confirmed(receipt):
    return bool(receipt is not None and getattr(receipt, 'durable', False) is True
                and getattr(receipt, 'worker_joined', False) is True
                and getattr(receipt, 'pending', None) == 0
                and getattr(receipt, 'accepted', None) == getattr(receipt, 'written', None)
                and getattr(receipt, 'error', None) is None)


def _display_confirmed(receipt):
    return bool(isinstance(receipt, dict) and receipt.get('owner_closed') is True
                and receipt.get('worker_joined') is True and not receipt.get('error'))


def finish_parallel(writer, display, timeout_s=30):
    """Return (writer receipt, display receipt, timings) only if both confirm.

    Each known close method receives only the remaining common budget. Their
    internal implementations must honor that budget; Python cannot preempt the
    caller's display.close. A writer close that outlives the join deadline raises
    ParallelCloseTimeout with writer_close_alive=True and a retained thread.
    """
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise TypeError('timeout_s must be a finite nonnegative number')
    timeout_s = float(timeout_s)
    if not math.isfinite(timeout_s) or timeout_s < 0:
        raise ValueError('timeout_s must be a finite nonnegative number')
    started = time.perf_counter()
    deadline = time.monotonic()+timeout_s
    state = dict(receipt=None, error=None, started_perf_s=None, done_perf_s=None)
    state_lock = threading.Lock()

    def remaining():
        return max(0.0, deadline-time.monotonic())

    def close_writer():
        with state_lock:
            state['started_perf_s'] = time.perf_counter()
        receipt = error = None
        try:
            receipt = writer.close(timeout_s=remaining())
            if not _writer_confirmed(receipt):
                error = RuntimeError('writer cleanup unconfirmed')
        except BaseException as exc:
            error = exc
            failure_receipt = getattr(exc, 'receipt', None)
            if failure_receipt is not None:
                receipt = failure_receipt
        finally:
            with state_lock:
                state.update(receipt=receipt, error=error, done_perf_s=time.perf_counter())

    worker = threading.Thread(target=close_writer, name='r20d-writer-close', daemon=True)
    thread_started = False
    try:
        worker.start()
        thread_started = True
    except BaseException as exc:
        state.update(error=exc, done_perf_s=time.perf_counter())
    display_receipt = display_error = None
    display_started = time.perf_counter()
    try:
        display_receipt = display.close(timeout_s=remaining())
        if not _display_confirmed(display_receipt):
            display_error = RuntimeError('display cleanup unconfirmed')
    except BaseException as exc:
        display_error = exc
        display_receipt = getattr(exc, 'receipt', None)
        if display_receipt is None:
            display_receipt = getattr(display, '_final_result', None)
    display_done = time.perf_counter()
    join_started = time.perf_counter()
    if thread_started:
        worker.join(remaining())
    writer_alive = worker.is_alive()
    with state_lock:
        snapshot = dict(state)
    ended = time.perf_counter()
    writer_duration = None
    if snapshot['started_perf_s'] is not None and snapshot['done_perf_s'] is not None:
        writer_duration = snapshot['done_perf_s']-snapshot['started_perf_s']
    timings = dict(parallel_close_started_perf_s=started, parallel_close_elapsed_s=ended-started,
                   writer_close_started_perf_s=snapshot['started_perf_s'],
                   writer_close_done_perf_s=snapshot['done_perf_s'], writer_close_s=writer_duration,
                   display_close_started_perf_s=display_started, display_close_done_perf_s=display_done,
                   display_close_s=display_done-display_started, writer_join_s=ended-join_started,
                   timeout_s=timeout_s)
    if writer_alive or snapshot['error'] is not None or display_error is not None:
        kind = ParallelCloseTimeout if writer_alive else ParallelCloseError
        raise kind(writer_receipt=snapshot['receipt'], display_receipt=display_receipt,
                   writer_error=snapshot['error'], display_error=display_error, timings=timings,
                   writer_close_alive=writer_alive, writer_close_thread=worker, writer_close_state=state)
    return snapshot['receipt'], display_receipt, timings


__all__ = ['finish_parallel', 'ParallelCloseError', 'ParallelCloseTimeout']
