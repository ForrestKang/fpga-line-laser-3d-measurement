"""Optional Linux KEEP_SIZE allocation; never changes payload EOF or commits."""
import ctypes
import os
import sys
import time

from resource_policy import accounted_file_bytes, BudgetExceeded


def linux_keep_size(fd, length):
    if not sys.platform.startswith('linux'):
        raise OSError('KEEP_SIZE requires Linux; no EOF-growing fallback')
    libc=ctypes.CDLL(None,use_errno=True)
    # ARMv7 may expose 32-bit off_t on fallocate. Require the explicit 64-bit
    # symbol; do not call a mismatched ABI or silently grow logical EOF.
    try:function=libc.fallocate64
    except AttributeError as exc:raise OSError('fallocate64 unavailable; KEEP_SIZE unsupported') from exc
    function.argtypes=(ctypes.c_int,ctypes.c_int,ctypes.c_longlong,ctypes.c_longlong)
    function.restype=ctypes.c_int
    if function(fd,1,0,length)!=0:
        error=ctypes.get_errno()
        raise OSError(error,os.strerror(error))


def prepare_payload(stream, budget, extent_bytes, run_bound_bytes, *,
                    allocate=linux_keep_size, stat=os.fstat, sync=os.fsync):
    """Reserve independently, conservatively double-charge later frame writes.

    This is an opt-in A/B experiment. The extra allocation debit is intentionally
    not consumed by frame tickets; it can reject a run early, never hide space.
    Partial allocation and sync errors retain their allocated-byte accounting.
    """
    report=dict(enabled=bool(extent_bytes),requested_bytes=extent_bytes,
        run_bound_bytes=run_bound_bytes,status='disabled' if not extent_bytes else 'starting',
        accounting='extra_allocation_plus_normal_frame_tickets_conservative_double_count',
        initialization_s=0.,allocation_s=0.,initial_sync_s=0.,allocated_charge_bytes=0,
        durable_measurement_claim=False)
    if not extent_bytes:return report
    if (type(extent_bytes) is not int or type(run_bound_bytes) is not int
            or extent_bytes<=0 or extent_bytes>run_bound_bytes):
        raise ValueError('positive KEEP_SIZE extent within explicit run budget required')
    started=time.perf_counter();fd=stream.fileno()
    before=stat(fd)
    if before.st_size!=0 or accounted_file_bytes(before)!=0 or stream.tell()!=0:
        raise ValueError('KEEP_SIZE only supports a fresh empty payload')
    # Include the still-to-be-written run in the initial admission. Later frame
    # reservations remain unchanged and conservative, but allocation must not
    # consume the space which that run's existing preflight relied on.
    report['preflight_reservation_bytes']=extent_bytes+run_bound_bytes
    token=budget.reserve(extent_bytes+run_bound_bytes)
    report['allocation_accounting_status']='reservation_retained_until_observed'
    original_error=None
    try:
        allocation_started=time.perf_counter()
        try:allocate(fd,extent_bytes)
        except BaseException as exc:original_error=exc
        finally:report['allocation_s']=time.perf_counter()-allocation_started
        after=stat(fd)
        charge=max(0,accounted_file_bytes(after)-accounted_file_bytes(before))
        report['allocated_charge_bytes']=charge
        # Account observed allocation even when the kernel partially allocated
        # before returning an error. This method never claims data durability.
        report['allocation_accounting_status']='settlement_attempted_observed_charge'
        budget.settle_allocation(token,charge)
        report['allocation_accounting_status']='observed_allocation_charged'
        if original_error is not None:raise original_error
        if charge>extent_bytes:
            raise BudgetExceeded('observed KEEP_SIZE allocation exceeded extent; accounted and stopped')
        if after.st_size!=before.st_size or stream.tell()!=0:
            raise OSError('KEEP_SIZE changed logical EOF/position; evidence retained')
        sync_started=time.perf_counter()
        try:sync(fd)
        finally:report['initial_sync_s']=time.perf_counter()-sync_started
        report['status']='ready'
        return report
    except BaseException as exc:
        report['status']='failed';report['error']=type(exc).__name__+': '+str(exc)
        # Expose timings without converting an error into success.
        try:exc.preallocation_report=report
        except Exception:pass
        raise
    finally:
        report['initialization_s']=time.perf_counter()-started
