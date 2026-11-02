"""Camera recovery diagnostics, independent of PYNQ, DMA and image algorithms."""
from __future__ import annotations

import time
from typing import Callable

EXPECTED_ID = 0x1324
EXPECTED_PCLK = 24_000_000
DIAG_OFFSETS = (0, 4, 8, 12, 0x10, 0x14, 0x24, 0x28, 0x30, 0x38, 0x3c, 0x44, 0x48, 0x50)


def snapshot(diag, control=None) -> dict:
    result = {'diagnostic': {hex(o): int(diag.read(o)) for o in DIAG_OFFSETS}}
    if control is not None:
        result['control'] = {hex(o): int(control.read(o)) for o in (4, 0x30, 0x60, 0x78)}
    result['interpretation'] = {
        'capture_started_by_this_session': False,
        'datapath_ready': bool(result['diagnostic']['0x8'] & 1),
        'datapath_busy_or_not_ready': bool(result['diagnostic']['0x8'] & 4),
        'measurement_window_completed': bool(result['diagnostic']['0x8'] & 2),
        'sccb_busy': bool(result['diagnostic']['0x38'] & 1),
        'physical_pin_levels_measured': False,
    }
    return result


def verify_runtime(diag, control):
    if int(control.read(0x78)) != 0x10000:
        raise RuntimeError('control VERSION mismatch')
    if int(diag.read(0)) != 0x10000 or int(diag.read(4)) != 0x19:
        raise RuntimeError('original camera_raw diagnostic VERSION/capabilities mismatch')
    if (int(diag.read(0x10)), int(diag.read(0x14))) != (752, 480):
        raise RuntimeError('camera geometry contract mismatch')


def read_sensor(diag, register, record, save: Callable, *, clock=time.monotonic, sleep=time.sleep):
    """One independent request; never scan addresses or retry a failed request."""
    item = {'address_7bit': 0x48, 'register': register, 'status_trace': []}
    record.setdefault('attempts', []).append(item)
    save()
    try:
        if int(diag.read(0x38)) & 1:
            raise RuntimeError('SCCB busy before request')
        diag.write(0x24, 0x48)
        diag.write(0x28, register)
        diag.write(0x34, 1)
        deadline = clock() + .5
        while clock() < deadline:
            status = int(diag.read(0x38))
            if not item['status_trace'] or item['status_trace'][-1] != status:
                item['status_trace'].append(status)
                save()
            if not status & 1 and status & 0xe:
                item.update(status=status, nack=bool(status & 4), timeout=bool(status & 8),
                            done=bool(status & 2), pclk_hz=int(diag.read(12)))
                if status == 2:
                    item['value'] = int(diag.read(0x30))
                save()
                if status != 2:
                    raise RuntimeError(f'SCCB register {register:#x} failed, status={status:#x}')
                return item['value']
            sleep(min(.0005, max(0, deadline - clock())))
        raise TimeoutError(f'SCCB register {register:#x} deadline')
    except Exception as error:
        item['error'] = {'type': type(error).__name__, 'message': str(error)}
        save()
        raise


def recover(diag, control, record, save, *, clock=time.monotonic, sleep=time.sleep):
    """Call only after a fresh known Overlay load; aggregate busy is not active."""
    verify_runtime(diag, control)
    record['after_load'] = snapshot(diag, control)
    save()
    if int(diag.read(0x38)) & 1:
        raise RuntimeError('SCCB busy after fresh Overlay load')
    record['reset_sequence'] = []
    try:
        diag.write(0x3c, 1)
        readback = int(diag.read(0x3c))
        record['reset_sequence'].append({'value': 1, 'readback': readback})
        save()
        if readback != 1:
            raise RuntimeError('RST assert/STB-low control readback mismatch')
        sleep(.001)
    finally:
        # Even an assertion readback error must leave the intended released state.
        diag.write(0x3c, 0)
        record['reset_sequence'].append({'value': 0, 'readback': int(diag.read(0x3c))})
        save()
    if record['reset_sequence'][-1]['readback'] != 0:
        raise RuntimeError('RST release/STB-low control readback mismatch')
    released = clock()
    record['pclk_startup_timeline'] = []
    for target_ms in (10, 30, 100, 300, 1000):
        remaining = released + target_ms / 1000 - clock()
        if remaining > 0:
            sleep(remaining)
        record['pclk_startup_timeline'].append({
            'target_ms': target_ms, 'observed_ms': (clock() - released) * 1000,
            'pclk_hz': int(diag.read(12)), 'diagnostic_status': int(diag.read(8)),
            'camera_control': int(diag.read(0x3c)),
        })
        save()
    value = read_sensor(diag, 0, record, save, clock=clock, sleep=sleep)
    if value != EXPECTED_ID:
        raise RuntimeError(f'ID mismatch: {value:#x}')
    record['initial_id_verified'] = True
    save()


def validate(diag, control, record, save, *, clock=time.monotonic, sleep=time.sleep):
    """No Overlay load or reset; stop and preserve the first failed request."""
    verify_runtime(diag, control)
    if int(diag.read(0x3c)) != 0:
        raise RuntimeError('camera reset/standby is not released')
    record['id_reads'] = []
    for _ in range(20):
        value = read_sensor(diag, 0, record, save, clock=clock, sleep=sleep)
        record['id_reads'].append(value)
        save()
        if value != EXPECTED_ID:
            raise RuntimeError(f'ID stability mismatch: {value:#x}')
    record['pclk_samples'] = []
    start = clock()
    for index in range(20):
        sleep(.05)
        value = int(diag.read(12))
        record['pclk_samples'].append({'index': index, 'elapsed_s': clock() - start, 'pclk_hz': value})
        save()
        if not EXPECTED_PCLK * .99 <= value <= EXPECTED_PCLK * 1.01:
            raise RuntimeError(f'PCLK outside 24 MHz +/-1%: {value}')
    record['window_mode_registers'] = {}
    for register in (1, 2, 3, 4, 5, 6, 7, 0xc, 0x72):
        record['window_mode_registers'][hex(register)] = read_sensor(
            diag, register, record, save, clock=clock, sleep=sleep)
        save()
    record['camera_control_recovered'] = True
    save()


def classify_failure(record):
    pclk = [p['pclk_hz'] for p in record.get('pclk_startup_timeline', [])]
    pclk += [p['pclk_hz'] for p in record.get('pclk_samples', [])]
    if not pclk and 'failure_snapshot' in record:
        pclk = [record['failure_snapshot']['diagnostic']['0xc']]
    attempts = record.get('attempts', [])
    id_ok = any(p.get('register') == 0 and p.get('value') == EXPECTED_ID and p.get('status') == 2
                for p in attempts)
    if not attempts or not pclk:
        return 'initialization_or_contract_failure_not_enough_sensor_evidence'
    if any(pclk) and not id_ok:
        return 'pclk_active_id_failed_check_sccb_conditions'
    if id_ok and not any(pclk):
        return 'id_active_pclk_zero_check_clock_path_and_meter'
    if not any(pclk) and not id_ok:
        return 'id_and_pclk_failed_measure_vcc_then_rst_then_stb'
    return 'intermittent_or_late_failure_preserve_timeline'
