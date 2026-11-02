"""Independent camera signal evidence and lossless first-frame artifacts.

No PYNQ import or hardware access occurs at import time. Monitor statistics
describe sampled digital signals, not an analog timing measurement.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
import time

MON_REGISTERS = {
    'control': 0x80, 'status': 0x84, 'result_id': 0x88,
    'fv_rises': 0x8c, 'fv_falls': 0x90, 'lv_rises': 0x94, 'lv_falls': 0x98,
    'fv_high_cycles': 0x9c, 'fv_low_cycles': 0xa0,
    'lv_high_cycles': 0xa4, 'lv_low_cycles': 0xa8,
    'line_pixels_min': 0xac, 'line_pixels_max': 0xb0,
    'frame_rows_min': 0xb4, 'frame_rows_max': 0xb8,
    'complete_frames': 0xbc, 'frame_period_min_pclk': 0xc0,
    'frame_period_max_pclk': 0xc4, 'pixel_min': 0xc8, 'pixel_max': 0xcc,
    'data_bit_variation_mask': 0xd0, 'error_flags': 0xd4, 'version': 0xd8,
}
MON_ERRORS = ('short_line', 'bad_frame', 'counter_overflow',
              'wrong_polarity_suspect', 'aborted', 'timeout')


def monitor_snapshot(mmio) -> dict:
    """Read one held snapshot, rejecting a concurrent command or publication."""
    before = int(mmio.read(MON_REGISTERS['result_id']))
    status = int(mmio.read(MON_REGISTERS['status']))
    if status & 1 or not status & 8:
        raise RuntimeError('monitor has no completed stable snapshot')
    result = {name: int(mmio.read(offset)) for name, offset in MON_REGISTERS.items()
              if name != 'control'}
    after = int(mmio.read(MON_REGISTERS['result_id']))
    after_status = int(mmio.read(MON_REGISTERS['status']))
    if before != after or result['result_id'] != before or after_status & 1 or not after_status & 8:
        raise RuntimeError('monitor snapshot changed during read')
    return result


def run_monitor(mmio, timeout_s: float = 6.0) -> dict:
    if timeout_s <= 0:
        raise ValueError('monitor timeout must be positive')
    if int(mmio.read(MON_REGISTERS['version'])) != 0x10000:
        raise RuntimeError('independent monitor ABI mismatch')
    if int(mmio.read(MON_REGISTERS['status'])) & 0x11:
        raise RuntimeError('monitor is busy or awaiting camera command acknowledgement')
    previous = int(mmio.read(MON_REGISTERS['result_id']))
    mmio.write(MON_REGISTERS['control'], 1)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = int(mmio.read(MON_REGISTERS['status']))
        result_id = int(mmio.read(MON_REGISTERS['result_id']))
        if result_id != previous and not status & 1 and status & 8:
            return monitor_snapshot(mmio)
        time.sleep(.002)
    mmio.write(MON_REGISTERS['control'], 2)
    raise TimeoutError('independent monitor host deadline; camera may have stopped')


def assess_signals(snapshot: dict, *, width: int, height: int, pclk_hz: int,
                   frames: int = 16) -> dict:
    if width not in (640, 752) or height != 480 or pclk_hz <= 0:
        raise ValueError('expected geometry/PCLK must be explicitly provided')
    errors = [name for bit, name in enumerate(MON_ERRORS)
              if snapshot['error_flags'] & (1 << bit)]
    unknown_errors = snapshot['error_flags'] & ~((1 << len(MON_ERRORS)) - 1)
    if unknown_errors:
        errors.append(f'other_monitor_error_bits={unknown_errors:#x}')
    problems = list(errors)
    for key, expected in (('line_pixels_min', width), ('line_pixels_max', width),
                          ('frame_rows_min', height), ('frame_rows_max', height),
                          ('complete_frames', frames)):
        if snapshot[key] != expected:
            problems.append(f'{key}={snapshot[key]}, expected {expected}')
    period_min = snapshot['frame_period_min_pclk']
    period_max = snapshot['frame_period_max_pclk']
    if not 0 < period_min <= period_max:
        problems.append('invalid frame period')
    if not all(snapshot[k] > 0 for k in ('fv_rises', 'fv_falls', 'lv_rises', 'lv_falls')):
        problems.append('FV/LV edges missing')
    if not all(snapshot[k] > 0 for k in ('fv_high_cycles', 'fv_low_cycles', 'lv_high_cycles', 'lv_low_cycles')):
        problems.append('FV/LV high/low intervals missing')
    if period_min and period_max > period_min * 1.10:
        problems.append('frame period variation exceeds 10 percent; inspect exposure/frame timing')
    return {
        'status': 'camera_signal_verified' if not problems else 'camera_signal_failed',
        'camera_signal_verified': not problems, 'camera_input_verified': False,
        'problems': problems, 'snapshot': snapshot, 'width': width, 'height': height,
        'pclk_hz': pclk_hz, 'fv_active': 1, 'lv_active': 1,
        'frame_period_s': period_max / pclk_hz if pclk_hz else None,
        'data_activity_observed': bool(snapshot['data_bit_variation_mask']),
        'meaning': 'sampled digital FV/LV geometry; not electrical setup/hold proof',
    }


def export_gray(raw: bytes, *, width: int, height: int, output: Path,
                request_id: int, label: str = 'REAL CAMERA CAPTURE') -> dict:
    from PIL import Image, ImageDraw
    import numpy as np
    if len(raw) != width * height:
        raise ValueError('gray payload has wrong actual length')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'gray.bin').write_bytes(raw)
    image = Image.frombytes('L', (width, height), raw)
    image.save(output / 'gray.png')
    with Image.open(output / 'gray.png') as decoded:
        if decoded.mode != 'L' or decoded.tobytes() != raw:
            raise RuntimeError('lossless PNG readback mismatch')
    a = np.frombuffer(raw, dtype=np.uint8).reshape(height, width)
    histogram = np.bincount(a.ravel(), minlength=256)
    stats = {'width': width, 'height': height, 'actual_bytes': len(raw),
             'raw_sha256': hashlib.sha256(raw).hexdigest(), 'request_id': request_id,
             'min': int(a.min()), 'max': int(a.max()), 'mean': float(a.mean()),
             'std': float(a.std()), 'unique_values': int(np.count_nonzero(histogram)),
             'zero_ratio': float((a == 0).mean()), 'saturation_ratio': float((a == 255).mean()),
             'histogram': histogram.tolist(), 'row_means': a.mean(axis=1).tolist(),
             'column_means': a.mean(axis=0).tolist(), 'png_readback_exact': True,
             'visual_scene_verified': False}
    (output / 'gray_statistics.json').write_text(json.dumps(stats, indent=2)+'\n', encoding='utf-8')
    canvas = Image.new('RGB', (1280, 720), (16, 24, 36))
    canvas.paste(image.convert('RGB'), ((1280-width)//2, (720-height)//2))
    draw = ImageDraw.Draw(canvas)
    draw.text((32, 32), f'{label}   {width}x{height}   request {request_id}', fill='white')
    draw.text((32, 680), 'Original 8-bit grayscale | 1:1 pixels | No contrast enhancement', fill='white')
    canvas.save(output / 'preview.png')
    # Channel order is unambiguous for the grayscale image; the annotation
    # canvas is converted explicitly to BGR for the HDMI driver.
    (output / 'display_bgr.bin').write_bytes(np.asarray(canvas)[:, :, ::-1].tobytes())
    return stats
