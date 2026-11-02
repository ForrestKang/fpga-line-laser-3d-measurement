"""Candidate bounded close: one close RPC, then wait for that same owner.

R19 remains frozen. Only an acknowledged close plus Popen.wait returning zero
confirms cleanup; journal text and PID observations are never substitutes.
"""
from __future__ import annotations

import math
import subprocess
import time
import traceback

from structured_light.p2.hdmi_owner import RemoteHDMIOutput
from .journal_owner_r19 import JournalHDMIOwner, JournalOwnerError


def close_budget(timeout_s):
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise TypeError('close timeout must be a finite nonnegative number')
    timeout_s = float(timeout_s)
    if not math.isfinite(timeout_s) or timeout_s < 0:
        raise ValueError('close timeout must be a finite nonnegative number')
    return timeout_s


class OwnerCloseError(RuntimeError):
    def __init__(self, message, receipt):
        super().__init__(message)
        self.receipt = receipt


class DeadlineRemoteHDMIOutput(RemoteHDMIOutput):
    """Use remaining close budget for owner exit instead of a fixed five s.

    A failed attempt is final. This is not a retry/recovery interface for an
    already quarantined R19 owner. No signal, second RPC or lock operation is
    performed. Mapped ordinary RAM is released as in the frozen implementation.
    """

    def show_canvas_startup(self, canvas, timeout_s=20.):
        """One cold canvas RPC, with a separate finite startup budget.

        This does not establish cleanup and never retries a failed RPC. The
        steady-state socket/diagnostic timeout is restored even on failure.
        """
        budget = close_budget(timeout_s)
        if not 0 < budget <= 30.:
            raise ValueError('startup canvas remaining budget must be in (0, 30] s')
        if getattr(self, '_startup_canvas_attempted', False):
            raise RuntimeError('startup canvas may be attempted only once')
        if self.connection is None:
            raise RuntimeError('HDMI owner connection unavailable for startup canvas')
        self._startup_canvas_attempted = True
        connection = self.connection
        previous_timeout = self.timeout_s
        previous_socket_timeout = connection.gettimeout()
        try:
            self.timeout_s = budget
            connection.settimeout(budget)
            return super().show_canvas(canvas)
        finally:
            self.timeout_s = previous_timeout
            if self.connection is connection:
                connection.settimeout(previous_socket_timeout)

    def close(self, timeout_s=30.):
        budget = close_budget(timeout_s)
        if getattr(self, '_deadline_close_receipt', None) is not None:
            return self._deadline_close_receipt
        if getattr(self, '_deadline_close_error', None) is not None:
            raise self._deadline_close_error
        started = time.monotonic()
        deadline = started+budget
        error = None
        wait_error = None
        cleanup_errors = []
        returncode = None
        exited = False

        def remaining():
            return max(0., deadline-time.monotonic())

        try:
            if self.connection is None:
                raise RuntimeError('HDMI owner connection unavailable; no close ACK')
            if remaining() <= 0:
                raise RuntimeError('HDMI close deadline expired before close RPC')
            self.connection.settimeout(min(self.timeout_s, remaining()))
            if self.preview_pending:
                self.finish_preview()
                if remaining() <= 0:
                    raise RuntimeError('HDMI close deadline expired after preview')
                self.connection.settimeout(min(self.timeout_s, remaining()))
            self._rpc('close')
            self._close_acknowledged = True
        except BaseException as exc:
            error = exc
        finally:
            if self.connection is not None:
                try:
                    self.connection.close()
                except BaseException as exc:
                    cleanup_errors.append('connection: '+repr(exc))
                finally:
                    self.connection = None
            for name, release in (('shared_canvas', self._drop_shared_canvas),
                                  ('shared_scene', self._drop_shared_scene)):
                try:
                    release()
                except BaseException as exc:
                    cleanup_errors.append(name+': '+repr(exc))
        try:
            returncode = self.process.wait(timeout=remaining())
            exited = True
        except subprocess.TimeoutExpired as exc:
            wait_error = exc
        except BaseException as exc:
            wait_error = exc
        acknowledged = getattr(self, '_close_acknowledged', False) is True
        confirmed = bool(acknowledged and exited and returncode == 0 and error is None
                         and not cleanup_errors
                         and time.monotonic() <= deadline)
        receipt = dict(closed_confirmed=confirmed, owner_exited=exited,
            owner_pid=getattr(self, 'owner_pid', getattr(self.process, 'pid', None)),
            returncode=returncode, close_acknowledged=acknowledged,
            close_timeout_s=budget, close_elapsed_s=time.monotonic()-started,
            close_rpc_error=None if error is None else repr(error),
            owner_wait_error=None if wait_error is None else repr(wait_error),
            shared_cleanup_errors=cleanup_errors)
        if not confirmed:
            detail = receipt['close_rpc_error'] or receipt['owner_wait_error'] or '; '.join(cleanup_errors)
            message = (f'HDMI owner {receipt["owner_pid"]} cleanup unconfirmed; '
                       f'close ACK={acknowledged}, owner exited={exited}, returncode={returncode}; '
                       f'retain hardware lock and allocations; {detail or "deadline/nonzero exit"}')
            self._deadline_close_error = OwnerCloseError(message, receipt)
            raise self._deadline_close_error
        getattr(self, '_scene_unconfirmed_leases', []).clear()
        self._deadline_close_receipt = receipt
        return receipt


class DeadlineJournalHDMIOwner(JournalHDMIOwner):
    """Keep inherited exclusive RAM-log persistence after the bounded wait.

    File/fsync calls retain their existing OS blocking behavior. The parent
    display deadline can expire during persistence and must remain conservative.
    """

    def __init__(self, config, *, remote_factory=None, **options):
        mocked = remote_factory is not None
        try:
            super().__init__(config, remote_factory=remote_factory or DeadlineRemoteHDMIOutput,
                             **options)
        except JournalOwnerError as exc:
            exc.journal_receipt['owner_mocked'] = mocked
            raise
        finally:
            self.owner_mocked = mocked

    def close(self, timeout_s=30.):
        budget = close_budget(timeout_s)
        if self._close_receipt is not None:
            return self._close_receipt
        remote = None
        close_error = None
        try:
            remote = self.owner.close(timeout_s=budget)
        except BaseException as exc:
            close_error = traceback.format_exc()
            remote = getattr(exc, 'receipt', None)
        confirmed = bool(isinstance(remote, dict) and remote.get('closed_confirmed') is True
                         and remote.get('owner_exited') is True and remote.get('returncode') == 0
                         and close_error is None)
        persistence = self._persist('confirmed_close' if confirmed else 'unconfirmed_close', final=True)
        self._close_receipt = dict(closed_confirmed=confirmed,
            owner_exited=bool(isinstance(remote, dict) and remote.get('owner_exited') is True),
            returncode=remote.get('returncode') if isinstance(remote, dict) else None,
            owner_close_receipt=remote, owner_close_error=close_error, **persistence)
        return self._close_receipt

    def render_and_show(self, renderer, gray, points, options):
        return self._proxy('render_and_show',renderer,gray,points,options)

    def show_canvas_startup(self, canvas, timeout_s=20.):
        return self._proxy('show_canvas_startup', canvas, timeout_s)


def factory(config):
    options = config.get('_journal_test_options')
    if options is not None:
        if not callable(options.get('remote_factory')):
            raise ValueError('journal test options require an explicit mock owner factory')
        return DeadlineJournalHDMIOwner(config, **options)
    return DeadlineJournalHDMIOwner(config)


ram_journal_owner = factory
