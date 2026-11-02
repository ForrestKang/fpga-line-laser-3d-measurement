"""One real-camera session: laser-on trial, manual off contrast, ten on pairs."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid


def write_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + '\n', encoding='utf-8')
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=['on_off_on', 'handheld', 'first_frame'], default='on_off_on')
    args = parser.parse_args()
    source = args.source_root.resolve(strict=True)
    sys.path[:0] = [str(source/'board/scripts'), str(source/'src/python')]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock = open('/tmp/sl_camera_preview.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    nonce = uuid.uuid4().hex
    record = {'schema': 'laser_pair_sequence_v1', 'nonce': nonce,
              'hardware': 'real_pynq_z2', 'source_root': str(source),
              'stage': 'laser_on_off_same_frame', 'phase': 'initializing',
              'laser_control': 'independent supply switched by human; no FPGA GPIO',
              'mode': args.mode,
              'no_hidden_retry': True, 'captures': [],
              'same_frame_profile_verified': False, 'real_laser_profile_verified': False,
              'started_board_utc': datetime.now(timezone.utc).isoformat()}
    dashboard = None
    stopped = False
    def terminate(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    def publish(phase):
        record['phase'] = phase
        record['updated_board_utc'] = datetime.now(timezone.utc).isoformat()
        write_json(output/'live_status.json', record)
        print(json.dumps({'phase': phase, 'captures': len(record['captures'])}), flush=True)
    def confirm(phase):
        publish(phase)
        # Existing nonce-bound confirmation; never infer an operator action.
        # Poll in one-second bounded slices to honor cancellation too.
        deadline = time.monotonic() + 600
        path = output/(phase+'_confirmation.json')
        while not path.is_file():
            if stopped or (output/'stop.request').exists():
                raise RuntimeError('operator stopped the laser measurement session')
            if time.monotonic() >= deadline:
                raise TimeoutError('operator confirmation deadline: '+phase)
            time.sleep(.2)
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('nonce') != nonce or value.get('phase') != phase:
            raise ValueError('operator reply belongs to another phase/session')
        if value.get('approved') is not True or not value.get('user_statement'):
            raise RuntimeError('explicit operator action confirmation required')
        record[phase+'_confirmation'] = value
    def captures(group, count):
        import numpy as np
        for index in range(count):
            if stopped or (output/'stop.request').exists():
                raise RuntimeError('laser capture cancelled')
            publish(group+'_'+str(index))
            item = dashboard.capture_and_compare()
            result, metrics = item['result'], item['metrics']
            if record['captures']:
                last = record['captures'][-1]
                if (result.epoch, result.session_id, result.request_id) != (last['epoch'], last['session_id'], last['request_id']+1):
                    raise RuntimeError('same-session request association changed')
            gray = np.frombuffer((item['directory']/'gray.bin').read_bytes(), np.uint8).reshape(480,752)
            x0,y0,x1,y1 = dashboard.parameters['roi']
            roi = gray[y0:y1,x0:x1]
            value = {'group': group, 'index': index, 'request_id': result.request_id,
                     'epoch': result.epoch, 'session_id': result.session_id,
                     'evidence': str(item['directory'].relative_to(output)),
                     'metrics': metrics, 'hdmi_update': item['hdmi_update'],
                     'roi_saturated_pixels': int((roi == 255).sum()),
                     'roi_pixel_count': int(roi.size)}
            record['captures'].append(value)
            dashboard.save_result()
            write_json(output/'capture_records.json', record['captures'])
    code = 1
    try:
        publish('initializing')
        from profile_dashboard_board import open_profile_dashboard
        dashboard = open_profile_dashboard(args.config, output, hdmi=True, background_owner=True)
        record['session'] = dashboard.board_context
        record['parameters'] = dashboard.parameters
        if args.mode == 'on_off_on':
            captures('laser_on_trial', 3)
            confirm('laser_off')
            captures('laser_off_control', 3)
            confirm('laser_on')
            captures('laser_on_validation', 10)
        elif args.mode == 'handheld':
            # Full-frame hand-held functional test. It cannot establish a
            # fixed-camera on/off contrast or metrological repeatability.
            record['scope'] = 'handheld full-frame digital and visible-stripe function only'
            captures('handheld_laser_validation', 10)
        else:
            record['scope'] = 'one real image only; operator review required before any new batch'
            captures('first_frame_review', 1)
        expected_count = {'on_off_on':16, 'handheld':10, 'first_frame':1}[args.mode]
        record['same_frame_profile_verified'] = len(record['captures']) == expected_count and all(x['metrics']['comparison_passed'] for x in record['captures'])
        if args.mode != 'first_frame':
            confirm('final_visual')
        # The host independently checks on/off identity, saturation and all
        # captured fields before recording the real-laser gate as passed.
        record['phase'] = 'first_frame_waiting_host_review' if args.mode == 'first_frame' else 'digital_verified_pending_host_laser_audit'
        code = 0
    except Exception as error:
        record.update(phase='failed', error_type=type(error).__name__, error=str(error))
    finally:
        if dashboard is not None:
            try:
                dashboard.close()
                record['display_stop_confirmed'] = True
            except Exception as error:
                record.update(cleanup_error=str(error), display_stop_confirmed=False)
                code = 1
        record['finished_board_utc'] = datetime.now(timezone.utc).isoformat()
        write_json(output/'summary.json', record)
        write_json(output/'live_status.json', record)
        lock.close()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
