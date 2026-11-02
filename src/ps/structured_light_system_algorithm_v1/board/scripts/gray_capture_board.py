"""Real-board signal/first-frame stages; called by verify_p2_board.py."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import time
from datetime import datetime, timezone


def _read_sensor(diag, register):
    if int(diag.read(0x38)) & 1:
        raise RuntimeError('SCCB is busy before independent read')
    diag.write(0x24, 0x48)
    diag.write(0x28, register)
    diag.write(0x34, 1)
    deadline = time.monotonic() + .5
    while time.monotonic() < deadline:
        status = int(diag.read(0x38))
        if status & 0xc:
            raise RuntimeError(f'SCCB reg {register:#x} failed, status={status:#x}')
        if status & 2 and not status & 1:
            return int(diag.read(0x30))
        time.sleep(.0005)
    raise TimeoutError(f'SCCB register {register:#x} deadline')


def verify_camera_control(diag, output, *, read_sensor=None, sleep=None):
    """Save partial control evidence even when the very first ID read fails."""
    read_sensor = read_sensor or _read_sensor
    sleep = sleep or time.sleep
    record = {'id_reads': [], 'registers': {}, 'pclk_hz': [],
              'camera_control_verified': False, 'dma_started': False}
    path = output / 'camera_control.json'
    def save():
        path.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    try:
        for _ in range(20):
            record['id_reads'].append(read_sensor(diag, 0))
            save()
        if record['id_reads'] != [0x1324] * 20:
            raise RuntimeError('ID stability failed in new Overlay')
        for reg in (1, 2, 3, 4, 5, 6, 7, 0xc, 0x72):
            record['registers'][hex(reg)] = read_sensor(diag, reg)
            save()
        for _ in range(3):
            sleep(.03)
            record['pclk_hz'].append(int(diag.read(0xc)))
        if min(record['pclk_hz']) <= 0 or max(record['pclk_hz']) > min(record['pclk_hz']) * 1.01:
            raise RuntimeError('PCLK zero or unstable')
        record['camera_control_verified'] = True
        save()
        return record
    except Exception as error:
        record['error'] = {'type': type(error).__name__, 'message': str(error)}
        # Read-only diagnostics; a failed snapshot must not mask the first error.
        record['failure_registers'] = {}
        for offset in (0, 4, 8, 0xc, 0x24, 0x28, 0x30, 0x38, 0x3c, 0x50):
            try:
                record['failure_registers'][hex(offset)] = int(diag.read(offset))
            except Exception as snapshot_error:
                record['failure_registers'][hex(offset)] = {'read_error': str(snapshot_error)}
        save()
        raise


def run_gray_stage(config, args, output):
    from verify_p2_board import validate_overlay_contract, release_camera, write_json
    from structured_light.p2.camera_evidence import run_monitor, assess_signals, export_gray, MON_REGISTERS
    from structured_light.p2.pynq_transport import PynqTransport, validate_runtime_contract
    from structured_light.p2.controller import P2Controller
    mode = config.get('overlay_mode', 'camera_raw_monitor')
    if mode not in {'camera_raw_monitor', 'camera_raw_hdmi'}:
        raise ValueError('signal/first-frame stages require a monitor-capable candidate')
    if args.stage == 'hdmi' and mode != 'camera_raw_hdmi':
        raise ValueError('HDMI stage requires the combined camera_raw_hdmi Overlay before download')
    validation = validate_overlay_contract(config, mode)
    write_json(output / 'artifact_validation.json', validation)
    from pynq import Overlay, MMIO
    # The exact same object is passed to the capture transport; it must not
    # reload the PL between signal verification and a first capture.
    scene_phase = args.stage.startswith('scene-')
    if scene_phase:
        from pynq import PL
        active_bit = Path(PL.bitfile_name or '')
        if not active_bit.is_file() or hashlib.sha256(active_bit.read_bytes()).hexdigest() != hashlib.sha256(Path(validation['bitfile']).read_bytes()).hexdigest():
            raise RuntimeError('scene capture requires the already running verified raw Overlay; no reload permitted')
    overlay = Overlay(validation['bitfile'], download=not scene_phase)
    diag = MMIO(0x43c10000, 0x10000)
    control = MMIO(0x43c00000, 0x10000)
    runtime = validate_runtime_contract(control, diag, overlay_mode=mode)
    if not runtime['capabilities'] & 0x20:
        raise RuntimeError('Overlay has no independent signal monitor')
    if not scene_phase:
        release_camera(diag)
    camera_control = verify_camera_control(diag, output)
    registers = camera_control['registers']
    pclk = camera_control['pclk_hz']
    width, height = registers['0x4'], registers['0x3']
    try:
        snapshot = run_monitor(diag)
    except Exception:
        write_json(output / 'monitor_failure_snapshot.json', {
            name: int(diag.read(offset)) for name, offset in MON_REGISTERS.items()
            if name != 'control'})
        raise
    signals = assess_signals(snapshot, width=width, height=height, pclk_hz=min(pclk))
    artifact_hashes = {name: hashlib.sha256(Path(validation[key]).read_bytes()).hexdigest()
                       for name, key in [('bit', 'bitfile'), ('hwh', 'hwh'), ('manifest', 'manifest')]}
    signals.update(artifact_sha256=artifact_hashes, runtime_contract=runtime,
                   overlay_mode=mode, camera_control_verified=True,
                   no_dma_during_monitor=True, no_sensor_mode_writes=True)
    write_json(output / 'signals.json', signals)
    if not signals['camera_signal_verified']:
        return dict(signals, stage_passed=False)
    # The confirmed run config is evidence-bound. Historical/pending configs
    # and manifests never acquire a fabricated hardware confirmation flag.
    confirmed = dict(config, hardware_confirmed=True, width=width, height=height,
                     fv_active=1, lv_active=1, pclk_hz=min(pclk),
                     frame_period_s=signals['frame_period_s'],
                     gate_a_evidence=str((output / 'signals.json').resolve()),
                     gate_a_sha256=hashlib.sha256((output / 'signals.json').read_bytes()).hexdigest())
    import yaml
    (output / 'confirmed_config.yaml').write_text(yaml.safe_dump(confirmed, sort_keys=False), encoding='utf-8')
    if args.stage == 'signals':
        return dict(signals, stage_passed=True)
    transport = controller = None
    records = []
    try:
        transport = PynqTransport(validation['bitfile'], validation['hwh'], validation['manifest'],
                                  frame_width=width, frame_height=height, overlay_mode=mode, overlay=overlay)
        controller = P2Controller(transport)
        deadline = max(1., 3 * signals['frame_period_s'] + .1)
        controller.mmio.write(0x50, int(deadline * 100000000))
        controller.default_timeout_s = deadline + 1
        # First capture + ten independent smoke requests. No retry hides a
        # failed transfer. The scene-change stage is a separate manual gate.
        count = 3 if scene_phase else (11 if args.stage == 'first-frame' else 1)
        previous = None
        for index in range(count):
            t0 = time.monotonic()
            frame = controller.capture_raw()
            identity = (int(diag.read(0x44)), int(diag.read(0x48)))
            if previous is not None and identity <= previous:
                raise RuntimeError('repeated/out-of-order accepted capture identity')
            if identity != (controller.mmio.epoch, controller.mmio.request_id):
                raise RuntimeError('published raw capture does not match accepted request')
            previous = identity
            errors = int(diag.read(0x50)) | int(control.read(0x30))
            if errors:
                raise RuntimeError(f'PL error after completed DMA: {errors:#x}')
            stats = export_gray(frame.pixels, width=width, height=height,
                                output=output / f'frame_{index:03d}', request_id=identity[1])
            pl_low = int(control.read(0x38))
            pl_high = int(control.read(0x3c))
            record = dict(index=index, epoch=identity[0], request_id=identity[1],
                          session_id=controller.session_id,
                          host_completed_utc=datetime.now(timezone.utc).isoformat(),
                          pl_counter_after_dma=pl_low | (pl_high << 32),
                          counter_semantics='100 MHz free-running LOW/HIGH snapshot read after DMA completion; not exposure/request start',
                          elapsed_s=time.monotonic()-t0, actual_bytes=len(frame.pixels),
                          sha256=stats['raw_sha256'], dma_status=int(transport.raw_dma.mmio.read(0x34)),
                          dma_actual_length=int(transport.raw_dma.mmio.read(0x58)),
                          pl_status=int(control.read(0x04)), capture_errors=int(diag.read(0x50)))
            records.append(record)
            write_json(output / 'capture_records.json', records)
        result = {'stage_passed': True, 'status': 'first_gray_frame_digital_verified',
                  'camera_signal_verified': True, 'camera_input_verified': False,
                  'first_gray_frame_digital_verified': True, 'first_gray_frame_verified': False,
                  'raw_dma_smoke_verified': count == 11, 'raw_dma_board_verified': False,
                  'requests': count, 'captures': records,
                  'pending': ['user confirms recognizable scene and orientation',
                              'scene-change capture', '1000-request raw acceptance']}
        if scene_phase:
            result.update(scene_phase=args.stage.removeprefix('scene-'), scene_change_verified=False, reused_loaded_overlay=True, no_camera_reset=True)
        if args.stage == 'hdmi':
            if mode != 'camera_raw_hdmi':
                raise ValueError('HDMI requires the camera/HDMI combined Overlay')
            from structured_light.p2.hdmi_output import create_output, colorbar_bgr, PIXEL_HZ, REFRESH_HZ
            display = create_output(overlay, validation['manifest'], background_owner=True,
                                    owner_log=output / 'hdmi_owner.log')
            hdmi = {'status': 'started', 'pixel_clock_hz_derived': PIXEL_HZ,
                    'refresh_hz_derived': REFRESH_HZ, 'physical_monitor_verified': False,
                    'memory_format': 'BGRX32', 'stream_format': 'BGR24', 'updates': [],
                    'buffer_owner_pid': display.owner_pid}
            write_json(output / 'hdmi.json', hdmi)
            try:
                display.configure()
                hdmi['updates'].append(dict(kind='synthetic_test_pattern', **display.show(colorbar_bgr())))
                write_json(output / 'hdmi.json', hdmi)
                print('HDMI_TEST_PATTERN_ACTIVE', flush=True)
                time.sleep(3)
                hdmi['updates'].append(dict(kind='real_camera_initial', **display.show(
                    (output / 'frame_000/display_bgr.bin').read_bytes())))
                write_json(output / 'hdmi.json', hdmi)
                print('HDMI_REAL_CAMERA_INITIAL_ACTIVE', flush=True)
                time.sleep(2)
                # Capture with the same Overlay while display MM2S remains
                # active on HP1; camera S2MM keeps using HP0.
                updated = controller.capture_raw()
                new_identity = (int(diag.read(0x44)), int(diag.read(0x48)))
                if new_identity <= previous:
                    raise RuntimeError('HDMI recapture repeated accepted request identity')
                if new_identity != (controller.mmio.epoch, controller.mmio.request_id):
                    raise RuntimeError('HDMI recapture does not match accepted request')
                stats = export_gray(updated.pixels, width=width, height=height,
                    output=output / 'frame_001', request_id=new_identity[1])
                if int(diag.read(0x50)) | int(control.read(0x30)):
                    raise RuntimeError('camera PL error during simultaneous HDMI recapture')
                hdmi['recapture'] = dict(epoch=new_identity[0], request_id=new_identity[1],
                    session_id=controller.session_id,
                    host_completed_utc=datetime.now(timezone.utc).isoformat(),
                    actual_bytes=len(updated.pixels), raw_sha256=stats['raw_sha256'],
                    dma_actual_length=int(transport.raw_dma.mmio.read(0x58)),
                    dma_status=int(transport.raw_dma.mmio.read(0x34)))
                hdmi['updates'].append(dict(kind='real_camera_recapture', **display.show(
                    (output / 'frame_001/display_bgr.bin').read_bytes())))
                hdmi['status'] = 'digital_output_and_recapture_verified'
                hdmi['coexistence_digital_verified'] = True
                hdmi['display_hold_s'] = 45
                write_json(output / 'hdmi.json', hdmi)
                print('HDMI_REAL_CAMERA_RECAPTURE_ACTIVE_45_SECONDS', flush=True)
                # Hold ownership of the DMA allocation while the user observes
                # the screen; then stop safely instead of leaving a freed buffer.
                for _ in range(45):
                    time.sleep(1)
                    if int(display.dma.read(4)) & 0xff0:
                        raise RuntimeError('VDMA error during static display hold')
            finally:
                display.close()
                hdmi['buffer_released_after_confirmed_halt'] = True
                write_json(output / 'hdmi.json', hdmi)
            result['hdmi'] = hdmi
            result['hdmi_display_verified'] = False
            result['pending'].append('user confirms actual monitor image')
        return result
    except Exception:
        write_json(output / 'failure_snapshot.json', {
            'diagnostic': {hex(o): int(diag.read(o)) for o in (8, 12, 0x18, 0x1c, 0x44, 0x48, 0x50)},
            'control': {hex(o): int(control.read(o)) for o in (4, 0x30)},
            'recovery': getattr(transport, 'last_recovery', {})})
        raise
    finally:
        if controller is not None:
            controller.close()
        elif transport is not None:
            transport.close()
