"""Explicit bounded exposure preparation; no sensor reset, reload, or movement."""
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import sys
import time
import traceback


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root', type=Path, required=True)
    p.add_argument('--exposure-module', type=Path, required=True)
    p.add_argument('--module-sha256', required=True)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--receipt', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    assert hashlib.sha256(a.exposure_module.read_bytes()).hexdigest() == a.module_sha256
    sys.path.insert(0, str(a.source_root / 'src/python'))
    sys.path.insert(0, str(a.source_root / 'board/scripts'))
    spec = importlib.util.spec_from_file_location('exposure_preparation', a.exposure_module)
    exposure = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exposure)
    from structured_light.motion.pynq_runtime import create_stage_b_runtime
    from structured_light.p2.camera_evidence import run_monitor, assess_signals
    from b_finish_static import export_result
    import numpy as np
    from PIL import Image

    out = a.output
    out.mkdir(parents=True, exist_ok=False)
    r = None
    record = {'schema': 'mt9v034_exposure_preparation_v1', 'phase': 'starting',
              'passed': False, 'motion_commands': 0, 'overlay_downloads': 0,
              'hidden_retry': False, 'sensor_reset': False, 'transitions': [],
              'sccb_trace': [], 'module_sha256': a.module_sha256,
              'roi': [470,210,630,405], 'visual_band': [550,224,595,398],
              'preparation_criterion': '0 clipped pixels, band max<=245, median row peak>=60, median peak-background>=30; not accuracy',
              'shutter_sequence_rows': [480,240,120,60,30,15,8,4,2,1]}

    def save():
        (out / 'status.json').write_text(json.dumps(record, indent=2), encoding='utf-8')

    def capture(folder):
        folder.mkdir(exist_ok=False)
        pair = r.session.run_static(raw=False)
        metrics = export_result(pair, folder, 0)
        raw = bytes(pair.raw_bytes)
        assert len(raw) == 360960 and metrics['comparison_passed'] is True
        frame = folder / 'frame_000'
        (frame / 'raw_gray8.bin').write_bytes(raw)
        gray = np.frombuffer(raw,np.uint8).reshape(480,752)
        assert np.array_equal(np.asarray(Image.open(frame / 'gray.png')), gray)
        return gray, metrics

    def quality(gray):
        band = gray[224:398,550:595]
        peaks = band.max(axis=1).astype(float)
        background = np.median(gray[224:398,520:540], axis=1)
        value = {'clipped_pixels': int(np.count_nonzero(band == 255)),
                 'max': int(band.max()), 'median_row_peak': float(np.median(peaks)),
                 'median_peak_minus_background': float(np.median(peaks-background)),
                 'full_clipped_ratio': float(np.mean(gray == 255))}
        value['preparation_passed'] = (value['clipped_pixels']==0 and value['max']<=245
            and value['median_row_peak']>=60 and value['median_peak_minus_background']>=30)
        return value

    save()
    try:
        r = create_stage_b_runtime(a.manifest, stage='static', attach=True,
            loaded_overlay_receipt=a.receipt, output=out/'runtime',
            installation='t02_exposure_prepare', no_display=True,
            stripe_roi=(470,210,630,405), roi=(470,210,630,405), threshold=60)
        record['guard_before'] = r.guard.status()
        assert not record['guard_before']['active'] and not record['guard_before']['step_pin'] and record['guard_before']['enable_pin']
        dev = exposure.SCCBAccess(r.transport._diag_mmio, record['sccb_trace'], save)
        regs = (0,7,0xAF,0x0B,0x35,0xD5,0x0F,0xBA,0xBB)
        backup = {reg: dev.read(reg) for reg in regs}
        assert backup[0] == 0x1324
        record['backup'] = {hex(k):v for k,v in backup.items()}
        record['restore_order'] = ['0x35', '0xb', '0xaf']
        (out/'sensor_backup.json').write_text(json.dumps(record['backup'],indent=2))
        save()  # original settings durable before first write
        for index, rows in enumerate(record['shutter_sequence_rows']):
            record['phase'] = 'preparing'
            item = {'index':index, 'coarse_rows':rows, 'gain_code':16,
                    'settle_wait_s':0.25, 'formal_acceptance_capture':False}
            record['transitions'].append(item);save()
            item['writes'] = exposure.apply_manual(dev,backup,coarse_rows=rows,gain_code=16)
            time.sleep(.25)  # >13 measured 18.5ms frame periods; sensor applies at new frame
            gray, metrics = capture(out/f'prepare_{index:02d}')
            item['capture'] = metrics
            item['quality'] = quality(gray);save()
            if item['quality']['preparation_passed']:
                record['selected'] = {'coarse_rows':rows,'gain_code':16,'gain_x':1.0,
                                      'automatic_a_disabled':True,'preparation_index':index}
                break
            if (item['quality']['clipped_pixels'] == 0
                    and item['quality']['median_row_peak'] < 60):
                raise RuntimeError('unclipped but too dim; stop decreasing exposure and retain preparation evidence')
        else:
            raise RuntimeError('bounded preparation sequence exhausted; no quality pass')

        record['phase'] = 'confirmation'
        monitor = run_monitor(r.transport._diag_mmio)
        pclk = int(r.transport._diag_mmio.read(12))
        record['signals'] = assess_signals(monitor,width=752,height=480,pclk_hz=pclk,frames=16)
        assert record['signals']['camera_signal_verified']
        gray, metrics = capture(out/'confirmation')  # exactly one fresh operator confirmation frame
        record['confirmation'] = {'capture':metrics,'quality':quality(gray)}
        assert record['confirmation']['quality']['preparation_passed']
        record['final_registers'] = {hex(reg):dev.read(reg) for reg in regs}
        assert record['final_registers']['0xaf'] & 3 == 0
        assert record['final_registers']['0xb'] == record['selected']['coarse_rows']
        assert record['final_registers']['0x35'] & 0x7F == 16
        record['passed'] = True
        record['phase'] = 'complete'
    except BaseException as exc:
        record['phase'] = 'failed'
        record['error'] = f'{type(exc).__name__}: {exc}'
        record['traceback'] = traceback.format_exc()
    finally:
        if r:
            record['guard_after'] = r.guard.status()
            record['cleanup'] = r.close()
        save()
    print(json.dumps({k:v for k,v in record.items() if k not in ('sccb_trace','transitions')},ensure_ascii=True))
    return 0 if record['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
