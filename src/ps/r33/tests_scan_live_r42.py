import ast
import json
from pathlib import Path
import threading
import time
import unittest

import numpy as np

from . import live
from .scan_live import AsyncMotor, StepScan, scan_panel, validate_authority


class Backend:
    def __init__(self):
        self.moves, self.stops, self.closes = [], 0, 0
        self.ready = True
        self.error = None

    def begin(self, pulses):
        self.moves.append(pulses)

    def poll(self):
        if self.error:
            raise self.error
        return dict(emitted=self.moves[-1], accepted_id=len(self.moves), epoch=7) if self.ready else None

    def stop(self):
        self.stops += 1

    def close(self):
        self.closes += 1


class ScanTests(unittest.TestCase):
    @staticmethod
    def guard_mock():
        # The historical orchestrator mock exposes permit polarity as enable_pin.
        # R42 checks the physical active-low EN pin, matching the RTL and board
        # preflight in rotating_stripe_preview.py; adapt only this test fixture.
        from structured_light.motion.mock import MockGuardTransport
        from structured_light.motion.guard import STATUS_BITS
        class PhysicalPinMock(MockGuardTransport):
            def _status_word(self):
                value = super()._status_word()
                bit = 1 << STATUS_BITS['enable_pin']
                return (value & ~bit) if value & 1 else (value | bit)
        return PhysicalPinMock()

    def test_authority_requires_exact_machine_boot_run_and_power(self):
        manifest = dict(motor_controller=dict(present=True, base_address=0x43c40000),
                        motor_scan=dict(present=True, pulses_per_revolution=3200,
                                       guard_base=0x43c50000, motor_base=0x43c40000))
        auth = dict(machine_id='m', boot_id='b', run_id='r', allow_motor=True,
                    motor_12v_connected=True, user_statement='approved this run')
        self.assertEqual(validate_authority(manifest, auth, run_id='r', machine_id='m', boot_id='b')['run_id'], 'r')
        for key, value in (('boot_id', 'stale'), ('run_id', 'old'), ('allow_motor', False),
                           ('motor_12v_connected', False), ('user_statement', '')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_authority(manifest, {**auth, key: value}, run_id='r', machine_id='m', boot_id='b')
        with self.assertRaises(ValueError):
            validate_authority({}, auth, run_id='r', machine_id='m', boot_id='b')
        for controller in ({}, dict(present=True, base_address=0x43c40004)):
            with self.subTest(controller=controller), self.assertRaises(ValueError):
                validate_authority({**manifest, 'motor_controller': controller}, auth,
                                   run_id='r', machine_id='m', boot_id='b')

    def test_no_permission_no_move_and_single_scan_only(self):
        scan = StepScan()
        scan.button(1)
        self.assertEqual(scan.state, 'IDLE')
        self.assertIn('refused', scan.message)
        for views in (1, 201, 240, 321):
            with self.assertRaises(ValueError):
                StepScan(views=views)
        with self.assertRaises(ValueError):
            StepScan(settle_frames=1)

    def test_full_200_and_320_views_with_settling_and_identity(self):
        for views in (200, 320):
            with self.subTest(views=views):
                backend = Backend()
                scan = StepScan(backend, views=views, settle_s=0)
                scan.button(1)
                for index in range(views):
                    self.assertFalse(scan.frame(valid=True))  # move completed; never accept this exposure
                    self.assertEqual(scan.identity(), dict(motor_id=index + 1, motor_epoch=7,
                                                          motor_steps=(index + 1) * (3200 // views)))
                    self.assertFalse(scan.frame(valid=True))
                    self.assertFalse(scan.frame(valid=True))
                    self.assertTrue(scan.frame(valid=True))
                    scan.advance()
                self.assertEqual(scan.state, 'DONE')
                self.assertEqual(scan.steps, 3200)
                self.assertEqual(sum(backend.moves), 3200)
                self.assertEqual((backend.stops, backend.closes), (1, 1))
                scan.button(1)
                self.assertEqual(len(backend.moves), views)

    def test_time_and_validity_gate(self):
        now = [0.]
        scan = StepScan(Backend(), settle_s=.5, monotonic=lambda: now[0])
        scan.button(1)
        scan.frame(valid=True)
        for _ in range(5):
            self.assertFalse(scan.frame(valid=True))
        now[0] = .6
        self.assertFalse(scan.frame(valid=False))
        self.assertTrue(scan.frame(valid=True))

    def test_stop_while_moving_is_nonblocking_and_closes(self):
        backend = Backend()
        backend.ready = False
        scan = StepScan(backend)
        scan.button(1)
        self.assertFalse(scan.frame(valid=True))
        scan.button(1)
        self.assertEqual(scan.state, 'STOPPED')
        self.assertEqual((backend.stops, backend.closes), (1, 1))
        self.assertFalse(scan.frame(valid=True))

    def test_camera_pfo3_motor_error_always_stop_close_no_retry(self):
        for reason in ('camera', 'PFO3', 'motor'):
            backend = Backend()
            scan = StepScan(backend)
            scan.button(1)
            if reason == 'motor':
                backend.error = RuntimeError('injected')
                with self.assertRaises(RuntimeError):
                    scan.frame(valid=True)
            else:
                scan.fail(reason)
            self.assertEqual(scan.state, 'FAILED')
            self.assertEqual((backend.stops, backend.closes), (1, 1))
            scan.button(1)
            self.assertEqual(len(backend.moves), 1)

    def test_pulse_mismatch_rejected_and_cleaned(self):
        backend = Backend()
        backend.poll = lambda: dict(emitted=15, accepted_id=1)
        scan = StepScan(backend)
        scan.button(1)
        with self.assertRaisesRegex(RuntimeError, 'pulse count'):
            scan.frame(valid=True)
        self.assertEqual((backend.stops, backend.closes), (1, 1))

    def test_real_motor_and_guard_controllers_on_repository_mock(self):
        from structured_light.motion.mock import MockMotorTransport, MockGuardTransport
        from structured_light.motion.motor_control import MotorController
        from structured_light.motion.guard import GuardController
        motor_transport, guard_transport = MockMotorTransport(), self.guard_mock()
        backend = AsyncMotor(MotorController(motor_transport), GuardController(guard_transport))
        backend.begin(16)
        deadline = time.monotonic() + 2
        result = None
        while result is None and time.monotonic() < deadline:
            result = backend.poll()
            time.sleep(.001)
        self.assertIsNotNone(result)
        self.assertEqual(result['emitted'], 16)
        backend.close()
        self.assertTrue(motor_transport.closed and guard_transport.closed)

    def test_actual_async_driver_stop_and_close_on_repository_mock(self):
        from structured_light.motion.mock import MockMotorTransport, MockGuardTransport
        from structured_light.motion.motor_control import MotorController
        from structured_light.motion.guard import GuardController
        motor_transport, guard_transport = MockMotorTransport(mode='stuck'), self.guard_mock()
        backend = AsyncMotor(MotorController(motor_transport), GuardController(guard_transport))
        # This model can expose a fault rather than running indefinitely: both outcomes
        # must still execute the stop/disarm/close path without a second START.
        backend.begin(16)
        backend.close()
        self.assertTrue(motor_transport.closed and guard_transport.closed)

    def test_panel_and_provisional_transform(self):
        from structured_light.scan3d.turntable import assumed_turntable
        model = assumed_turntable((0, 0, 0), (0, 1, 0), source='Mock')
        p = model.to_object_frame(np.array([[1., 0., 0.]]), 800)
        self.assertTrue(np.allclose(np.abs(p[0]), [0, 0, 1]))
        images = scan_panel(StepScan(), [np.column_stack((p, [1]))])
        self.assertEqual(images[0].shape, (480, 456, 3))
        self.assertEqual(images[1].shape, (106, 1280, 3))

    def test_live_wires_buttons_scan_object_output_and_main_thread_pillow(self):
        source = Path(live.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        for token in ('buttons.poll()', 'scan.button(delta[0])', 'scan.frame(valid=', 'scan.advance()',
                      "'xyz_object_mm.npy'", "'scan_frames.json'", 'scan.fail(str(error))',
                      'scan.stop(\'live run stopped\')', 'hardware_motor(overlay, manifest'):
            self.assertIn(token, source)
        display_loop = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                            and node.name == 'display_loop')
        calls = [ast.unparse(node.func) for node in ast.walk(display_loop) if isinstance(node, ast.Call)]
        self.assertFalse(any('render' in name or 'panel' in name or 'Image' in name for name in calls))


if __name__ == '__main__':
    unittest.main()
