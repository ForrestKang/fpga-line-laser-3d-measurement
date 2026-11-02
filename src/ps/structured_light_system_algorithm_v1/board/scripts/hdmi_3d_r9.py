"""Render a saved R8 view or empty current-calibration-pending view.

Offline by default. Optional HDMI attaches through the existing checked owner;
no FPGA download, camera access or motor operations exist in this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'src/python'))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]/'structured_light_system_geometry_v1/src/python'))
from structured_light.scan3d.hdmi_3d_r9 import ViewSource, load_saved_r8, render_3d_canvas


def _finish_lock(hardware_lock, remote, owner_attempted, receipt):
    if hardware_lock is None:
        return
    if owner_attempted and (remote is None or remote.process.poll() is None):
        # LOCK_UN would unlock the owner's inherited open file description.
        # Close only our handle while an owner may retain live CMA buffers.
        receipt.update(hardware_lock_released=False, external_recovery_required=True,
                       owner_still_alive_or_startup_unconfirmed=True)
        if hardware_lock._handle is not None:
            hardware_lock._handle.close()
            hardware_lock._handle = None
    else:
        hardware_lock.release()
        receipt['hardware_lock_released'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw', type=Path, required=True, help='exact 752x480 gray8 file')
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--saved-r8', type=Path)
    choice.add_argument('--current-pending', metavar='CURRENT_CAPTURE_ID')
    parser.add_argument('--captured-request-id', type=int, help='request ID of the supplied snapshot')
    parser.add_argument('--source-timestamp', help='explicit timestamp of capture; absent means unknown')
    parser.add_argument('--output', type=Path, required=True, help='new PNG, never overwrite')
    parser.add_argument('--manifest', type=Path, help='optional existing active-bit HDMI manifest')
    parser.add_argument('--hold-seconds', type=float, default=10.)
    args = parser.parse_args()
    if not math.isfinite(args.hold_seconds) or not 0 <= args.hold_seconds <= 300:
        parser.error('hold-seconds must be in 0..300')
    if args.captured_request_id is not None and args.captured_request_id < 0:
        parser.error('captured-request-id must be nonnegative')
    report_path = args.output.with_suffix('.json')
    if args.output.exists() or report_path.exists():
        parser.error('output PNG/JSON already exists')
    if args.raw.stat().st_size != 752*480:
        parser.error('raw must be an exact 752x480 gray8 file')
    raw = args.raw.read_bytes()
    if args.saved_r8:
        source, points, faces = load_saved_r8(args.saved_r8)
    else:
        source = ViewSource('current_snapshot', args.current_pending, 'pending')
        points = faces = None
    started = time.perf_counter()
    canvas, receipt = render_3d_canvas(raw, source=source, points=points, triangles=faces)
    receipt.update(render_seconds=time.perf_counter()-started,
                   raw_sha256=hashlib.sha256(raw).hexdigest(), raw_path=str(args.raw.resolve()),
                   overlay_download_count=0, motor_start_count=0, camera_capture_count=0,
                   captured_request_id=args.captured_request_id if args.captured_request_id is not None else 'unknown',
                   source_timestamp=args.source_timestamp if args.source_timestamp else 'unknown',
                   source_file_count=1, display_submissions=0,
                   hardware_executed=False, hdmi_close_confirmed=None)
    if args.saved_r8:
        receipt['geometry_sha256'] = hashlib.sha256(args.saved_r8.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects earlier evidence from accidental replacement.
    ok, encoded = cv2.imencode('.png', canvas)
    if not ok:
        raise RuntimeError('PNG encoding failed')
    with args.output.open('xb') as stream:
        stream.write(encoded.tobytes())
    remote = hardware_lock = None
    owner_attempted = False
    try:
        if args.manifest is not None:
            from structured_light.motion.lifecycle import HardwareProcessLock
            from structured_light.p2.hdmi_owner import RemoteHDMIOutput
            hardware_lock = HardwareProcessLock(label='r9_3d_canvas_only').acquire()
            os.environ['SL_HDMI_PROCESSED_CANVAS'] = '1'
            owner_attempted = True
            remote = RemoteHDMIOutput(args.manifest, owner_log=report_path.with_suffix('.owner.log'),
                                      timeout_s=5., startup_timeout_s=60., preview_only=True,
                                      hardware_lock_fd=hardware_lock._handle.fileno())
            remote.configure()
            receipt['hardware_executed'] = True
            receipt['sink_receipt'] = remote.show_canvas(canvas)
            receipt['display_submissions'] = 1
            receipt['hdmi_acknowledged'] = receipt['sink_receipt'].get('hdmi_acknowledged') is True
            deadline = time.monotonic()+args.hold_seconds
            while time.monotonic() < deadline:
                time.sleep(min(.2, max(0., deadline-time.monotonic())))
    except BaseException as exc:
        receipt['display_error'] = str(exc)
        raise
    finally:
        try:
            if remote is not None:
                try:
                    remote.close()
                    receipt['hdmi_close_confirmed'] = True
                except BaseException as exc:
                    receipt['hdmi_close_confirmed'] = False
                    receipt['close_error'] = str(exc)
                    raise
        finally:
            _finish_lock(hardware_lock, remote, owner_attempted, receipt)
            with report_path.open('x', encoding='utf-8') as stream:
                json.dump(receipt, stream, indent=2, allow_nan=False)
    print(json.dumps(receipt, allow_nan=False))


if __name__ == '__main__':
    main()
