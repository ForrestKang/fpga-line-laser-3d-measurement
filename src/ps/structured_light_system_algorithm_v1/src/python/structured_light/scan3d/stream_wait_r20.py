"""Bounded capacity wait without draining unrelated queued measurements.

Persistence, snapshots, ordering and durable commit remain the original writer.
This method neither accepts an item nor treats uncommitted work as free space.
"""
import time
from .stream_chunk_writer import ChunkedAsyncWriter
from .stream_writer import WriterFailure,_timeout

class CapacityWaitWriter(ChunkedAsyncWriter):
    def submit_wait(self,record,timeout_s=30.0):
        """Snapshot once, then wait only for a rejected capacity ticket."""
        deadline=time.monotonic()+_timeout(timeout_s)
        # Do not catch BufferError from user data/snapshot validation.
        snapshot=self._snapshot_record(record)
        waited=False
        while True:
            try:
                # The frozen parent enqueue validates IDs/epoch and raises
                # BufferError only when its durable-capacity accounting is full.
                super()._enqueue('frame',snapshot)
                return waited
            except BufferError:
                waited=True;remaining=deadline-time.monotonic()
                if remaining<=0 or not self.wait_for_capacity(remaining):
                    raise TimeoutError('bounded persistence capacity wait expired; frame was not accepted')

    def wait_for_capacity(self,timeout_s=5.0):
        timeout=_timeout(timeout_s);deadline=time.monotonic()+timeout
        with self._condition:
            while True:
                if self._worker_error is not None:
                    raise WriterFailure(self._error_text or 'persistence writer failed',self._receipt_locked())
                if not self._accepting or self._worker_stopped:
                    raise RuntimeError('writer is closed')
                if self._accepted-self._written<self._capacity:
                    return True
                remaining=deadline-time.monotonic()
                if remaining<=0:return False
                self._condition.wait(remaining)

def submit_lossless_wait(writer,record,timeout_s=30.0):
    """Retry only rejected items, with one finite deadline and no queue loss."""
    return writer.submit_wait(record,timeout_s=timeout_s)
