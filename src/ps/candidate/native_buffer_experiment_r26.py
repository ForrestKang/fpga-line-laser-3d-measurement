"""Explicit cold-start experiment; never enabled by importing this module.

The caller must be a NEW process before any native assemble call, alias caching,
or worker thread startup. Historical calls/aliases cannot be inferred from the
legacy module, so the caller must explicitly attest to these preconditions.
No protocol or C implementation is replaced. This is not a production default.
"""
from __future__ import annotations
import ctypes
from pathlib import Path
import threading

MAX_CAPACITY=16*1024**2
_INSTALL_LOCK=threading.Lock()


def capacity_upper_bound(primary_bytes):
    """Every tagged 17-word record can contribute at most one 64-byte output.

    Counting patches as output is conservative. Malformed framing is still
    rejected by the unchanged C routine, before it writes any output.
    """
    if type(primary_bytes) is not int or not 0<primary_bytes<=MAX_CAPACITY:
        raise ValueError('native assemble input out of bounds')
    if primary_bytes<384 or primary_bytes%4 or (primary_bytes-384)%68:
        return 256
    bound=256+((primary_bytes-384)//68)*64
    if bound>MAX_CAPACITY:raise ValueError('native assemble capacity exceeded')
    return bound


class _BoundedNativeAssembler:
    def __init__(self,module):
        self._module=module;self._library=module._LIB;self._lock=module._CALL_LOCK
        self._buffer=None;self.allocated_bytes=0;self.calls=0;self.growth_events=0

    def __call__(self,primary_stream):
        data=bytes(primary_stream)
        bound=capacity_upper_bound(len(data))
        capacity=max(256,1<<(bound-1).bit_length())
        with self._lock:
            if self._module._LIB is not self._library:
                raise RuntimeError('native library changed after cold-start installation')
            if capacity>self.allocated_bytes:
                # No caller can own this buffer: returns are copied to bytes
                # under the same lock. Drop the old allocation before growth.
                self._buffer=None;self.allocated_bytes=0
                self._buffer=ctypes.create_string_buffer(capacity)
                self.allocated_bytes=capacity;self.growth_events+=1
            length=ctypes.c_uint32(0)
            code=self._library.assemble_r26(data,len(data),self._buffer,
                                           self.allocated_bytes,ctypes.byref(length))
            if code:
                from .pfo3_r25 import PFO3Error
                raise PFO3Error(self._module.ERRORS.get(code,'native assemble failed')
                               +(' ' if code==4 else ''))
            if length.value>self.allocated_bytes:
                raise RuntimeError('native output length exceeds allocated capacity')
            result=ctypes.string_at(self._buffer,length.value)
            self.calls+=1
            return result


def install_cold_start(*,acknowledge_new_process_no_calls_or_aliases=False,native_module=None):
    """Install once, explicitly, in the single-threaded main startup sequence.

    Returns the callable with observable allocation counters. The legacy module
    attribute is replaced before its 16 MiB buffer reference is released. Cached
    aliases to the old callable are forbidden; concurrent installation is an
    error. This cannot be used to retrofit an already running application.
    """
    if acknowledge_new_process_no_calls_or_aliases is not True:
        raise RuntimeError('explicit new-process/no-prior-calls/no-cached-aliases attestation required')
    if threading.current_thread() is not threading.main_thread() or threading.active_count()!=1:
        raise RuntimeError('cold-start installation requires the single-threaded main thread')
    if not _INSTALL_LOCK.acquire(blocking=False):raise RuntimeError('concurrent native-buffer installation forbidden')
    try:
        if native_module is None:
            from . import pfo3_assemble_native_r26 as native_module
        expected=Path(__file__).with_name('pfo3_assemble_native_r26.py').resolve()
        if Path(native_module.__file__).resolve()!=expected:
            raise RuntimeError('unexpected native binding module')
        if getattr(native_module,'_r26_buffer_experiment',None) is not None:
            raise RuntimeError('native-buffer experiment already installed')
        if native_module._LIB is None:raise RuntimeError('native assemble library not built')
        if not native_module._CALL_LOCK.acquire(blocking=False):
            raise RuntimeError('native assemble call active during installation')
        try:
            replacement=_BoundedNativeAssembler(native_module)
            native_module.assemble_native=replacement
            native_module._r26_buffer_experiment=replacement
            native_module._OUTPUT=None
        finally:native_module._CALL_LOCK.release()
        return replacement
    finally:_INSTALL_LOCK.release()
