"""Opt-in journal KEEP_SIZE preparation; allocation is not a durable frame.

This experiment may reduce allocation metadata during later journal fsyncs.
It has no measured performance claim and changes neither transaction bytes nor
the fsync-before-acknowledgement contract. Call only before producer admission.
"""
from .storage_journal15c import JournalSink15C
from .storage_prealloc15b import linux_keep_size, prepare_payload
import os


MAX_JOURNAL_EXTENT_BYTES=64*1024*1024


def prepare_journal(journal, budget, extent_bytes, run_bound_bytes, *,
                    allocate=linux_keep_size, stat=os.fstat, sync=os.fsync):
    """Prepare a fresh journal, retaining conservative allocation accounting.

    The allocation debit is additional to every normal frame reservation. Seed
    the sink's observed-allocation baseline so its first transaction does not
    mistake preallocated blocks for an unreserved per-frame allocation excess.
    Failures retain the helper's actual debit or unobserved reservation and are
    propagated unchanged. There is no truncate, fallback, retry or early ACK.
    """
    if (type(extent_bytes) is not int or type(run_bound_bytes) is not int
            or not 0<=extent_bytes<=MAX_JOURNAL_EXTENT_BYTES
            or run_bound_bytes<0 or extent_bytes>run_bound_bytes):
        raise ValueError('journal KEEP_SIZE extent must be 0..64MiB within explicit run bound')
    common=dict(storage_backend='journal15c',experiment='journal_keep_size_r28',
        performance_verified=False,initialization_scope='before_producer_admission',
        journal_budget_baseline_bytes=0)
    if not extent_bytes:
        report=prepare_payload(None,None,0,run_bound_bytes)
        report.update(common)
        return report
    if not isinstance(journal,JournalSink15C):
        raise TypeError('JournalSink15C required for journal KEEP_SIZE preparation')
    if (journal.txn!=0 or journal.logical_end!=0 or journal.current is not None
            or journal.budget_accounted_bytes!=0
            or getattr(journal,'_r28_preallocation_attempted',False)):
        raise ValueError('journal KEEP_SIZE requires an unused journal; no retry after allocation attempt')
    journal._r28_preallocation_attempted=True
    try:
        report=prepare_payload(journal.stream,budget,extent_bytes,run_bound_bytes,
            allocate=allocate,stat=stat,sync=sync)
    except BaseException as exc:
        report=getattr(exc,'preallocation_report',None)
        if isinstance(report,dict):report.update(common)
        raise
    # This is an allocation ledger baseline, never a written/durable watermark.
    journal.budget_accounted_bytes=report['allocated_charge_bytes']
    report.update(common)
    report['journal_budget_baseline_bytes']=journal.budget_accounted_bytes
    return report
