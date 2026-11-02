"""Latch the parent deadline after all G close observations have completed.

Frozen G owns the worker, renderer and bounded owner protocol. A clean child
observed after the parent deadline cannot authorize a successful H close.
"""
import time

from .display_r20g import ProcessDisplay as GProcessDisplay
from .display_r20g import DisplayStartupError, DisplayCloseError
from .journal_owner_r20g import close_budget


class ProcessDisplay(GProcessDisplay):
    def close(self, timeout_s=30.):
        if self._final_result is not None:
            return super().close(timeout_s=timeout_s)
        budget = close_budget(timeout_s)
        deadline = time.monotonic()+budget
        failure = None
        try:
            result = super().close(timeout_s=max(0., deadline-time.monotonic()))
        except DisplayCloseError as exc:
            failure = exc
            result = exc.receipt
        # G's final drain, last is_alive observation and pipe close have all
        # completed. Keep actual joined/exit facts while latching late failure.
        result['close_deadline_exceeded'] = time.monotonic() > deadline
        if result['close_deadline_exceeded']:
            result['owner_closed'] = False
            detail = 'parent display close deadline exceeded; cleanup unconfirmed; retain hardware lock'
            result['error'] = (result['error']+'\n' if result['error'] else '')+detail
            raise DisplayCloseError(result['error'], result)
        if failure is not None:
            raise failure
        return result

    @property
    def cleanup_confirmed(self):
        result = self._final_result
        return bool(result is not None and not result.get('close_deadline_exceeded')
                    and result.get('owner_closed') is True
                    and result.get('worker_joined') is True
                    and result.get('worker_exitcode') == 0)


__all__ = ['ProcessDisplay', 'DisplayStartupError', 'DisplayCloseError']
