"""Best-effort bounded writer evidence; never creates a durable receipt."""

import json
import math
import sys


def save_cleanup_preserving_error(save_callback, cleanup, original_error=None, *, stderr=None):
    """Save normally; retain an in-flight error if diagnostic persistence fails.

    The callback keeps the caller's exclusive-create and capacity policy. This
    helper neither retries the write nor supplies an unbudgeted disk fallback.
    False means cleanup was NOT saved. The caller must keep propagating its
    original exception; without one, the callback's failure is re-raised.
    """
    try:
        save_callback()
        return True
    except BaseException as save_error:
        def short_error(error):
            try:return type(error).__name__+': '+str(error)[:256]
            except BaseException:return type(error).__name__+': <unprintable>'

        def scalar(value):
            if value is None or type(value) is bool:return value
            if type(value) is int:return max(-2**63,min(value,2**63-1))
            if type(value) is float:return value if math.isfinite(value) else None
            if type(value) is str:return value[:256]
            return '<unsupported>'

        # Only copy this fixed field set, never the potentially large timeline.
        initialization={}
        telemetry=cleanup.get('writer_telemetry',{})
        preallocation=telemetry.get('payload_preallocation',{}) if isinstance(telemetry,dict) else {}
        if isinstance(preallocation,dict):
            for key in ('enabled','status','requested_bytes','run_bound_bytes',
                        'allocated_charge_bytes','allocation_accounting_status',
                        'initialization_s','allocation_s','initial_sync_s','error'):
                if key in preallocation:initialization[key]=scalar(preallocation[key])
        diagnostic=dict(event='cleanup_save_failed',cleanup_persisted=False,
            completed=False,durability_claim=False,cleanerror=short_error(save_error),
            original_error=short_error(original_error) if original_error is not None else None,
            accepted=scalar(cleanup.get('writer_accepted',0)),
            written=scalar(cleanup.get('writer_written',0)),
            durable=cleanup.get('writer_durable') is True,initialization=initialization)
        if original_error is not None:
            try:
                diagnostics=getattr(original_error,'diagnostics',None)
                if not isinstance(diagnostics,dict):
                    diagnostics={}
                    original_error.diagnostics=diagnostics
                diagnostics['cleanup_save_failure']=diagnostic
            except BaseException:
                # Some external exceptions prohibit attribute assignment. The
                # bounded stderr record remains available to the supervisor.
                pass
        try:
            target=sys.stderr if stderr is None else stderr
            target.write(json.dumps(diagnostic,allow_nan=False,separators=(',',':'))+'\n')
        except BaseException:
            # A broken diagnostic pipe must never replace either real error.
            pass
        if original_error is None:raise
        return False


def cleanup_writer_telemetry(writer, receipt):
    if writer is None:
        return dict(status='not_created', durability_claim=False)
    # A stuck/unfinished worker must not make error reporting wait on a lock.
    condition=getattr(writer,'_condition',None)
    if condition is None:
        return dict(status='unavailable_no_condition',durability_claim=False,
                    worker_joined=bool(getattr(receipt,'worker_joined',False)))
    if not condition.acquire(False):
        return dict(status='unavailable_lock_busy', durability_claim=False,
                    worker_joined=bool(getattr(receipt,'worker_joined',False)))
    try:
        try:
            timeline=writer.persistence_timeline
            summary=writer.persistence_timeline_summary
            batches=writer.batch_stats
            accounting=writer._account_batches()
            return dict(status='captured',durability_claim=False,
                payload_preallocation=getattr(writer,'payload_preallocation',None),
                worker_joined=bool(getattr(receipt,'worker_joined',False)),
                persistence_timeline=[dict(row) for row in timeline],
                persistence_timeline_summary=dict(summary),
                batch_stats=[dict(row) for row in batches],
                persistence_io_accounting=dict(accounting))
        except Exception as exc:
            # The original run error and actual receipt keep precedence.
            return dict(status='capture_failed',durability_claim=False,
                error=type(exc).__name__+': '+str(exc),
                worker_joined=bool(getattr(receipt,'worker_joined',False)))
    finally:
        condition.release()
