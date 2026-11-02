"""Clean capture-generation handoff for the Stage B runtime."""
from ..p2.pynq_transport import PynqTransport
from ..p2.contract import DIAG_OFFSETS, REG_STATUS, REG_PROFILE_ID, REG_ERROR_FLAGS
from ..p2.errors import ProtocolError


class HandoffPynqTransport(PynqTransport):
    """Resume only a clean, recorded generation; preserve P2 START checks.

    The runtime must verify boot/artifact identity and hold the process lock
    before restoring. Cancellation tokens are intentionally process-local.
    A failed/in-flight close cannot publish a resumable checkpoint.
    """

    def _handoff_state(self):
        if (self._accepted_request_id is not None or self._accepted_epoch is not None
                or self._raw_buffer is not None or self._profile_buffer is not None
                or self.raw_dma.transfer_state is not None
                or (self.profile_dma is not None and self.profile_dma.transfer_state is not None)
                or self.recovery_required or self._recovery_token is not None):
            raise ProtocolError('capture handoff requires no in-flight buffers or recovery token')

        return self._read_idle_state()

    def _read_idle_state(self):
        def read():
            return {
                'status': int(self.mmio.read(REG_STATUS)),
                'control_id': int(self.mmio.read(REG_PROFILE_ID)),
                'control_error': int(self.mmio.read(REG_ERROR_FLAGS)),
                **{k: int(self._diag_mmio.read(DIAG_OFFSETS[k])) for k in
                   ('ready', 'request_id', 'epoch', 'error', 'row_count', 'frame_count')},
            }
        state = read()
        if state != read():
            raise ProtocolError('capture handoff hardware snapshot changed while reading')
        if (state['status'] & 5 or state['ready'] & 5 != 1
                or state['error'] or state['control_error']
                or state['request_id'] != state['control_id']):
            raise ProtocolError('capture handoff requires idle, ready, error-free matching hardware identity')
        return state

    def export_capture_checkpoint(self):
        state = self._handoff_state()
        if self.last_accepted_id is None:
            raise ProtocolError('capture handoff requires a locally completed capture')
        if (state['control_id'] != self.last_accepted_id
                or state['epoch'] != self.last_accepted_epoch or not state['status'] & 2
                or state['row_count'] != self.mmio.frame_height):
            raise ProtocolError('capture handoff does not match a completed generation')
        return {'schema_version': 1, 'kind': 'completed', 'state': state}

    def restore_capture_checkpoint(self, checkpoint):
        if self.last_accepted_id is not None or self.last_accepted_epoch is not None:
            raise ProtocolError('capture handoff can only initialize a fresh transport')
        state = self._handoff_state()
        if checkpoint is None:
            # An aborted first request followed by CLEAR can also present
            # zero ID/epoch/counts. Only a real load establishes next ID0.
            raise ProtocolError('attach requires the previous clean capture_handoff.json before START')
        if (not isinstance(checkpoint, dict) or type(checkpoint.get('schema_version')) is not int
                or checkpoint['schema_version'] != 1
                or checkpoint.get('kind') != 'completed'
                or not isinstance(checkpoint.get('state'), dict)):
            raise ProtocolError('invalid capture handoff schema')
        saved = checkpoint['state']
        if set(saved) != set(state) or any(type(v) is not int for v in saved.values()) or saved != state:
            raise ProtocolError('capture handoff is stale or differs from current hardware')
        if not state['status'] & 2 or state['row_count'] != self.mmio.frame_height:
            raise ProtocolError('capture handoff lacks completed-frame evidence')
        self._last_accepted_id = state['control_id']
        self._last_accepted_epoch = state['epoch']
        self.mmio._request_id = state['control_id']
        self.mmio.epoch = state['epoch']
        self._attach_checkpoint = state

    def start_capture(self, *, raw, profile):
        checkpoint = getattr(self, '_attach_checkpoint', None)
        if checkpoint is not None and self._read_idle_state() != checkpoint:
            self._recovery_required = True
            raise ProtocolError('capture handoff changed after admission; no START issued')
        accepted = super().start_capture(raw=raw, profile=profile)
        self._attach_checkpoint = None
        return accepted
