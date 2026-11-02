"""Run live.main itself with offline camera/PL/HDMI/motor adapters; no board access."""
import contextlib
import json
import struct
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np

from . import live
from .scan_live import StepScan
from .tests_scan_live_r42 import Backend
from .protocol import FINGERPRINT, HEADER_FIELDS, IDENTITY_FIELDS, PFO3Error
from .protocol_r35 import assemble_r35


def decoded_pl_frame(identity):
    """A real decoded, frame-qualified packet with one geometrically valid point.

    Image quality needs 120 supported rows. The other image observations are
    explicit geometry rejects, retaining the one-point-per-view scan fixture.
    """
    count = 120
    header = dict.fromkeys(HEADER_FIELDS, 0)
    for key, value in identity.items():
        if key + '_lo' in header:
            header[key + '_lo'], header[key + '_hi'] = value & 0xffffffff, value >> 32
        else:
            header[key] = value
    header.update(width=752, height=480, roi_x1=752, roi_y1=480, roi_pixels=752*480,
                  algorithm_revision=33, fixed_revision=1, policy_revision=1)
    header.update({'fingerprint'+str(i): value for i, value in
                   enumerate(struct.unpack('<8I', bytes.fromhex(FINGERPRINT)))})
    start = np.array([header[key] for key in HEADER_FIELDS], dtype='<u4')
    sequence = identity['frame_id'] + 1
    start[:5] = [0x35544850, 35, 752, 480, sequence]
    local = np.zeros((count, 16), dtype='<u4')
    local[:, 0], local[:, 1] = np.arange(count), np.arange(200, 200+count)
    local[:, 2:4] = [299, 301]
    local[:, 4], local[:, 5] = 300*4096, local[:, 1]*4096
    local[:, 13] = (1 << 24) | (1 << 25) | (1 << 26)
    rows = np.zeros((480, 16), dtype='<u4')
    rows[:, 0] = np.arange(480)
    rows[200:200+count, 1] = 200
    rows[200:200+count, 4:8] = 1
    rows[200:200+count, 8:10] = 3
    rows[200:200+count, 10:12] = 200
    geometry = np.zeros((count, 16), dtype='<u4')
    geometry[:, 0] = np.arange(count)
    geometry[:, 7] = (1 << 16) | 3
    geometry[0, 1:7] = [65536, 2*65536, 100*65536]*2
    geometry[0, 7] = 0x103
    metadata = np.zeros((1, 16), dtype='<u4')
    metadata[0, 0], metadata[0, 11:13] = identity['epoch'], [1, 2]
    records = []
    for tag, block in ((0, local), (1, local), (2, rows), (3, geometry), (6, metadata)):
        tagged = np.empty((len(block), 17), dtype='<u4')
        tagged[:, 0], tagged[:, 1:] = 0x35520000 | tag, block
        records.append(tagged)
    body = np.concatenate(records)
    footer = np.zeros(32, dtype='<u4')
    footer[:8] = [0x35544650, 35, count, count, count, count, count, count]
    footer[11], footer[14], footer[16] = 1, 448813, 752*480
    footer[23:27] = [sequence, 96+17*len(body), 1, 1]
    footer[29] = count
    primary = start.tobytes() + body.tobytes() + footer.tobytes()
    frame, receipt = assemble_r35(primary, expected_identity=identity, backend='reference')
    return frame, primary, receipt


class LiveIntegrationTests(unittest.TestCase):
    def test_transport_fixture_obeys_decoded_frame_contract(self):
        identity = dict.fromkeys(IDENTITY_FIELDS, 0)
        identity.update(frame_id=17, epoch=3, motor_steps=(1 << 32)+16)
        frame, _, _ = decoded_pl_frame(identity)
        self.assertEqual(set(frame.header), set(HEADER_FIELDS))
        self.assertEqual(frame.header['frame_id'], 17)
        self.assertEqual(frame.header['motor_steps_hi'], 1)
        self.assertEqual(frame.header['frame_decisions'], 3)
        self.assertEqual(frame.header['measurement_count'], 120)
        self.assertEqual(frame.header['preview_count'], 120)
        self.assertEqual(int(frame.rows[:, 6].sum()), 120)
        self.assertEqual(int(frame.accepted('measurement').sum()), 120)
        self.assertEqual(int(np.count_nonzero((frame.geometry_records[:, 7] & 0x101) == 0x101)), 1)
        self.assertFalse(frame.records.flags.writeable or frame.geometry_records.flags.writeable)

    def test_transport_fixture_still_uses_strict_protocol_rejection(self):
        identity = dict.fromkeys(IDENTITY_FIELDS, 0)
        _, primary, _ = decoded_pl_frame(identity)
        bad_identity = dict(identity, frame_id=1)
        with self.assertRaisesRegex(PFO3Error, 'identity mismatch'):
            assemble_r35(primary, expected_identity=bad_identity, backend='reference')
        bad_quality = bytearray(primary)
        struct.pack_into('<I', bad_quality, len(primary)-32*4+25*4, 0)
        with self.assertRaisesRegex(PFO3Error, 'frame quality decision inconsistent'):
            assemble_r35(bad_quality, expected_identity=identity, backend='reference')

    def run_live(self, *, stop_button=False, camera_error=False, pl_error=False,
                 allow_motor=True, deadline=600., full=False, on_pl_packet=None,
                 camera_close_error=False, capture_busy_after_close=False, lingering_thread=False,
                 pipeline=False, on_capture=None):
        import r33.buttons as button_module
        import r33.transport as transport_module
        import r33.scan_live as scan_module
        import structured_light.motion.lifecycle as lifecycle
        import structured_light.p2.pynq_transport as camera_transport
        import structured_light.p2.camera_evidence as evidence
        import structured_light.p2.controller as controller_module

        now, captures, polls, displayed, calls = [0.], [0], [0], [], []
        backend = Backend()
        scan_holder = []
        class MMIO:
            def __init__(self, *_): pass
            def read(self, _): return 0
            def write(self, *_): pass
        class Overlay:
            def __init__(self, *_, **__):
                self.ip_dict = {'pfo3_frontend': {'phys_addr': 0x43c60000}}
        class Camera:
            def enable_raw_buffer_reuse(self):
                calls.append('camera.enable_raw_buffer_reuse')
                self.raw_buffer_pool_stats = dict(allocations=1, reuses=0, capacity_bytes=752*480)
            def __init__(self, *_args, **_kwargs):
                self.mmio = MMIO()
                self._diag_mmio = types.SimpleNamespace(read=lambda _: 1)
                self.last_recovery = {}
                self.raw_dma = types.SimpleNamespace(reset_confirmed=False, last_stop_evidence={},
                    mmio=types.SimpleNamespace(read=lambda offset: 1 if offset == 0x34 else 0),
                    control_offset=0x30, status_offset=0x34)
                self.profile_dma = None
            def reset(self, reason):
                calls.append('camera.reset')
                self.last_recovery = dict(reason=reason, recovery_barrier_passed=True)
                self.raw_dma.reset_confirmed = True
                self.raw_dma.last_stop_evidence = dict(reset_observed=True, safe_to_release=True)
        class Controller:
            def __init__(self, transport, **_kwargs): self.transport = transport
            def clear_errors(self): pass
            def capture_raw(self):
                captures[0] += 1
                if on_capture is not None:
                    on_capture(captures[0])
                now[0] += .2
                if camera_error and captures[0] == 10:
                    calls.append(('injected_camera_failure', captures[0]))
                    raise RuntimeError('injected camera failure')
                return types.SimpleNamespace(pixels=bytes(752*480), sha256='mock-raw')
            def close(self):
                calls.append('camera.close')
                if camera_close_error:
                    raise RuntimeError('injected camera close failure')
                cam = self.transport
                if capture_busy_after_close:
                    cam.mmio.read = lambda _: 1
        class Transport:
            def __init__(self, *_args, **_kwargs): pass
            def configure_geometry(self, *_): pass
            def run(self, gray, identity):
                calls.append(('identity', dict(identity)))
                if pl_error and identity['frame_id'] == 8:
                    calls.append(('injected_pl_failure', identity['frame_id']))
                    raise RuntimeError('injected PFO3 failure')
                result, primary, receipt = decoded_pl_frame(identity)
                if on_pl_packet is not None:
                    on_pl_packet(identity, result.geometry_records)
                return result, primary, receipt, None
            def close(self): calls.append('pl.close')
        class Display:
            def __init__(self, *_): pass
            def configure(self): pass
            def show(self, *_): displayed.append(now[0])
            def close(self): calls.append('display.close')
        class Buttons:
            def __init__(self, *_): pass
            def poll(self):
                polls[0] += 1
                # A real counter delta becomes one start, optionally one later STOP.
                if polls[0] == 1 or (stop_button and polls[0] == 8):
                    return (1, 0, 0, 0)
                if polls[0] == 4: return (0, 1, 0, 0)
                if polls[0] == 5: return (0, 0, 0, 1)
                return (0, 0, 0, 0)
        class Lock:
            def __init__(self, *_args, **_kwargs): pass
            def acquire(self): return self
            def release(self): calls.append('lock.release')
        def new_scan(**kwargs):
            kwargs['monotonic'] = lambda: now[0]
            kwargs['deadline_s'] = deadline
            scan = StepScan(**kwargs)
            scan_holder.append(scan)
            return scan
        fake_pynq = types.ModuleType('pynq')
        fake_pynq.MMIO, fake_pynq.Overlay = MMIO, Overlay
        fake_release = types.ModuleType('verify_p2_board')
        fake_release.release_camera = lambda *_: None
        fake_capture = types.ModuleType('gray_capture_board')
        fake_capture.verify_camera_control = lambda *_: {'pclk_hz': [48_000_000]}
        manifest = dict(bit_absolute='mock.bit', buttons=dict(present=True))
        with tempfile.TemporaryDirectory(dir=live.ROOT) as temp, contextlib.ExitStack() as stack:
            out = Path(temp) / 'run'
            stack.enter_context(patch.dict('sys.modules', {'pynq': fake_pynq, 'verify_p2_board': fake_release,
                                                         'gray_capture_board': fake_capture}))
            replacements = [(lifecycle, 'HardwareProcessLock', Lock),
                (transport_module, 'ActualPLTransportR25', Transport),
                (transport_module, 'resolve_manifest', lambda *_: manifest),
                (camera_transport, 'PynqTransport', Camera),
                (camera_transport, 'validate_runtime_contract', lambda *_args, **_kwargs: {}),
                (evidence, 'assess_signals', lambda *_args, **_kwargs: {'camera_signal_verified': True, 'frame_period_s': .02}),
                (evidence, 'run_monitor', lambda *_args, **_kwargs: {}),
                (controller_module, 'P2Controller', Controller),
                (button_module, 'Buttons', Buttons),
                (scan_module, 'hardware_motor', lambda *_: backend),
                (scan_module, 'StepScan', new_scan),
                (scan_module, 'scan_panel', lambda *_args, **_kwargs: (np.zeros((480,456,3), np.uint8), np.zeros((106,1280,3), np.uint8))),
                (live, 'LiveDisplay', Display),
                (live, 'camera_manifest', lambda *_: Path(temp)/'camera_manifest.json'),
                (live, 'render_header', lambda *_: np.zeros((116,1280,3), np.uint8))]
            for module, name, value in replacements:
                stack.enter_context(patch.object(module, name, value))
            stack.enter_context(patch.object(live.time, 'monotonic', lambda: now[0]))
            stack.enter_context(patch.object(live.signal, 'signal', lambda *_: None))
            stack.enter_context(patch('faulthandler.enable'))
            if lingering_thread:
                real_join = live.join_live_threads
                fake_thread = types.SimpleNamespace(name='R41_capture', join=lambda **_: None,
                                                    is_alive=lambda: True)
                stack.enter_context(patch.object(live, 'join_live_threads',
                                                lambda state, threads, errors: real_join(state, [fake_thread], errors)))
            args = ['--manifest', 'mock.json', '--intrinsics', str(live.ROOT/'calibration_r41/camera_intrinsics_r41.json'),
                    '--laser-plane', str(live.ROOT/'calibration_r41/laser_plane_r41.json'), '--output', str(out),
                    '--duration-s', '5', '--pages', 'A,C', '--initial-page', 'C',
                    '--pipeline' if pipeline else '--no-pipeline',
                    '--scan-settle-s', '0']
            if allow_motor:
                args += ['--allow-motor', '--motor-authority', 'mock-authority.json']
            rc = live.main(args)
            summary = json.loads((out/'summary.json').read_text())
            frames = json.loads((out/'scan_frames.json').read_text())
            object_cloud = np.load(out/'xyz_object_mm.npy') if (out/'xyz_object_mm.npy').is_file() else None
            return rc, summary, frames, object_cloud, backend, calls, len(displayed)

    def test_static_pipeline_does_not_prefetch_during_current_pl(self):
        # Use the real capture/display threads: a next raw exposure must not begin
        # while the current frame is held inside PL processing. In the old order,
        # the event is already set at dequeue, so the capture worker enters here.
        import threading
        in_pl, captured_during_pl = threading.Event(), threading.Event()
        observations = []
        def on_capture(number):
            if number > 2 and in_pl.is_set():
                captured_during_pl.set()
        def on_packet(identity, _):
            if identity['frame_id'] == 1:
                in_pl.set()
                observations.append(captured_during_pl.wait(timeout=.05))
                in_pl.clear()
        rc, state, frames, _, backend, _, displays = self.run_live(
            allow_motor=False, pipeline=True, on_capture=on_capture, on_pl_packet=on_packet)
        self.assertEqual(observations, [False])
        self.assertEqual(rc, 0)
        self.assertGreater(len(frames), 5)
        self.assertGreater(displays, 5)
        self.assertEqual(state['capture_prefetch'], 'after_processing')
        self.assertEqual(state['threads_alive_after_join'], [])
        self.assertTrue(state['hardware_quiescent'])
        self.assertEqual(backend.moves, [])

    def test_pipeline_pl_failure_stops_capture_and_closes_owners(self):
        rc, state, _, _, backend, calls, _ = self.run_live(
            allow_motor=False, pipeline=True, pl_error=True)
        self.assertEqual(rc, 1)
        self.assertEqual(state['phase'], 'failed')
        self.assertEqual(state['error'], 'injected PFO3 failure')
        self.assertEqual(calls.count(('injected_pl_failure', 8)), 1)
        self.assertEqual(state['threads_alive_after_join'], [])
        self.assertIn('camera.close', calls)
        self.assertIn('pl.close', calls)
        self.assertEqual(backend.moves, [])
        self.assertFalse(state['automatic_retry'])

    def test_main_completes_full_scan_after_demo_duration(self):
        rc, state, frames, cloud, backend, calls, displays = self.run_live(full=True)
        self.assertEqual(rc, 0)
        self.assertEqual(state['scan']['state'], 'DONE')
        self.assertEqual(state['scan']['views'], 200)
        self.assertEqual(state['scan']['steps'], 3200)
        self.assertEqual(sum(frame['accepted'] for frame in frames), 200)
        self.assertEqual(cloud.shape, (200, 4))
        self.assertGreater(len(frames), 25)  # exceeded five-second demonstration timer
        self.assertTrue(any(frame['motor_id'] > 0 for frame in frames))
        self.assertEqual((backend.stops, backend.closes), (1, 1))
        self.assertGreater(displays, 200)

    def test_main_button_stop_keeps_partial_cloud_and_no_retry(self):
        rc, state, frames, cloud, backend, *_ = self.run_live(stop_button=True)
        self.assertEqual(rc, 0)
        self.assertEqual(state['scan']['state'], 'STOPPED')
        self.assertLess(state['scan']['steps'], 3200)
        self.assertEqual((backend.stops, backend.closes), (1, 1))
        self.assertIsNotNone(cloud)

    def test_main_camera_and_pl_errors_stop_and_close(self):
        for camera_error, pl_error in ((True, False), (False, True)):
            with self.subTest(camera=camera_error):
                rc, state, _, _, backend, calls, _ = self.run_live(camera_error=camera_error, pl_error=pl_error)
                self.assertEqual(rc, 1)
                expected = 'injected camera failure' if camera_error else 'injected PFO3 failure'
                trigger = ('injected_camera_failure', 10) if camera_error else ('injected_pl_failure', 8)
                self.assertEqual(state['error'], expected)
                self.assertEqual(calls.count(trigger), 1)
                self.assertEqual(state['scan']['state'], 'FAILED')
                self.assertEqual((backend.stops, backend.closes), (1, 1))
                self.assertIn('camera.close', calls)
                self.assertIn('pl.close', calls)

    def test_main_deadline_is_failure_and_not_complete(self):
        rc, state, _, _, backend, *_ = self.run_live(deadline=1.)
        self.assertEqual(rc, 1)
        self.assertEqual(state['scan']['state'], 'FAILED')
        self.assertIn('deadline', state['error'])
        self.assertEqual((backend.stops, backend.closes), (1, 1))

    def test_main_default_button_never_creates_motion_backend(self):
        rc, state, _, cloud, backend, *_ = self.run_live(allow_motor=False)
        self.assertEqual(rc, 0)
        self.assertEqual(state['scan']['state'], 'IDLE')
        self.assertEqual(backend.moves, [])
        self.assertIsNone(cloud)
        self.assertIn('refused', state['scan']['message'])

    def test_shutdown_success_requires_fresh_capture_and_dma_readback(self):
        rc, state, _, _, _, calls, _ = self.run_live(allow_motor=False)
        self.assertEqual(rc, 0)
        self.assertTrue(state['capture_quiescent'])
        self.assertTrue(state['hardware_quiescent'])
        self.assertFalse(state['camera_stop_evidence']['sensor_stream_stopped'])
        self.assertEqual(state['camera_stop_evidence']['readback']['status'], 0)
        self.assertTrue(state['camera_stop_evidence']['dma']['raw']['last_stop_evidence']['safe_to_release'])
        self.assertTrue(state['camera_stop_evidence']['dma']['raw']['readback']['halted'])
        self.assertTrue(state['camera_stop_evidence']['bounded_stop']['success'])
        self.assertEqual(state['camera_stop_evidence']['bounded_stop']['reason'], 'live_shutdown')
        self.assertLess(calls.index('camera.reset'), calls.index('camera.close'))
        self.assertTrue(state['owner_close_results']['controller']['success'])
        self.assertFalse(state['automatic_retry'])

    def test_shutdown_close_failure_is_failed_not_stopped(self):
        rc, state, *_ = self.run_live(allow_motor=False, camera_close_error=True)
        self.assertEqual(rc, 1)
        self.assertEqual(state['phase'], 'failed')
        self.assertFalse(state['hardware_quiescent'])
        self.assertFalse(state['owner_close_results']['controller']['success'])
        self.assertIn('injected camera close failure', state['cleanup_errors'][0])
        self.assertIsNone(state['camera_stop_evidence']['readback'])

    def test_shutdown_lingering_thread_is_failed_and_skips_concurrent_close(self):
        rc, state, _, _, _, calls, _ = self.run_live(allow_motor=False, lingering_thread=True)
        self.assertEqual(rc, 1)
        self.assertEqual(state['phase'], 'failed')
        self.assertEqual(state['threads_alive_after_join'], ['R41_capture'])
        self.assertFalse(state['hardware_quiescent'])
        self.assertFalse(state['owner_close_results']['controller']['attempted'])
        self.assertNotIn('camera.close', calls)
        self.assertNotIn('camera.reset', calls)
        self.assertFalse(state['automatic_retry'])
        self.assertIn('process exit', state['hardware_lock_limit'])

    def test_successful_close_does_not_prove_busy_capture_is_quiescent(self):
        rc, state, *_ = self.run_live(allow_motor=False, capture_busy_after_close=True)
        self.assertEqual(rc, 1)
        self.assertEqual(state['phase'], 'failed')
        self.assertTrue(state['owner_close_results']['controller']['success'])
        self.assertFalse(state['capture_quiescent'])
        self.assertFalse(state['hardware_quiescent'])
        self.assertFalse(state['automatic_retry'])

    def test_close_without_dma_stop_evidence_is_not_confirmed(self):
        state = {}
        owner = types.SimpleNamespace(close=lambda: None)
        cam = types.SimpleNamespace(mmio=types.SimpleNamespace(read=lambda _: 0),
                                    _diag_mmio=types.SimpleNamespace(read=lambda _: 1),
                                    last_recovery={}, raw_dma=types.SimpleNamespace(), profile_dma=None)
        failures = live.close_live_owners(state, owner, owner, owner, cam)
        self.assertTrue(failures)
        self.assertFalse(state['capture_quiescent'])
        self.assertFalse(state['hardware_quiescent'])
        self.assertFalse(state['automatic_retry'])


if __name__ == '__main__':
    unittest.main()
