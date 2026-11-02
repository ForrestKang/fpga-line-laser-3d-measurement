"""Opt-in exact-size DMA allocation reuse behind existing safe buffer cleanup.

No DMA stop/reset rule is changed. The existing transport copies bytes before
BoundedS2MM.cleanup_buffer calls a lease's freebuffer. That call returns the
allocation to this pool, rather than returning its CMA storage to the system.
Only a confirmed runtime close with zero active leases permits physical free.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import threading
import time
from typing import Any, Callable


class DMAPoolError(RuntimeError):
    pass


@dataclass
class _Allocation:
    buffer: Any
    size: int
    active: bool = False
    freed: bool = False


class DMALease:
    """One owner-thread lease; a returned proxy can never become active again.

    Slices return detached bytes, so callers cannot retain an ndarray view of
    CMA storage beyond the lease. The transport already requests bytes slices.
    """
    def __init__(self, pool, entry, number):
        self._pool = pool
        self._entry = entry
        self._number = number
        self._active = True

    def _check(self):
        self._pool._check_owner()
        if not self._active:
            raise DMAPoolError('DMA lease was already returned; use after return refused')
        self._pool._check_available()

    @property
    def nbytes(self):
        self._check()
        return self._entry.size

    @property
    def device_address(self):
        self._check()
        return getattr(self._entry.buffer, 'device_address', None)

    @property
    def physical_address(self):
        self._check()
        return getattr(self._entry.buffer, 'physical_address', None)

    def invalidate(self):
        self._check()
        invalidate = getattr(self._entry.buffer, 'invalidate', None)
        if callable(invalidate):
            try:
                invalidate()
            except BaseException as exc:
                self._pool.quarantine('underlying invalidate failed: '+type(exc).__name__+': '+str(exc))
                raise

    def __getitem__(self, key):
        self._check()
        if isinstance(key, slice):
            return bytes(self._entry.buffer[key])
        if type(key) is int:
            return int(self._entry.buffer[key])
        raise TypeError('DMA lease supports flat integer and slice reads only')

    def freebuffer(self):
        self._check()
        self._entry.active = False
        self._active = False
        del self._pool._active[self._number]
        self._pool._returns += 1


class FiniteDMAPool:
    """Owner-thread-only allocation pool, globally bounded to at most four.

    Exhaustion rejects allocation without eviction. Allocation/cache/free
    failures are sticky: all remaining references are retained and no automatic
    retry is made. Diagnostics never access an underlying buffer.
    """
    def __init__(self, allocate: Callable, *, max_buffers=4):
        if not callable(allocate):
            raise TypeError('allocate must be callable')
        if type(max_buffers) is not int or not 1 <= max_buffers <= 4:
            raise ValueError('max_buffers must be in 1..4')
        self._allocate = allocate
        self.max_buffers = max_buffers
        self._owner = threading.current_thread()
        self._entries = []
        self._active = {}
        self._next_lease = 0
        self._returns = self._reuses = self._peak = 0
        self._allocation_s = self._acquire_s = 0.
        self._failure = None
        self._closed = False
        self._close_attempted = False

    def _check_owner(self):
        if threading.current_thread() is not self._owner:
            raise DMAPoolError('DMA pool and leases require the installing owner thread')

    def _check_available(self):
        if self._failure is not None:
            raise DMAPoolError('DMA pool quarantined: '+self._failure)
        if self._closed or self._close_attempted:
            raise DMAPoolError('DMA pool is closed to allocation and lease access')

    def quarantine(self, reason):
        self._check_owner()
        if self._failure is None:
            self._failure = str(reason)

    def acquire(self, size):
        self._check_owner()
        self._check_available()
        if type(size) is not int or size <= 0:
            raise ValueError('DMA allocation size must be a positive integer')
        started = time.perf_counter()
        try:
            entry = next((item for item in self._entries if item.size == size and not item.active), None)
            if entry is None:
                if len(self._entries) >= self.max_buffers:
                    raise BufferError('DMA pool allocation bound reached; no exact-size idle allocation')
                before = time.perf_counter()
                try:
                    buffer = self._allocate(size)
                    # Retain even an invalid allocation; do not discard unknown
                    # memory ownership by trying a speculative immediate free.
                    entry = _Allocation(buffer, size)
                    self._entries.append(entry)
                    if getattr(buffer, 'nbytes', None) != size:
                        raise DMAPoolError('underlying allocation does not have the exact requested nbytes')
                    if not any(callable(getattr(buffer, name, None)) for name in ('freebuffer', 'free')):
                        raise DMAPoolError('underlying allocation has no explicit free method')
                except BaseException as exc:
                    self.quarantine('underlying allocation failed: '+type(exc).__name__+': '+str(exc))
                    raise
                finally:
                    self._allocation_s += time.perf_counter()-before
            else:
                self._reuses += 1
            self._next_lease += 1
            lease = DMALease(self, entry, self._next_lease)
            entry.active = True
            self._active[self._next_lease] = lease
            self._peak = max(self._peak, len(self._active))
            return lease
        finally:
            self._acquire_s += time.perf_counter()-started

    def close(self, runtime_receipt):
        self._check_owner()
        if self._closed:
            return self.summary()
        if self._failure is not None or self._close_attempted:
            raise DMAPoolError('DMA pool close is quarantined; no automatic retry')
        self._close_attempted = True
        if not isinstance(runtime_receipt, Mapping) or runtime_receipt.get('closed') is not True:
            self.quarantine('runtime close was not confirmed closed')
            raise DMAPoolError(self._failure)
        if self._active:
            self.quarantine('runtime closed with active DMA leases; retain all allocations')
            raise DMAPoolError(self._failure)
        for entry in self._entries:
            try:
                free = getattr(entry.buffer, 'freebuffer', None)
                if not callable(free):
                    free = getattr(entry.buffer, 'free')
                free()
                entry.freed = True
            except BaseException as exc:
                self.quarantine('underlying free failed: '+type(exc).__name__+': '+str(exc))
                raise
        self._closed = True
        return self.summary()

    def summary(self):
        self._check_owner()
        return dict(max_buffers=self.max_buffers, allocations=len(self._entries),
            allocation_sizes=[entry.size for entry in self._entries],
            retained_allocations=sum(not entry.freed for entry in self._entries),
            underlying_freed=sum(entry.freed for entry in self._entries),
            leases_issued=self._next_lease, leases_returned=self._returns,
            active_leases=len(self._active), peak_active_leases=self._peak,
            reuse_count=self._reuses, underlying_allocate_s=self._allocation_s,
            acquire_inclusive_s=self._acquire_s, closed=self._closed,
            quarantined=self._failure is not None, error=self._failure,
            close_order='confirmed runtime close, then physical pool free',
            hardware_lock_may_be_released_before_pool_free=True)


def install_runtime_dma_pool(runtime, *, max_buffers=4):
    """Explicit opt-in; install only on an idle, open runtime owner thread.

    The original runtime close is called once. Its confirmed receipt remains
    authoritative for DMA shutdown. It may release the hardware lock before
    the pool is physically freed; held allocations cannot be reused by a new
    owner during that interval. Unconfirmed close retains the entire pool.
    The returned receipt keeps original handoff fields and adds pool evidence.
    Call this before transport timing instrumentation to time every acquire.
    """
    if hasattr(runtime, 'r14_dma_pool'):
        raise DMAPoolError('runtime DMA pool already installed')
    if getattr(runtime, 'closed', False):
        raise DMAPoolError('cannot install a DMA pool on a closed runtime')
    transport = runtime.transport
    for name in ('_raw_buffer', '_profile_buffer'):
        if getattr(transport, name, None) is not None:
            raise DMAPoolError('cannot install a DMA pool while the transport owns a buffer')
    for name in ('raw_dma', 'profile_dma'):
        if getattr(getattr(transport, name, None), 'transfer_state', None) is not None:
            raise DMAPoolError('cannot install a DMA pool while DMA owns a transfer')
    pool = FiniteDMAPool(transport._allocate, max_buffers=max_buffers)
    original_close = runtime.close
    attempted = False
    final_receipt = None
    close_error = None

    def close():
        nonlocal attempted, final_receipt, close_error
        pool._check_owner()
        if attempted:
            if close_error is not None:
                raise DMAPoolError('runtime/pool close previously failed; no automatic retry') from close_error
            return dict(final_receipt, r14_dma_pool=pool.summary())
        attempted = True
        try:
            receipt = original_close()
        except BaseException as exc:
            close_error = exc
            pool.quarantine('runtime close raised: '+type(exc).__name__+': '+str(exc))
            raise
        final_receipt = dict(receipt) if isinstance(receipt, Mapping) else {'runtime_close_receipt':receipt}
        try:
            pool.close(receipt)
        except BaseException as exc:
            # Signal incomplete whole-owner cleanup even when hardware itself
            # closed. Never discard the original shutdown/handoff evidence.
            final_receipt.update(runtime_closed=final_receipt.get('closed') is True,
                closed=False, dma_pool_unconfirmed=True, error=type(exc).__name__+': '+str(exc))
        return dict(final_receipt, r14_dma_pool=pool.summary())

    runtime.r14_dma_pool = pool  # Strong reference survives every failure path.
    transport._allocate = pool.acquire
    runtime.close = close
    return pool
