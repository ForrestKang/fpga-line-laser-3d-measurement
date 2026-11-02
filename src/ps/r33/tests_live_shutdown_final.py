"""Offline shutdown checks using the real controller/transport/DMA stop APIs."""
import types
import unittest

from .live import close_live_owners
from structured_light.p2.contract import DIAG_OFFSETS, REG_CONTROL, REG_STATUS
from structured_light.p2.controller import P2Controller
from structured_light.p2.pynq_transport import BoundedS2MM, PynqTransport


class Registers:
    def __init__(self, values, events, name, reset_stuck=False):
        self.values, self.events, self.name = dict(values), events, name
        self.reset_stuck = reset_stuck

    def read(self, offset):
        self.events.append(('read', self.name, offset))
        return self.values.get(offset, 0)

    def write(self, offset, value):
        self.events.append(('write', self.name, offset, value))
        self.values[offset] = value
        if self.name in ('raw', 'profile') and offset == 0x30:
            if value == 0:
                self.values[0x34] = 1
            elif value == 4 and not self.reset_stuck:
                self.values[0x30], self.values[0x34] = 0, 1


def fixture(profile=False, reset_stuck=False, ready=3):
    events = []
    cam = PynqTransport.__new__(PynqTransport)
    cam.transition_timeout_s = .005
    cam.mmio = Registers({REG_STATUS: 2}, events, 'capture')
    cam._diag_mmio = Registers({DIAG_OFFSETS['ready']: ready,
                               DIAG_OFFSETS['request_id']: 90,
                               DIAG_OFFSETS['epoch']: 3}, events, 'diag')
    cam.raw_dma = BoundedS2MM(Registers({0x30: 1, 0x34: 0x1002}, events, 'raw', reset_stuck))
    cam.profile_dma = (BoundedS2MM(Registers({0x30: 1, 0x34: 0x1002}, events, 'profile'))
                       if profile else None)
    cam._raw_buffer = cam._profile_buffer = None
    cam._accepted_request_id = cam._accepted_epoch = None
    cam._last_accepted_id, cam._last_accepted_epoch = 90, 3
    cam._want_raw = cam._want_profile = False
    cam._recovery_required = False
    cam._recovery_token = None
    cam.last_recovery = {}
    cam._read_cancel_evidence = lambda: {'valid': False, 'reason': None}
    controller = P2Controller.__new__(P2Controller)
    controller.transport, controller.mmio = cam, cam.mmio
    controller._inflight, controller.recorder, controller._closed = None, None, False
    owner = types.SimpleNamespace(close=lambda: events.append(('close', 'owner')))
    return cam, controller, owner, events


class LiveShutdownTests(unittest.TestCase):
    def test_idle_controller_close_alone_does_not_reset(self):
        cam, controller, _, events = fixture()
        controller.close()
        self.assertFalse(cam.raw_dma.reset_confirmed)
        self.assertEqual(cam.raw_dma.last_stop_evidence, {})
        self.assertFalse(any(e[0] == 'write' for e in events))

    def test_explicit_stop_resets_all_present_channels_then_reads_fresh_status(self):
        for profile in (False, True):
            with self.subTest(profile=profile):
                cam, controller, owner, events = fixture(profile=profile)
                state = {}
                self.assertEqual(close_live_owners(state, owner, owner, controller, cam), [])
                evidence = state['camera_stop_evidence']
                self.assertTrue(state['capture_quiescent'])
                self.assertTrue(state['hardware_quiescent'])
                self.assertFalse(evidence['sensor_stream_stopped'])
                self.assertEqual(evidence['last_recovery']['reason'], 'live_shutdown')
                stop = events.index(('write', 'capture', REG_CONTROL, 2))
                for name in evidence['dma']:
                    reset = events.index(('write', name, 0x30, 4))
                    self.assertLess(stop, reset)
                    self.assertIn(('read', name, 0x30), events[reset+1:])
                    self.assertIn(('read', name, 0x34), events[reset+1:])
                    self.assertTrue(evidence['dma'][name]['readback']['halted'])
                self.assertTrue(controller._closed)

    def test_reset_timeout_is_failed_and_not_retried(self):
        cam, controller, owner, events = fixture(reset_stuck=True)
        state = {}
        failures = close_live_owners(state, owner, owner, controller, cam)
        self.assertTrue(failures)
        self.assertFalse(state['capture_quiescent'])
        self.assertFalse(state['hardware_quiescent'])
        self.assertFalse(state['camera_stop_evidence']['bounded_stop']['success'])
        self.assertEqual(events.count(('write', 'raw', 0x30, 4)), 1)
        self.assertFalse(controller._closed)

    def test_camera_ready_barrier_failure_stays_failed(self):
        cam, controller, owner, _ = fixture(ready=5)
        state = {}
        self.assertTrue(close_live_owners(state, owner, owner, controller, cam))
        self.assertFalse(state['capture_quiescent'])

    def test_fresh_dma_readback_cannot_be_replaced_by_reset_receipt(self):
        cam, controller, owner, _ = fixture()
        real_close = controller.close
        def close():
            real_close()
            cam.raw_dma.mmio.values.update({0x30: 1, 0x34: 2})
        controller.close = close
        state = {}
        self.assertTrue(close_live_owners(state, owner, owner, controller, cam))
        self.assertTrue(state['camera_stop_evidence']['dma']['raw']['reset_confirmed'])
        self.assertFalse(state['capture_quiescent'])

    def test_live_worker_blocks_stop_close_and_readback(self):
        cam, controller, owner, events = fixture()
        state = {'threads_alive_after_join': ['R41_capture']}
        self.assertTrue(close_live_owners(state, owner, owner, controller, cam))
        self.assertFalse(state['capture_quiescent'])
        self.assertFalse(state['camera_stop_evidence']['bounded_stop']['attempted'])
        self.assertFalse(any(e[0] in ('write', 'read') for e in events))


if __name__ == '__main__':
    unittest.main()
