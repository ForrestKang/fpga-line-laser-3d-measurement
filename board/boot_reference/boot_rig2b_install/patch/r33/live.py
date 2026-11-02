"""R41 live: real camera -> PL capture -> PFO3 laser-line + PL geometry -> HDMI, frame by frame.

Runs on the board against an Overlay that a separate, user-authorised loader already
configured (R40 b10: raw-only capture, PFO3, PL cloud view). Nothing here loads a bit,
powers the motor supply or switches the laser. Motion is separately gated by --allow-motor and a run-bound authorization.

Per frame: P2 capture core grabs one complete 752x480 gray frame over raw_dma (B-stage
driver, camera_raw mode) -> the same bytes go through pfo3_dma into PFO3 (candidates,
PL fixed-point geometry) -> HDMI shows the gray frame with the PL-accepted laser centres;
the PL cloud view draws its own panel into the video stream. BTN0 can start a finite step-and-settle scan only with current-run motor authority.

Calibration: geometry_v1 camera intrinsics + laser plane (measured candidates, nominal 6 mm
board) and a provisional rotation axis. XYZ is labelled accordingly in every output.
"""
from pathlib import Path
import argparse, hashlib, json, math, os, queue, signal, sys, threading, time
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
ALGO = ROOT / 'structured_light_system_algorithm_v1'
for p in (ROOT, ALGO / 'src/python', ALGO / 'board/scripts', ROOT / 'structured_light_system_geometry_v1/src/python'):
    sys.path.insert(0, str(p))

W, H = 752, 480
GRAY_X, GRAY_Y = 24, 120          # left of the PL view window (x 808..1255, y 128..607)
RAW_SOURCE = 1                    # PFO3 identity: actual camera frame


def write_json(path, value):
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, default=str) + '\n', encoding='utf-8')
    tmp.replace(path)


def poll_motion_frame(scan):
    """Discard every moving frame, including the frame observing move completion.

    Completion updates the stopped motor identity and starts StepScan's existing
    settling gate. This frame may have been exposed during motion and must not
    reach PFO3, measurement acceptance, or the next finite move.
    """
    if scan.state != 'SCANNING' or scan.phase != 'move':
        return False
    scan.frame(valid=False)
    return True


def settle_on_pl(scan, packets, mark):
    """Live stream: count settling frames as PL packets instead of ARM-processed frames.

    True while the gate holds. Each settling frame is one more PL packet finished after the
    move; none is decoded. The accepted frame is grabbed after the gate and the PL serves a
    grab with a frame requested after it, so its exposure follows the settling time.
    """
    if scan.state != 'SCANNING' or scan.phase != 'settle':
        return False
    if scan.stable < scan.settle_frames:
        n = packets()
        if mark.get('at') != (scan.request_id, scan.stable):
            mark.update(at=(scan.request_id, scan.stable), packets=n)
        elif n != mark['packets']:
            scan.frame(valid=False)
        return True
    return scan.clock() - scan.ready_at < scan.settle_s


def section_target(sequence, offset, errors, errors0):
    """Section packet number for a live PFO3 sequence.

    The section block publishes only packets without an error footer, so every live error
    packet since live_start puts it one further behind the PFO3 sequence counter.
    """
    return (sequence + offset - (errors - errors0)) & 0xffffffff


class ViewScanSync:
    """Live stream: the PL cloud view builds the scanned object from settled views.

    A scan start clears the view. While the motor moves nothing is inserted; after each move
    the PL geometry gets the new turntable angle (motor steps, exact) before insertion resumes.
    The angle turns only the PL axis coordinates, which only the view uses. When the scan
    ends, insertion stops so the finished cloud stays on screen until the next scan.
    """

    def __init__(self, pfo3, view, calibration, steps_per_rev=3200):
        self.pfo3, self.view, self.calibration = pfo3, view, calibration
        self.rad_per_step = 2 * math.pi / steps_per_rev
        self.epoch, self.at, self.angles = 1, None, 0

    def update(self, scan):
        key = (scan.state, scan.phase, scan.steps)
        if key == self.at:
            return
        before, self.at = self.at, key
        if scan.state != 'SCANNING':
            if before is not None and before[0] == 'SCANNING':
                self.view.set_insert(False)
            return
        if before is None or before[0] != 'SCANNING':
            self.view.set_insert(False)
            self.view.clear()
        if scan.phase == 'move':
            self.view.set_insert(False)
        elif scan.phase == 'settle' and (before[1] != 'settle' or before[2] != scan.steps):
            self.epoch += 1
            self.pfo3.live_reangle(self.calibration, scan.steps * self.rad_per_step, self.epoch)
            self.angles += 1
            self.view.set_insert(True)


def join_live_threads(state, threads, errors):
    """A timed-out join is a failed shutdown, never a successful stopped run."""
    for thread in threads:
        thread.join(timeout=5.)
    alive = [thread.name for thread in threads if thread.is_alive()]
    state['threads_alive_after_join'] = alive
    if alive:
        errors.append('worker shutdown unconfirmed: ' + ', '.join(alive))
    state['thread_errors'] = list(errors)


def close_live_owners(state, display, pfo3, controller, cam):
    """Record close outcomes and fresh capture/DMA readbacks, without retrying."""
    from copy import deepcopy
    from structured_light.p2.contract import DIAG_OFFSETS, REG_STATUS

    alive = set(state.get('threads_alive_after_join', []))
    outcomes, failures = {}, []
    capture_stop = dict(attempted=False, success=False, method='PynqTransport.reset',
                        reason='live_shutdown')
    for name, owner, worker in (('display', display, 'R41_display'), ('pfo3', pfo3, None),
                                 ('controller', controller, 'R41_capture')):
        item = dict(present=owner is not None, attempted=False, success=False)
        outcomes[name] = item
        if owner is None:
            item['reason'] = 'owner not created'
        elif worker in alive:
            item['reason'] = 'worker still alive; owner close skipped'
            failures.append(name + ': ' + item['reason'])
        else:
            item['attempted'] = True
            try:
                if name == 'controller' and cam is not None:
                    # A completed receive owns no buffers, so controller.close
                    # and transport.close may both be no-ops. Explicitly use
                    # the driver's bounded PL STOP -> DMA reset -> ready barrier.
                    capture_stop['attempted'] = True
                    cam.reset(reason='live_shutdown')
                    capture_stop['success'] = True
                owner.close()
                item['success'] = True
            except Exception as error:
                item['error'] = '%s: %s' % (type(error).__name__, error)
                state[name + '_close_error'] = str(error)
                failures.append(name + ': ' + item['error'])
    state['owner_close_results'] = outcomes
    evidence = dict(capture_quiescent=False, sensor_stream_stopped=False,
                    scope='PL capture gate and capture DMA only; sensor stream not stopped',
                    last_recovery={}, dma={}, readback=None)
    if cam is not None and 'R41_capture' not in alive:
        evidence['last_recovery'] = deepcopy(getattr(cam, 'last_recovery', {}))
        for name in ('raw', 'profile'):
            dma = getattr(cam, name + '_dma', None)
            if dma is not None:
                evidence['dma'][name] = dict(reset_confirmed=bool(getattr(dma, 'reset_confirmed', False)),
                    last_stop_evidence=deepcopy(getattr(dma, 'last_stop_evidence', {})))
        if outcomes['controller']['success']:
            try:
                for name in evidence['dma']:
                    dma = getattr(cam, name + '_dma')
                    control = int(dma.mmio.read(dma.control_offset))
                    dma_status = int(dma.mmio.read(dma.status_offset))
                    evidence['dma'][name]['readback'] = dict(control=control, status=dma_status,
                        halted=bool(dma_status & 1), reset_clear=not bool(control & 4),
                        run_clear=not bool(control & 1), error_free=not bool(dma_status & 0x70),
                        monotonic_s=time.monotonic())
                status = int(cam.mmio.read(REG_STATUS))
                ready = int(cam._diag_mmio.read(DIAG_OFFSETS['ready']))
                evidence['readback'] = dict(status=status, diagnostic_ready=ready,
                                           monotonic_s=time.monotonic())
                channels = evidence['dma']
                dma_safe = bool(channels) and all(item['reset_confirmed']
                    and item['last_stop_evidence'].get('reset_observed') is True
                    and item['last_stop_evidence'].get('safe_to_release') is True
                    and all(item['readback'][key] for key in ('halted', 'reset_clear', 'run_clear', 'error_free'))
                    for item in channels.values())
                evidence['capture_quiescent'] = bool((status & 0x5) == 0 and (ready & 1)
                    and not (ready & 4) and dma_safe and capture_stop['success'])
                if not evidence['capture_quiescent']:
                    failures.append('capture stop not confirmed by capture/DMA readback')
            except Exception as error:
                evidence['readback_error'] = '%s: %s' % (type(error).__name__, error)
                failures.append('capture stop readback: ' + evidence['readback_error'])
    elif cam is not None:
        evidence['reason'] = 'capture worker still alive; concurrent hardware readback skipped'
    evidence['bounded_stop'] = capture_stop
    state['camera_stop_evidence'] = evidence
    state['capture_quiescent'] = evidence['capture_quiescent']
    state['hardware_quiescent'] = bool(evidence['capture_quiescent'] and not alive and not failures
                                      and outcomes['pfo3']['success'])
    state['automatic_retry'] = False
    state['hardware_lock_limit'] = ('FD lock is released during cleanup and by process exit; '
                                    'an unconfirmed stop is not made safe by retaining a process lock. '
                                    'No automatic retry; require new hardware admission.')
    return failures


def measurement_quality(result, gray):
    """Expose existing PL decisions and raw saturation; never reclassify points."""
    rows = np.asarray(result.rows)
    decisions = int(result.header['frame_decisions'])
    return dict(raw_sat250=int(np.count_nonzero(gray >= 250)),
                pl_sat250=int(rows[:, 2].sum(dtype=np.uint64)),
                local_measurement_count=int(result.header['measurement_count']),
                measurement_rows=int(np.count_nonzero(rows[:, 6])),
                ambiguous_rows=int(np.count_nonzero(rows[:, 12])),
                frame_decisions=decisions, measurement_frame_usable=bool(decisions & 1),
                preview_frame_usable=bool(decisions & 2))


def live_calibration(intrinsics_path, plane_path, out_path, axis_path=None):
    """Measured camera/laser candidates + turntable axis -> line_laser_camera_live_v1 file.

    axis_path: a checkerboard-on-turntable axis (turntable_axis_v1); without it the axis is
    provisional.
    """
    cam = json.loads(Path(intrinsics_path).read_text())
    plane = json.loads(Path(plane_path).read_text())
    if cam.get('image_size') != [W, H]:
        raise ValueError('camera intrinsics are not for 752x480')
    k = np.asarray(cam['camera_matrix'], np.float64).reshape(3, 3)
    dist = np.asarray(cam['distortion_coefficients'], np.float64).reshape(-1)
    pl = np.asarray(plane['plane_coefficients'], np.float64).reshape(4)
    # Provisional axis until the board-on-turntable calibration: through the point where
    # the optical axis meets the laser plane, parallel to the image vertical (camera -Y).
    t = -pl[3] / pl[2]
    if not np.isfinite(t) or t <= 0:
        raise ValueError('optical axis does not meet the laser plane in front of the camera')
    axis = dict(axis_point_camera_mm=[0.0, 0.0, float(t)], axis_direction_camera=[0.0, -1.0, 0.0],
                radial_reference_camera=[1.0, 0.0, 0.0], rotation_angle_sign=1)
    axis_status, calibration_id = 'PROVISIONAL_NOT_CALIBRATED', 'R41_LIVE_CAMERA_LASER_CANDIDATE_PROVISIONAL_AXIS'
    if axis_path is not None:
        fitted = json.loads(Path(axis_path).read_text())
        if fitted.get('schema') != 'turntable_axis_v1' or fitted.get('coordinate_frame') != 'camera':
            raise ValueError('turntable axis must be a turntable_axis_v1 camera-frame file')
        axis = {key: fitted[key] for key in axis}
        axis_status, calibration_id = 'CALIBRATED_CHECKERBOARD', 'R41_LIVE_CAMERA_LASER_CHECKERBOARD_AXIS'
    record = dict(schema='line_laser_camera_live_v1', evidence_type='MEASURED_CANDIDATE', simulation_only=False,
                  units='mm', image_size=[W, H],
                  calibration_id=calibration_id,
                  camera_matrix=k.tolist(), distortion=dist.tolist(), laser_plane_camera=pl.tolist(), **axis,
                  sources=dict(camera_intrinsics=str(intrinsics_path), laser_plane=str(plane_path),
                               camera_intrinsics_sha256=hashlib.sha256(Path(intrinsics_path).read_bytes()).hexdigest(),
                               laser_plane_sha256=hashlib.sha256(Path(plane_path).read_bytes()).hexdigest()),
                  axis_status=axis_status, board='11x8 inner corners, nominal 6 mm')
    if axis_path is not None:
        record['sources'].update(turntable_axis=str(axis_path),
                                 turntable_axis_sha256=hashlib.sha256(Path(axis_path).read_bytes()).hexdigest())
    write_json(out_path, record)
    return record


def camera_manifest(deploy_manifest, out_path):
    """The deploy manifest declares the combined Overlay; the B-stage camera driver needs
    camera_raw (PROFILE_ENABLE=0) with the same bit/HWH hashes and contract."""
    m = json.loads(Path(deploy_manifest).read_text())
    base = Path(deploy_manifest).parent
    rec = {k: m[k] for k in ('device', 'regmap_version', 'diagnostic_version', 'profile_format_version',
                             'frame_width', 'frame_height', 'carrier_mode', 'preboard_passed',
                             'contract_sha256', 'source_sha256') if k in m}
    rec.update(overlay_mode='camera_raw', overlay='camera_raw', contract_file=str(base / m['contract_file']),
               addresses={k: v for k, v in m['addresses'].items() if k in
                          ('pipeline_control', 'camera_diagnostics', 'raw_dma')},
               files={k: dict(v, path=str(base / v['path']) if 'path' in v else v.get('path'))
                      for k, v in m['files'].items()})
    write_json(out_path, rec)
    return out_path


HEADER_H = 116                    # status strip above the gray image (GRAY_Y = 120)


_HEADER = {}


def render_header(lines):
    """Status lines -> RGB strip. Main thread only (the display thread does no Pillow work); text
    comes from the glyph cache, so after the first frames no Pillow call is made per frame."""
    if not _HEADER:
        from PIL import ImageFont
        from r33.profile_panel import GlyphCache
        _HEADER.update(font=ImageFont.load_default(), glyphs=GlyphCache())
    out = np.empty((HEADER_H, 1280, 3), np.uint8)
    out[:] = (0x10, 0x18, 0x22)
    for i, (text, colour) in enumerate(lines[:6]):
        _HEADER['glyphs'].text(out, (24, 6 + 18 * i), text, colour, _HEADER['font'])
    return out


class LiveDisplay:
    """Triple-buffered HDMI from the B-stage bounded output; this module draws the frame."""

    def __init__(self, overlay):
        from pynq import MMIO, allocate
        from structured_light.p2.preview_output import PreviewHDMIOutput
        for name, base in (('hdmi_vdma', 0x40420000),):
            if int(overlay.ip_dict[name]['phys_addr']) != base:
                raise RuntimeError('HDMI VDMA not at its contract address')
        self.out = PreviewHDMIOutput(MMIO(0x43c30000, 0x10000), MMIO(0x43c20000, 0x10000),
                                     MMIO(0x40420000, 0x10000), allocate)
        self.cleared = set()

    def configure(self):
        self.out.configure()

    def show(self, gray, centres, header, panel=None):
        """Numpy only: gray frame, laser centres, a pre-rendered header strip and panel images."""
        from r33.profile_panel import CARD_Y, PLOT_X, PLOT_Y, blit
        o = self.out
        if o.geometry is None:
            o._initialize(W, H, 0, False)
        o._status()
        reg = int(o.dma.read(0x28))
        if (reg >> 16) & 31 != o.current or reg & 31 != o.current:
            raise RuntimeError('unexpected HDMI active-store ownership')
        target = (o.current + 1) % 3
        frame = o.buffers[target]
        words = np.asarray(frame).view(np.uint32).reshape(720, 1280)
        if target not in self.cleared:
            words[:] = 0x00182210          # dark background in the G,B,R,X lane order
            self.cleared.add(target)
        full_page = header.shape == (720, 1280, 3)
        if not full_page:
            np.multiply(gray, 0x010101, out=words[GRAY_Y:GRAY_Y + H, GRAY_X:GRAY_X + W], dtype=np.uint32)
        if not full_page and len(centres):
            ys, xs = centres[:, 0], centres[:, 1]
            for dx in (-1, 0, 1):
                xx = np.clip(xs + dx, 0, W - 1)
                words[GRAY_Y + ys, GRAY_X + xx] = 0x0000ff   # red
        blit(frame, header, 0, 0)
        if not full_page and panel is not None:
            plot, cards = panel
            blit(frame, plot, PLOT_Y, PLOT_X)
            blit(frame, cards, CARD_Y, 0)
        frame.flush()
        o.dma.write(4, 0x1000)
        o.dma.write(0x28, (int(o.dma.read(0x28)) & ~31) | target)
        o._wait_store(target)
        o.current = target
        o.updates += 1
        return o.updates

    def readback_rgb(self):
        """Read the CPU-owned current framebuffer after VDMA store confirmation."""
        # preview_output uses G,B,R,X byte lanes, as does profile_panel.blit.
        frame = np.asarray(self.out.buffers[self.out.current])
        return frame[..., [2, 0, 1]].copy()

    def close(self):
        self.out.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--manifest', type=Path, required=True, help='deploy manifest of the loaded Overlay')
    ap.add_argument('--intrinsics', type=Path, required=True)
    ap.add_argument('--laser-plane', type=Path, required=True)
    ap.add_argument('--turntable-axis', type=Path, default=None,
                    help='checkerboard-calibrated turntable axis (turntable_axis_v1); omitted: provisional')
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--duration-s', type=float, default=60.)
    ap.add_argument('--save-raw-every', type=int, default=0, help='keep every Nth raw frame (0: none)')
    ap.add_argument('--pl-view', action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument('--pl-view-voxel-mm', type=float, default=3.0,
                    help='PL view voxel; the grid is 64x64x96 voxels around the turntable axis')
    ap.add_argument('--pl-view-z0-mm', type=float, default=-150.,
                    help='PL view grid bottom along the axis, from the axis point')
    ap.add_argument('--exposure-rows', type=int, default=None,
                    help='manual exposure start (coarse rows, 1..480); omitted: sensor stays as is')
    ap.add_argument('--gain-code', type=int, default=16, help='analog gain code 16..64 (1..4x)')
    ap.add_argument('--pfo3-bin2', action=argparse.BooleanOptionalAction, default=False,
                    help='b14+: PFO3 detector on 2x horizontally binned pixels (wide / speckled stripes)')
    ap.add_argument('--profile-mode', action=argparse.BooleanOptionalAction, default=False,
                    help='fixed-point profile: step/gap + tolerance, change, quality on HDMI (PL view off)')
    ap.add_argument('--step-nominal-mm', type=float, default=None)
    ap.add_argument('--step-tol-mm', type=float, default=.2)
    ap.add_argument('--gap-nominal-mm', type=float, default=None)
    ap.add_argument('--gap-tol-mm', type=float, default=.2)
    ap.add_argument('--change-tol-mm', type=float, default=.5)
    ap.add_argument('--profile-reference-frames', type=int, default=10, help='0: no change detection')
    ap.add_argument('--profile-z-min-mm', type=float, default=None)
    ap.add_argument('--profile-z-max-mm', type=float, default=None)
    ap.add_argument('--profile-auto-depth-mm', type=float, default=250.,
                    help='without a Z window keep points up to nearest+this (0: all)')
    ap.add_argument('--profile-rows', default=None, help='image row window FIRST:LAST')
    ap.add_argument('--section-backend', choices=('pl', 'arm'), default=None,
                    help='section numbers: declared PL block by default, otherwise ARM; explicit PL never silently falls back')
    ap.add_argument('--pl-measure', action=argparse.BooleanOptionalAction, default=False,
                    help='b12+: PL live distance/size statistics per frame')
    ap.add_argument('--measure-z-origin-mm', type=float, default=0.)
    ap.add_argument('--measure-z-shift', type=int, default=18, help='Z bin = 2^shift/65536 mm (18: 4 mm)')
    ap.add_argument('--pl-ae', action=argparse.BooleanOptionalAction, default=False,
                    help='b11+: PL laser auto-exposure keeps adjusting 0x0B every frame')
    ap.add_argument('--ae-min-rows', type=int, default=16)
    ap.add_argument('--ae-max-rows', type=int, default=480)
    ap.add_argument('--ae-sat-limit', type=int, default=200)
    ap.add_argument('--ae-target-low', type=int, default=200)
    ap.add_argument('--exposure-step', type=float, default=.5,
                    help='multiply exposure rows by this while saturated (0.3..0.95)')
    ap.add_argument('--view-preview', action=argparse.BooleanOptionalAction, default=True,
                    help='PL view and live XYZ include preview-grade points')
    ap.add_argument('--max-saturated', type=int, default=300,
                    help='halve exposure while a probe frame has more pixels >= 250 than this')
    ap.add_argument('--live-stream', action='store_true',
                    help='camera straight into PFO3 in the PL (every frame); ARM samples one packet per loop')
    ap.add_argument('--pipeline', action=argparse.BooleanOptionalAction, default=True,
                    help='capture, PFO3 and HDMI in parallel threads (newest frame wins)')
    ap.add_argument('--pages', default='A,B,C', help='enabled pages: A=live, B=profile, C=object scan')
    ap.add_argument('--button-pages', default='A,B,C', help='BTN1,BTN2,BTN3 direct page mapping')
    ap.add_argument('--initial-page', choices=('A', 'B', 'C'), default=None)
    ap.add_argument('--layout', choices=('pages', 'approved', 'legacy'), default='pages',
                    help='pages: live/section/scan pages; approved: UI13; legacy: status text')
    ap.add_argument('--ui-refresh-s', type=float, default=1.0, help='page redraw period (camera view every frame)')
    ap.add_argument('--motor-power-disconnected', action='store_true',
                    help='record current user confirmation only; never inferred from missing motor permit')
    ap.add_argument('--ui-cycle-test-s', type=float, default=0.,
                    help='explicit UI test only: cycle pages without claiming physical button presses')
    ap.add_argument('--allow-motor', action='store_true', help='requires separate current-run authority JSON')
    ap.add_argument('--motor-authority', type=Path, default=None)
    ap.add_argument('--scan-start-request', type=Path, default=None,
                    help='optional one-shot start JSON bound to this machine, boot and output run')
    ap.add_argument('--scan-views', type=int, choices=(200, 320), default=200)
    ap.add_argument('--scan-settle-frames', type=int, default=2)
    ap.add_argument('--scan-invalid-retries', type=int, default=3,
                    help='frames tried per view before it is taken as preview or skipped')
    ap.add_argument('--section-compare-every', type=int, default=1,
                    help='with the PL section: run the ARM float check every N frames (0: never)')
    ap.add_argument('--scan-preview-min-points', type=int, default=60,
                    help='accept a view with only preview points (marked preview) at this count; 0: off')
    ap.add_argument('--scan-settle-s', type=float, default=.1)
    ap.add_argument('--scan-deadline-s', type=float, default=600., help='absolute finite scan deadline, 1..1800 s')
    args = ap.parse_args(argv)
    if args.live_stream:
        args.pipeline = True            # display thread; frames come from the PL, not a capture thread
    if not .2 <= args.ui_refresh_s <= 5:
        ap.error('--ui-refresh-s must be 0.2..5')
    if args.ui_cycle_test_s and args.ui_cycle_test_s < 2:
        ap.error('--ui-cycle-test-s must be 0 or >=2')
    from r33.buttons import Pages
    from r33.scan_live import StepScan
    try:
        pages = Pages(tuple(args.pages.split(',')), tuple(args.button_pages.split(',')),
                      args.initial_page or ('B' if args.profile_mode else args.pages.split(',')[0]))
        # measured motor steps per turntable revolution (checkerboard axis file), else nominal 3200
        steps_per_turn = (int(json.loads(args.turntable_axis.read_text()).get('steps_per_turn', 3200))
                          if args.turntable_axis else 3200)
        scan = StepScan(views=args.scan_views, settle_frames=args.scan_settle_frames, settle_s=args.scan_settle_s,
                        deadline_s=args.scan_deadline_s, invalid_retries=args.scan_invalid_retries,
                        steps_per_turn=steps_per_turn, axis_calibrated=args.turntable_axis is not None)
    except ValueError as error:
        ap.error(str(error))
    if args.allow_motor and args.motor_authority is None:
        ap.error('--allow-motor requires --motor-authority for this machine, boot and output run')
    if args.motor_authority is not None and not args.allow_motor:
        ap.error('--motor-authority requires explicit --allow-motor')
    if args.scan_start_request is not None and not args.allow_motor:
        ap.error('--scan-start-request requires explicit --allow-motor and current-run authority')
    if not 5 <= args.duration_s <= 1800:
        ap.error('duration must be 5..1800 s')
    profile_cfg = None
    if args.profile_mode or 'B' in pages.pages:
        from r33.profile_live import ProfileConfig
        try:
            rows_win = None if args.profile_rows is None else tuple(int(v) for v in args.profile_rows.split(':'))
            profile_cfg = ProfileConfig(step_nominal_mm=args.step_nominal_mm, step_tol_mm=args.step_tol_mm,
                                        gap_nominal_mm=args.gap_nominal_mm, gap_tol_mm=args.gap_tol_mm,
                                        change_tol_mm=args.change_tol_mm,
                                        reference_frames=args.profile_reference_frames,
                                        z_min_mm=args.profile_z_min_mm, z_max_mm=args.profile_z_max_mm,
                                        auto_depth_mm=args.profile_auto_depth_mm, rows=rows_win)
        except ValueError as error:
            ap.error('profile mode: %s' % error)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    import faulthandler
    fault_log = open(out / 'faulthandler.log', 'w')      # kept open for the whole run
    faulthandler.enable(fault_log, all_threads=True)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append('SIGTERM'))
    signal.signal(signal.SIGINT, lambda *_: stop.append('SIGINT'))

    from structured_light.motion.lifecycle import HardwareProcessLock
    from r33.transport import ActualPLTransportR25, resolve_manifest
    from r33.scope import PFO3LiveScopeR41, PFO3LiveOverlapScopeR41
    from r33.geometry import load_calibration
    state = dict(schema='R41_LIVE_V1', pid=os.getpid(), phase='init', frames=0, motor='idle',
                 laser='user_controlled', started_unix=time.time())
    write_json(out / 'status.json', state)
    lock = HardwareProcessLock(label='R41_LIVE_CAMERA_PL').acquire()
    controller = cam = pfo3 = display = view = None
    ae = last_ae = None
    plm = section = None
    section_log = None
    tracker = basis = panel = None
    stats, points, object_points, scan_frames = [], [], [], []
    buttons = None
    scan_start_request = None
    page_cache = [None, 0., None, 0.]
    renderer = compositor = page_state = None
    ui_events, page_changes, ui_saved_pages = [], [], set()
    if args.layout == 'pages':
        from r33.ui_pages import PageCompositor, PageRenderer
        renderer = compositor = PageCompositor(PageRenderer(), args.ui_refresh_s)
        state['ui'] = dict(version='industrial_pages', default_page='A', refresh_s=args.ui_refresh_s,
                           source='ACTUAL_CAMERA_PL', cycle_test_s=args.ui_cycle_test_s, physical_button_test=False)
    elif args.layout == 'approved':
        from r33.pages_ui13 import PageRenderer
        renderer = PageRenderer()
        state['ui'] = dict(version='UI13', default_page='A', backend=renderer.backend,
                           source='ACTUAL_CAMERA_PL', motor_12v='user_confirmed_disconnected' if args.motor_power_disconnected else 'not_confirmed',
                           cycle_test_s=args.ui_cycle_test_s, physical_button_test=False)
    cloud_frames = [0]

    def pl_overlay(page):
        """Pages A and C leave their point-cloud panel to the FPGA view."""
        return page in ('A', 'C') if compositor is not None else args.layout == 'legacy' and page == 'A'

    rc = 0
    try:
        manifest = resolve_manifest(args.manifest)
        from r33.pl_section import backend_for
        section_backend = backend_for(manifest, args.section_backend) if profile_cfg is not None else 'off'
        if section_backend == 'pl' and profile_cfg.rows is not None:
            raise ValueError('PL section has no image-row ROI; use --section-backend arm')
        state['section_backend'] = section_backend
        from pynq import Overlay, MMIO
        overlay = Overlay(manifest['bit_absolute'], download=False)
        if (manifest.get('buttons') or {}).get('present'):
            from r33.buttons import Buttons
            buttons = Buttons(manifest, MMIO, overlay.ip_dict['pfo3_frontend']['phys_addr'])
        if args.allow_motor:
            from r33.scan_live import hardware_motor
            scan.backend = hardware_motor(overlay, manifest, args.motor_authority, out.name)
            scan.message = 'BTN0 ready: finite step-and-settle scan; ' + scan.axis_note
            if args.scan_start_request is not None:
                from r33.scan_command import ScanStartRequest
                scan_start_request = ScanStartRequest(args.scan_start_request,
                    machine_id=Path('/etc/machine-id').read_text().strip(),
                    boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(), run_id=out.name)
        state['buttons'] = dict(present=buttons is not None, pages=list(pages.pages), mapping=list(pages.mapping))
        state['scan'] = scan.status()

        # Camera gate (B-stage helpers): reset release, SCCB/PCLK, 16-frame FV/LV signal check.
        from verify_p2_board import release_camera
        from gray_capture_board import verify_camera_control
        from structured_light.p2.camera_evidence import assess_signals, run_monitor
        from structured_light.p2.pynq_transport import PynqTransport, validate_runtime_contract
        from structured_light.p2.controller import P2Controller
        diag, control = MMIO(0x43c10000, 0x10000), MMIO(0x43c00000, 0x10000)
        state['runtime_contract'] = validate_runtime_contract(control, diag, overlay_mode='camera_raw')
        release_camera(diag)
        camera = verify_camera_control(diag, out)
        sig = assess_signals(run_monitor(diag, timeout_s=6), width=W, height=H,
                             pclk_hz=min(camera['pclk_hz']), frames=16)
        state.update(camera=camera, signals=sig, phase='camera_gate')
        write_json(out / 'status.json', state)
        if not sig['camera_signal_verified']:
            raise RuntimeError('16-frame camera signal gate failed')

        cam_manifest = camera_manifest(args.manifest, out / 'camera_manifest.json')
        cam = PynqTransport(manifest['bit_absolute'], Path(manifest['bit_absolute']).with_suffix('.hwh'),
                            cam_manifest, frame_width=W, frame_height=H, overlay_mode='camera_raw',
                            overlay=overlay, strict_manifest=False)
        # Allocate during warm-up; capture worker never allocates/frees an XRT BO per frame.
        cam.enable_raw_buffer_reuse()
        # The Overlay stays loaded between live runs, so the capture core's request ID and
        # epoch continue from the previous process, while a new transport expects ID 0. Take
        # up the hardware generation only from an idle core with a non-zero ID (a fresh load
        # reads 0 and keeps the original first-request rule).
        from structured_light.p2.contract import REG_PROFILE_ID, REG_STATUS, DIAG_OFFSETS
        hw_id = int(cam.mmio.read(REG_PROFILE_ID)) & 0xFFFFFFFF
        hw_epoch = int(diag.read(DIAG_OFFSETS['epoch'])) & 0xFFFF
        if hw_id:
            cam.resume_generation(hw_id, hw_epoch)
        state['camera_generation_resumed'] = dict(request_id=hw_id, epoch=hw_epoch, resumed=bool(hw_id))
        controller = P2Controller(cam, default_timeout_s=max(2., 3 * sig['frame_period_s'] + 1))
        # A request cancelled by a previous process leaves sticky capture error flags; record
        # them, then clear once through the driver's own CONTROL clear before the first START.
        from structured_light.p2.contract import REG_ERROR_FLAGS
        state['camera_errors_before_clear'] = dict(status=int(cam.mmio.read(REG_STATUS)),
                                                   error_flags=int(cam.mmio.read(REG_ERROR_FLAGS)))
        controller.clear_errors()
        state['camera_errors_after_clear'] = dict(status=int(cam.mmio.read(REG_STATUS)),
                                                  error_flags=int(cam.mmio.read(REG_ERROR_FLAGS)))
        if args.exposure_rows is not None:
            # Manual exposure (B-stage helper, verified writes), halved until the laser line is
            # not saturated beyond the PFO3 frame gate. Every SCCB command is traced to disk.
            from structured_light.p2.camera_exposure import SCCBAccess, apply_manual
            trace = []
            sccb = SCCBAccess(diag, trace, lambda: write_json(out / 'sccb_trace.json', trace))
            snapshot = {reg: sccb.read(reg) for reg in (0x07, 0x0F, 0xAF, 0x35, 0x0B, 0xD5)}
            state['exposure_snapshot_before'] = {hex(k): v for k, v in snapshot.items()}
            rows, steps = max(1, min(480, int(args.exposure_rows))), []
            for _ in range(1 if args.pl_ae else 7):
                apply_manual(sccb, snapshot, coarse_rows=rows, gain_code=args.gain_code)
                for _settle in range(2):          # the first frame may still carry the old exposure
                    probe = controller.capture_raw()
                sat = int((np.frombuffer(probe.pixels, np.uint8) >= 250).sum())
                steps.append(dict(coarse_rows=rows, saturated_250=sat))
                if sat <= args.max_saturated or rows == 1:
                    break
                rows = max(1, min(rows - 1, int(rows * min(.95, max(.3, args.exposure_step)))))
            state['exposure'] = dict(gain_code=args.gain_code, steps=steps, final_rows=rows,
                                     final_saturated_250=steps[-1]['saturated_250'])
            write_json(out / 'status.json', state)
        if args.pl_ae:
            if args.exposure_rows is None:
                raise ValueError('--pl-ae needs --exposure-rows (manual exposure, sensor AEC off)')
            # Never probe: on an Overlay without the block, 0x60 answers SLVERR (a bus error on Zynq).
            if not (manifest.get('pl_laser_ae') or {}).get('present'):
                raise RuntimeError('loaded Overlay manifest declares no PL laser AE (b11 or later required)')
            diag.write(0x60, 0x200)                              # disabled, settle 2
            diag.write(0x6C, rows)                               # current manual exposure
            diag.write(0x64, (args.ae_max_rows << 16) | args.ae_min_rows)
            diag.write(0x68, (args.ae_target_low << 24) | args.ae_sat_limit)
            diag.write(0x60, 0x203)                              # clear statistics + enable
            if int(diag.read(0x6C)) != rows or not int(diag.read(0x60)) & 1:
                raise RuntimeError('PL AE configuration readback mismatch')
            ae = diag

            def ae_status():
                peak = int(ae.read(0x74)); st = int(ae.read(0x7C))
                return dict(rows=int(ae.read(0x6C)), last_saturated=int(ae.read(0x70)), last_peak=peak & 0xFF,
                            frames=peak >> 16, writes=int(ae.read(0x78)), failures=st >> 16,
                            last_write_failed=bool(st & 4), active=bool(st & 2), enabled=bool(st & 1))
            state['pl_ae'] = dict(start=ae_status(), limits=[args.ae_min_rows, args.ae_max_rows],
                                  sat_limit=args.ae_sat_limit, target_low=args.ae_target_low)

        live_calibration(args.intrinsics, args.laser_plane, out / 'calibration_live.json', args.turntable_axis)
        calibration = load_calibration(out / 'calibration_live.json', admit_measured=True)
        ActualPLTransportR25.scope_class = PFO3LiveOverlapScopeR41 if args.pipeline else PFO3LiveScopeR41
        pfo3 = ActualPLTransportR25(overlay, manifest=manifest, hardware_lock=lock)
        if args.pfo3_bin2:
            # The mode is selected per frame by the header policy revision; an older bit would
            # ignore it and its output would fail the decoder, so require the manifest flag.
            if not (manifest.get('pfo3_binning') or {}).get('present'):
                raise RuntimeError('loaded Overlay manifest declares no PFO3 binning mode (b14 or later)')
            pfo3.binning = True
        state['pfo3_binning'] = bool(args.pfo3_bin2)
        if args.live_stream and not (manifest.get('live_stream') or {}).get('present'):
            # an older bit ignores 0x024 and never streams
            raise RuntimeError('loaded Overlay manifest declares no live camera stream')
        pfo3.configure_geometry(calibration, 0.0, 1)
        # PL camera coordinates never depend on the angle; the PS TurntableModel unrotates
        # settled frames. In a live scan ViewScanSync writes the motor angle so that the PL
        # axis coordinates (cloud view only) turn with the object.
        from structured_light.scan3d.turntable import assumed_turntable
        calibration_json = json.loads((out / 'calibration_live.json').read_text())
        turntable = assumed_turntable(calibration_json['axis_point_camera_mm'],
                                      calibration_json['axis_direction_camera'], steps_per_rev=steps_per_turn,
                                      source='checkerboard axis' if args.turntable_axis else 'R42 provisional axis')
        write_json(out / 'turntable_preview.json', turntable.to_json())
        if compositor is not None:
            from r33.page_state import PageState
            page_state = PageState(run_name=out.name, binned=args.pfo3_bin2, motor_allowed=args.allow_motor,
                                   motor_power_disconnected=args.motor_power_disconnected, turntable=turntable)

        if profile_cfg is not None:
            from r33.profile_live import ProfileTracker, SectionBasis
            from r33.profile_panel import ProfilePanel
            basis = SectionBasis(json.loads((out / 'calibration_live.json').read_text())['laser_plane_camera'])
            tracker, panel = ProfileTracker(profile_cfg), ProfilePanel()
            state['profile'] = dict(config=dict(profile_cfg.__dict__), chinese_font=panel.chinese,
                                    grade='PL preview grade', section='laser plane (camera/laser calibration only)')
            if section_backend == 'pl':
                from r33.pl_section import PLSection
                section = PLSection(manifest, MMIO, overlay.ip_dict['pfo3_frontend']['phys_addr'])
                state['section_measure'] = dict(config=section.configure(basis, profile_cfg, preview=args.view_preview),
                                                comparison='section_compare.jsonl', board_verified=False)
                section_log = open(out / 'section_compare.jsonl', 'x', encoding='utf-8')
        if args.pl_view and (manifest.get('pl_cloud_view') or {}).get('register_window'):
            from r33.pl_view import PLCloudView, default_params
            from r33.ui_pages import PL_WINDOWS
            view = PLCloudView(MMIO, overlay.ip_dict['pfo3_frontend']['phys_addr'])
            # real-object grid: 64x64x96 voxels around the axis; background beyond it is cropped
            grid = dict(voxel_mm=args.pl_view_voxel_mm, radius_mm=32 * args.pl_view_voxel_mm,
                        z0_mm=args.pl_view_z0_mm, height_mm=96 * args.pl_view_voxel_mm)
            view.configure(default_params(window=PL_WINDOWS.get(pages.current, PL_WINDOWS['A']), **grid)
                           if compositor is not None else default_params(**grid))
            view.start(auto_rotate=True, include_preview=args.view_preview)
            view.set_overlay(pl_overlay(pages.current))
        if args.pl_measure:
            # Never probe: without the block, 0x500-0x5ff of the PFO3 window reads zero.
            if not (manifest.get('pl_measure') or {}).get('present'):
                raise RuntimeError('loaded Overlay manifest declares no PL measurement block (b12 or later)')
            from r33.pl_measure import PLMeasure
            plm = PLMeasure(MMIO, overlay.ip_dict['pfo3_frontend']['phys_addr'])
            state['pl_measure'] = dict(config=plm.configure(preview=args.view_preview,
                                                             z_origin_mm=args.measure_z_origin_mm,
                                                             z_shift=args.measure_z_shift),
                                       axis='provisional (not calibrated): height/radius values are indicative')
        display = LiveDisplay(overlay)
        display.configure()
        state.update(phase='running', pipeline=args.pipeline,
                     capture_prefetch='after_processing' if args.pipeline and not args.allow_motor
                     else 'after_dequeue' if args.pipeline else 'serial')
        started = time.monotonic()
        state['running_start_monotonic'] = started
        halt = threading.Event()
        errors = []
        dropped = dict(capture=0, display=0)
        display_ms = []
        def show_frame(gray, centres, image, panel_imgs, page, frame_number, ready_time, captured_at,
                       render_ms=None):
            begin = time.perf_counter()
            ack = display.show(gray, centres, image, panel_imgs)
            end = time.perf_counter()
            display_ms.append(1e3 * (end - begin))
            event = dict(frame=frame_number, page=page, ack=ack,
                         acknowledged_unix=time.time(), ack_monotonic=end,
                         display_ms=1e3 * (end-begin),
                         ready_to_ack_ms=1e3 * (end-ready_time),
                         input_to_ack_ms=1e3 * (end-captured_at))
            if render_ms is not None:
                event['render_ms'] = render_ms
            if renderer is not None and page not in ui_saved_pages and hasattr(display, 'readback_rgb'):
                actual = display.readback_rgb()
                event['framebuffer_equal'] = bool(np.array_equal(actual, image))
                if not event['framebuffer_equal']:
                    raise RuntimeError('UI framebuffer differs after VDMA confirmation')
                actual.tofile(out / ('ui_page_' + page + '.rgb'))
                event['snapshot_sha256'] = hashlib.sha256(actual.tobytes()).hexdigest()
                ui_saved_pages.add(page)
            ui_events.append(event)
        cam_q, disp_q = queue.Queue(maxsize=1), queue.Queue(maxsize=1)

        def put_latest(q, item, key):
            # Newest frame wins: a stale item is dropped, never queued behind.
            while True:
                try:
                    q.put_nowait(item)
                    return
                except queue.Full:
                    try:
                        q.get_nowait()
                        dropped[key] += 1
                    except queue.Empty:
                        pass

        want = threading.Event()
        want.set()

        # Pillow work stays in the main loop and is rate-limited (board: plot 37 ms, cards 83 ms,
        # header 39 ms per update): plot every 0.33 s, cards and status header every 1.0 s.
        plot_cache, cards_cache, header_cache = [None, 0.], [None, 0.], [None, 0.]

        def header_rgb(lines):
            now = time.monotonic()
            if header_cache[0] is None or now - header_cache[1] >= 1.:
                header_cache[:] = [render_header(lines), now]
            return header_cache[0]

        def render_panel(prof):
            if prof is None:
                return None
            summary, reference = prof
            now = time.monotonic()
            if plot_cache[0] is None or now - plot_cache[1] >= .33:
                plot_cache[:] = [panel.plot(summary, reference), now]
            if cards_cache[0] is None or now - cards_cache[1] >= 1.:
                cards_cache[:] = [panel.cards(summary), now]
            return plot_cache[0], cards_cache[0]

        def capture_loop():
            try:
                while not halt.is_set():
                    if not want.wait(timeout=.2):
                        continue
                    want.clear()
                    t0 = time.perf_counter()
                    frame = controller.capture_raw()
                    put_latest(cam_q, (frame, t0, time.perf_counter()), 'capture')
            except BaseException as error:
                errors.append('capture: %s: %s' % (type(error).__name__, error))
                halt.set()

        def display_loop():
            try:
                while not halt.is_set():
                    try:
                        gray, centres, header, panel_imgs, page, frame_number, ready_time, captured_at, render_ms = disp_q.get(timeout=.2)
                    except queue.Empty:
                        continue
                    show_frame(gray, centres, header, panel_imgs, page, frame_number, ready_time, captured_at, render_ms)
            except BaseException as error:
                errors.append('display: %s: %s' % (type(error).__name__, error))
                halt.set()

        # Warm-up on the main thread: every first-use DMA allocation (raw buffer, PFO3 buffers,
        # HDMI frame stores) happens here, before any worker thread exists.
        warm = controller.capture_raw()
        warm_gray = np.frombuffer(warm.pixels, np.uint8).reshape(H, W)
        pfo3.run(warm_gray, identity=dict(frame_id=0, epoch=1, cfg_version=33, exposure_version=0, motor_id=0,
                                          motor_epoch=0, motor_steps=0, FV_ticks=0, exposure_start=0,
                                          exposure_end=0, source=RAW_SOURCE))
        display.show(warm_gray, np.zeros((0, 2), np.int64), render_header([('R41 LIVE | warm-up frame', 'white')]))
        state['warmup'] = dict(raw_sha256=warm.sha256)
        if section is not None:
            # Warm-up is a real committed PFO3 packet too. Consume its solver
            # publication before any formal packet or capture worker starts;
            # retain the exact-next-packet check rather than skipping a counter.
            state['warmup']['section'] = section.frame()
            state['warmup']['section']['input_frame_id'] = 0
            write_json(out / 'warmup_section.json', state['warmup']['section'])
        threads = []
        live_offset = 0
        settle_mark = {}
        view_sync = (ViewScanSync(pfo3, view, calibration, steps_per_turn)
                     if args.live_stream and view is not None else None)
        if args.live_stream:
            # section packet numbers vs PFO3 sequence numbers, fixed while no packet fails
            live_offset = ((section.packet if section is not None else 0) - pfo3.ctrl.read(0x1c)) & 0xffffffff
            pfo3.live_start()
            state['live_stream'] = dict(started=True, section_offset=live_offset, **pfo3.live_counters())
            threads = [threading.Thread(target=display_loop, name='R41_display', daemon=True)]
        elif args.pipeline:
            threads = [threading.Thread(target=capture_loop, name='R41_capture', daemon=True),
                       threading.Thread(target=display_loop, name='R41_display', daemon=True)]
        for t in threads:
            t.start()
        published = 0.
        i = 1
        last_cycle = 0
        try:
            while (not stop and not halt.is_set() and not (out / 'stop.request').exists()
                   and (scan.state == 'SCANNING' or time.monotonic() - started < args.duration_s)):
                delta = buttons.poll() if buttons is not None else (0, 0, 0, 0)
                previous_page = pages.current
                pages.poll(delta)
                if args.ui_cycle_test_s:
                    cycle = int((time.monotonic() - started) / args.ui_cycle_test_s)
                    if cycle != last_cycle:
                        pages.current = pages.pages[cycle % len(pages.pages)]
                        last_cycle = cycle
                if pages.current != previous_page:
                    page_changes.append(dict(frame=i, before=previous_page, page=pages.current,
                                             button_delta=list(delta),
                                             source='physical_button' if any(delta[1:]) else 'explicit_cycle_test'))
                    if view is not None:
                        if compositor is not None and pages.current in PL_WINDOWS:
                            view.set_window(*PL_WINDOWS[pages.current])
                        view.set_overlay(pl_overlay(pages.current))
                    plot_cache[:], cards_cache[:], header_cache[:], page_cache[:] = [None, 0.], [None, 0.], [None, 0.], [None, 0., None, 0.]
                scan.button(delta[0])
                if scan_start_request is not None:
                    command = scan_start_request.poll(scan, allow_motor=args.allow_motor)
                    if command is not None:
                        state['scan_start_request'] = dict(command, frame=i, monotonic_s=time.monotonic())
                        write_json(out / 'scan_start_request_receipt.json', state['scan_start_request'])
                live_measured = None
                if args.live_stream:
                    if scan.state == 'SCANNING' and scan.phase == 'move':
                        scan.frame(valid=False)            # wait for the move; the PL keeps streaming
                        time.sleep(.005)
                        continue
                    if view_sync is not None:
                        view_sync.update(scan)
                    if settle_on_pl(scan, lambda: pfo3.ctrl.read(0x28), settle_mark):
                        time.sleep(.002)
                        continue
                    motor_identity = scan.identity()
                    identity = dict(frame_id=i, epoch=1, cfg_version=33, exposure_version=0,
                                    FV_ticks=0, exposure_start=0, exposure_end=0, source=RAW_SOURCE, **motor_identity)
                    t0 = time.perf_counter()
                    pixels, primary, live_seq = pfo3.grab_live(cam.raw_dma, identity=identity)
                    t1 = time.perf_counter()
                    if section is not None:
                        target = section_target(live_seq, live_offset, pfo3.ctrl.read(0x2c),
                                                state['live_stream']['errors'])
                        live_measured = section.frame(live=True, target=target)
                    from types import SimpleNamespace
                    frame = SimpleNamespace(pixels=pixels, sha256=hashlib.sha256(pixels).hexdigest())
                elif args.pipeline:
                    frame = None
                    waited = time.monotonic()
                    while frame is None and not halt.is_set():
                        try:
                            frame, t0, t1 = cam_q.get(timeout=.1)
                            if args.allow_motor:
                                # Preserve the existing step/identity schedule in motion mode.
                                want.set()
                        except queue.Empty:
                            if time.monotonic() - waited > 3.:
                                raise RuntimeError('no camera frame for 3 s')
                    if frame is None:
                        break
                else:
                    t0 = time.perf_counter()
                    frame = controller.capture_raw()
                    t1 = time.perf_counter()
                gray = np.frombuffer(frame.pixels, np.uint8).reshape(H, W)
                if not args.live_stream and poll_motion_frame(scan):
                    motion = dict(frame=i, accepted=False, pfo3_processed=False,
                                  discard_reason='motion_or_completion', phase=scan.phase,
                                  raw_sha256=frame.sha256, **scan.identity())
                    scan_frames.append(motion)
                    state.update(scan=scan.status(), motor=scan.state, page=pages.current,
                                 frames=i, processing='motion_capture_only', last_motion=motion)
                    state['motion_capture_frames'] = state.get('motion_capture_frames', 0) + 1
                    with (out / 'motion_frames.jsonl').open('a', encoding='utf-8') as log:
                        log.write(json.dumps(motion) + '\n')
                    write_json(out / 'status.json', state)
                    # Retain the last display while motion is in flight. No new
                    # measurement or page framebuffer is produced for this raw frame.
                    i += 1
                    continue
                if args.live_stream:
                    t_pl = t0
                    state['processing'] = 'PFO3_LIVE'
                    result, receipt = pfo3.decode_live(primary, identity=identity, sequence=live_seq)
                else:
                    t_pl = time.perf_counter()
                    state['processing'] = 'PFO3'
                    motor_identity = scan.identity()
                    identity = dict(frame_id=i, epoch=1, cfg_version=33, exposure_version=0,
                                    FV_ticks=0, exposure_start=0, exposure_end=0, source=RAW_SOURCE, **motor_identity)
                    result, primary, receipt, _ = pfo3.run(gray, identity=identity)
                t2 = time.perf_counter()
                accepted = result.accepted('measurement')
                rec = result.records[accepted]
                centres = np.column_stack(((rec[:, 1]).astype(np.int64),
                                           ((rec[:, 4].astype(np.int64) + 2048) >> 12))) if len(rec) else np.zeros((0, 2), np.int64)
                centres = centres[(centres[:, 0] >= 0) & (centres[:, 0] < H) & (centres[:, 1] >= 0) & (centres[:, 1] < W)]
                geo = np.asarray(result.geometry_records)
                # flags = (reason<<16)|valid(0x100)|preview(2)|measurement(1); measurement is a
                # subset of preview-grade points.
                flags = geo[:, 7] if len(geo) else np.zeros(0, np.uint32)
                meas = (flags & 0x101) == 0x101
                prev = (flags & 0x102) == 0x102
                show = prev | meas if args.view_preview else meas
                xyz = geo[show, 1:4].view(np.int32).astype(np.float64) / 65536 if len(geo) else np.zeros((0, 3))
                xyz_meas = int(meas.sum())
                quality = measurement_quality(result, gray)
                state['measurement_quality'] = quality
                if len(xyz):
                    points.append(np.column_stack((xyz, meas[show].astype(np.float64))))
                preview_points = int((prev & ~meas).sum())
                preview_valid = (args.scan_preview_min_points > 0 and quality['preview_frame_usable']
                                 and preview_points >= args.scan_preview_min_points)
                accepted_scan = scan.frame(valid=bool(xyz_meas), preview_valid=preview_valid)
                if accepted_scan or scan.phase == 'skipped':
                    # identity used for this frame predates poll completion, so a frame
                    # completing a move is discarded by StepScan. Accepted frames must
                    # bind exactly the known stopped motor generation.
                    if motor_identity != scan.identity():
                        raise RuntimeError('scan frame identity changed during acceptance')
                if accepted_scan:
                    object_xyz = turntable.to_object_frame(xyz, scan.steps)
                    object_points.append(np.column_stack((object_xyz, meas[show].astype(np.float64))))
                scan_frames.append(dict(frame=i, accepted=accepted_scan, angle_deg=360.*motor_identity['motor_steps']/steps_per_turn,
                                        phase=scan.phase,
                                        view_decision=scan.last_view if scan.phase in ('accepted', 'skipped') else None,
                                        measurement_points=xyz_meas, **motor_identity))
                fps = len(stats) / max(1e-9, time.monotonic() - started)
                lines = [(f'R41 LIVE | frame {i} | {fps:.2f} fps | capture {1e3*(t1-t0):.1f} ms | PL {1e3*(t2-t_pl):.1f} ms'
                          + (' | pipelined' if args.pipeline else ''), 'white'),
                         (f'PL laser centres {len(centres)} | PL XYZ {len(xyz)} (measurement {xyz_meas}) | motor {scan.state}/{scan.phase}', '#60ff90'),
                         ('XYZ: measured camera/laser candidates, AXIS UNCALIBRATED (indicative only)', '#ffd060')]
                lines.append((f'PAGE {pages.current} | BTN1 live / BTN2 profile / BTN3 object scan | BTN0 start/STOP', '#80c8ff'))
                prof = None
                if tracker is not None:
                    if (out / 'reference.request').exists():
                        (out / 'reference.request').unlink()
                        tracker.reset_reference()
                        state.setdefault('profile_reference_resets', []).append(i)
                    from r33.profile_live import assess_quality, log_view, measure_frame, point_rows_from
                    ids = geo[show, 0].astype(np.int64) if len(geo) else np.zeros(0, np.int64)
                    point_rows = point_rows_from(result.records, ids)
                    if section is None:
                        pf = measure_frame(xyz, basis, profile_cfg, point_rows, result.rows)
                        pf['backend'] = 'arm'
                    else:
                        from r33.pl_section import SectionWindowMismatch, comparison, profile_frame
                        grade = prev if args.view_preview else meas
                        section_xyz = geo[grade, 1:4].view(np.int32).astype(np.float64) / 65536
                        section_rows = point_rows_from(result.records, geo[grade, 0].astype(np.int64))
                        effective = section.effective_config()
                        measured = live_measured if args.live_stream else section.frame()
                        measured['preview'] = args.view_preview
                        try:
                            pf = profile_frame(measured, section_xyz, basis, effective, section_rows, result.rows)
                        except SectionWindowMismatch:
                            if not args.live_stream:
                                raise
                            # a live packet computed with the previous Z window: this frame's
                            # section comes from the ARM path on the same points and window
                            pf = measure_frame(section_xyz, basis, effective, section_rows, result.rows)
                            pf['backend'] = 'arm_window_fallback'
                            state['section_window_fallbacks'] = state.get('section_window_fallbacks', 0) + 1
                        next_z = section.next_window(section_xyz)
                        if args.section_compare_every and i % args.section_compare_every == 0:
                            # sampled ARM float check of the PL result (costly on the ARM)
                            arm = measure_frame(section_xyz, basis, effective, section_rows, result.rows)
                            section_log.write(json.dumps(comparison(i, pf, arm, next_z_max_mm=next_z),
                                                         ensure_ascii=False, default=str) + '\n')
                            section_log.flush()
                    summary = tracker.update(pf, assess_quality(result.rows, pf['points'], width_scale=2 if args.pfo3_bin2 else 1,
                                                                row_window=pf['window'].get('rows')))
                    prof = (summary, tracker.reference)
                    pv = log_view(summary)
                    st = summary['step']
                    lines.append((f'PROFILE MODE | window {pf["points"]} pts | step '
                                  + ('--' if st['value'] is None else f'{st["value"]:.2f} mm {st["status"]}')
                                  + f' | quality {summary["quality"]["level"]} | noise {pf["noise_mm"]:.2f} mm',
                                  '#ffa040'))
                m = plm.frame() if plm is not None else None
                if m is not None and m['points']:
                    lines.append((f'PL MEASURE | {m["points"]} pts | distance median '
                                  f'{m.get("z_median_mm", float("nan")):.1f} mm (min {m["z_min_mm"]:.1f}, '
                                  f'max {m["z_max_mm"]:.1f}) | profile X {m["x_max_mm"] - m["x_min_mm"]:.1f} '
                                  f'x Y {m["y_max_mm"] - m["y_min_mm"]:.1f} mm', '#ffa0ff'))
                if ae is not None and i % 5 == 0:
                    a = ae_status()
                    last_ae = a
                    lines.append((f'PL AUTO-EXPOSURE | rows {a["rows"]} | saturated {a["last_saturated"]} | '
                                  f'peak {a["last_peak"]} | writes {a["writes"]} | failures {a["failures"]}', '#80c8ff'))
                elif ae is not None and last_ae is not None:
                    lines.append((f'PL AUTO-EXPOSURE | rows {last_ae["rows"]} | saturated {last_ae["last_saturated"]} | '
                                  f'peak {last_ae["last_peak"]} | writes {last_ae["writes"]}', '#80c8ff'))
                if args.pipeline and not args.allow_motor and not halt.is_set():
                    # In static mode, prefetch only after PL/section processing. The
                    # next exposure then overlaps page rendering instead of waiting
                    # through a whole additional processing period in cam_q.
                    want.set()
                panel_imgs = None
                if renderer is None and pages.current == 'B':
                    panel_imgs = render_panel(prof)
                elif renderer is None and pages.current == 'C':
                    now_panel = time.monotonic()
                    if page_cache[0] is None or now_panel - page_cache[1] >= .33:
                        from r33.scan_live import scan_panel
                        cards_due = page_cache[2] is None or now_panel - page_cache[3] >= 1.
                        plot_rgb, cards_rgb = scan_panel(scan, object_points, render_cards=cards_due)
                        page_cache[0:2] = [plot_rgb, now_panel]
                        if cards_due:
                            page_cache[2:4] = [cards_rgb, now_panel]
                    panel_imgs = page_cache[0], page_cache[2]
                # Page A must also clear the old PS panel, including when no PL overlay exists.
                if panel_imgs is None:
                    from r33.profile_panel import PLOT_H, PLOT_W, CARD_H
                    panel_imgs = (np.zeros((PLOT_H, PLOT_W, 3), np.uint8), np.zeros((CARD_H, 1280, 3), np.uint8))
                frame_render_ms = None
                if compositor is not None:
                    from r33.profile_live import assess_quality
                    page_state.frame(number=i, fps=fps, capture_ms=1e3*(t1-t0), frame_ms=1e3*(t2-t_pl),
                                     cycles=int(result.header['processing_cycles_lo'])
                                     + (int(result.header['processing_cycles_hi']) << 32),
                                     gray=gray, centres=centres, rows=result.rows, points=len(xyz),
                                     quality=assess_quality(result.rows, len(xyz),
                                                            width_scale=2 if args.pfo3_bin2 else 1),
                                     exposure_rows=last_ae['rows'] if last_ae else None, captured_at=t0)
                    if prof is not None:
                        page_state.profile(prof[0]['frame'], prof[0], profile_cfg, gray, centres)
                    page_state.scan(scan.status(), object_points)
                    render_before = compositor.at
                    header_image = compositor.frame(pages.current,
                                                    lambda: page_state.build(pages.current, cloud_frames[0]),
                                                    gray, centres)
                    if compositor.at != render_before and compositor.render_ms:
                        frame_render_ms = float(compositor.render_ms[-1])
                    if header_image is None:
                        header_image = header_rgb(lines)
                elif renderer is not None:
                    # This is a display-only sample. Full clouds remain in the unchanged
                    # measurement save path. Static camera data are never made into a
                    # fake 360-degree model.
                    cloud = xyz
                    if object_points:
                        recent = object_points[-64:]
                        cloud = np.concatenate(recent)
                        cloud = cloud[::max(1, (len(cloud)+15999)//16000)]
                    ui_state = dict(frame=i, fps=fps, capture_ms=1e3*(t1-t0), pl_ms=1e3*(t2-t_pl),
                                    measurement_points=xyz_meas, preview_points=len(xyz),
                                    total_points=sum(len(p) for p in object_points) if object_points else len(xyz),
                                    scan=scan.status(), raw_source='相机实采 / PL观测',
                                    calibration_label='相机与光平面候选 / 转轴未校准 / 尺寸待验证',
                                    cloud_frame='object' if object_points else 'camera',
                                    display_kind='已拼接观测 / 转轴待验证' if object_points else '静态剖面 / 未旋转',
                                    motor_power_disconnected=args.motor_power_disconnected,
                                    motor_authorized=args.allow_motor)
                    render_begin = time.perf_counter()
                    header_image = renderer.render(pages.current, gray, centres, cloud,
                                                   profile=prof[0] if prof else None, state=ui_state)
                    frame_render_ms = 1e3 * (time.perf_counter() - render_begin)
                else:
                    header_image = header_rgb(lines)
                if args.pipeline:
                    put_latest(disp_q, (gray, centres, header_image, panel_imgs, pages.current, i, t1, t0, frame_render_ms), 'display')
                else:
                    show_frame(gray, centres, header_image, panel_imgs, pages.current, i, t1, t0, frame_render_ms)
                stats.append(dict(frame=i, capture_ms=1e3*(t1-t0), pl_ms=1e3*(t2-t_pl),
                                  capture_started_monotonic=t0, capture_ready_monotonic=t1,
                                  processed_monotonic=t2,
                                  frame_age_ms=1e3*(t2-t1), centres=int(len(centres)), xyz=int(len(xyz)),
                                  xyz_measurement=xyz_meas,
                                  measurement_quality=quality,
                                  measure=None if m is None else {k: m[k] for k in m if k != 'bands'},
                                  profile=pv if prof is not None else None,
                                  raw_sha256=frame.sha256, primary_bytes=len(primary),
                                  page=pages.current, scan_accepted=accepted_scan, **motor_identity))
                if args.save_raw_every and i % args.save_raw_every == 0:
                    (out / f'raw_{i:06d}.gray8').write_bytes(frame.pixels)
                    (out / f'primary_{i:06d}.bin').write_bytes(primary)
                # Start the next finite move only after saving this frame's identity/result.
                scan.advance()
                state['scan'] = scan.status()
                state['motor'] = scan.state
                state['page'] = pages.current
                i += 1
                now = time.monotonic()
                if now - published >= 1:
                    state.update(frames=i, fps=fps, last=stats[-1], elapsed_s=now - started, dropped=dict(dropped))
                    if args.live_stream:
                        state['live_stream'].update(pfo3.live_counters(), elapsed_s=now - started)
                    if view is not None:
                        state['pl_view'] = view.status()
                        cloud_frames[0] = state['pl_view']['frames_committed']
                    if ae is not None:
                        trace = state.setdefault('pl_ae_trace', [])
                        trace.append(dict(t=round(now - started, 2), **ae_status()))
                        del trace[:-120]                    # bounded: status.json is rewritten every second
                    if plm is not None:
                        state['pl_measure']['last'] = plm.frame(bands=True)
                    if tracker is not None and prof is not None:
                        state['profile']['last'] = pv
                        sh = prof[0]['frame']['profile']
                        write_json(out / 'profile_last.json', dict(frame=i, s_mm=sh[:, 0].round(3).tolist(),
                                                                   h_mm=sh[:, 1].round(3).tolist(), summary=pv))
                    write_json(out / 'status.json', state)
                    published = now
        finally:
            # Stop/disarm before waiting for capture/display owners. Their bounded
            # join must never extend a motor permit after camera/PFO3 failure.
            try:
                if scan.state == 'SCANNING':
                    if errors or sys.exc_info()[0] is not None:
                        scan.fail('live camera/PFO3/display failure')
                    else:
                        scan.stop('live run stopped')
            except Exception as error:
                errors.append('scan cleanup: %s' % error)
                rc = 1
            halt.set()
            join_live_threads(state, threads, errors)
            state['dropped'] = dict(dropped)
            if display_ms:
                state['display_median_ms'] = float(np.median(display_ms))
                state['display_updates'] = len(display_ms)
            if compositor is not None and compositor.render_ms:
                ms = np.asarray(compositor.render_ms)
                state['ui']['render_ms'] = dict(median=float(np.median(ms)), p95=float(np.percentile(ms, 95)),
                                                count=len(ms))
        if errors:
            raise RuntimeError('; '.join(errors))
        state['phase'] = 'stopped'
        state['stop_reason'] = stop[0] if stop else ('requested' if (out / 'stop.request').exists() else 'duration')
    except Exception as error:
        try:
            scan.fail(str(error))
        except Exception as cleanup_error:
            state['scan_cleanup_error'] = str(cleanup_error)
        state.update(phase='failed', error_type=type(error).__name__, error=str(error))
        rc = 1
    finally:
        cleanup_errors = []
        try:
            if scan.state == 'SCANNING':
                scan.stop('live run stopped')
            else:
                scan.close()
        except Exception as error:
            state['scan_cleanup_error'] = str(error)
            cleanup_errors.append('scan: ' + str(error))
            rc = 1
        state['scan'] = scan.status()
        state['motor'] = scan.state
        try:
            if ae is not None:
                state['pl_ae']['end'] = ae_status()
                ae.write(0x60, 0x200)
        except Exception as error:
            state['pl_ae_disable_error'] = str(error)
            cleanup_errors.append('pl_ae: ' + str(error))
            rc = 1
        try:
            if plm is not None:
                state['pl_measure']['end'] = plm.frame(bands=True)
                plm.disable()
        except Exception as error:
            state['pl_measure_disable_error'] = str(error)
            cleanup_errors.append('pl_measure: ' + str(error))
            rc = 1
        try:
            if section is not None:
                section.disable()
            if section_log is not None:
                section_log.close()
        except Exception as error:
            state['section_cleanup_error'] = str(error)
            cleanup_errors.append('section: ' + str(error))
            rc = 1
        if cam is not None and hasattr(cam, 'raw_buffer_pool_stats'):
            state['raw_buffer_pool'] = dict(cam.raw_buffer_pool_stats)
        if args.live_stream and pfo3 is not None and getattr(pfo3, 'live', False):
            try:
                pfo3.live_stop()
                state['live_stream'].update(pfo3.live_counters(), stopped=True)
            except Exception as error:
                cleanup_errors.append('live stop: ' + str(error))
        cleanup_errors.extend(close_live_owners(state, display, pfo3, controller, cam))
        if cleanup_errors:
            state.update(phase='failed', cleanup_errors=cleanup_errors, hardware_quiescent=False)
            rc = 1
        if stats:
            arr = {k: np.array([s[k] for s in stats], np.float64) for k in ('capture_ms', 'pl_ms', 'frame_age_ms')}
            state['timing_median_ms'] = {k: float(np.median(v)) for k, v in arr.items()}
            state['frames'] = len(stats)
        if points:
            # columns: x, y, z (camera mm), 1.0 = measurement-grade / 0.0 = preview-grade
            np.save(out / 'xyz_camera_mm.npy', np.concatenate(points))
        if object_points:
            np.save(out / 'xyz_object_mm.npy', np.concatenate(object_points))
        write_json(out / 'scan_frames.json', scan_frames)
        state['page_changes'] = page_changes
        state['ui_display_events'] = len(ui_events)
        if ui_events:
            state['display_timing_ms'] = {key: dict(
                median=float(np.median([e[key] for e in ui_events])),
                p95=float(np.percentile([e[key] for e in ui_events], 95)),
                maximum=float(max(e[key] for e in ui_events)))
                for key in ('display_ms', 'ready_to_ack_ms', 'input_to_ack_ms')}
            state['display_timing_definition'] = dict(
                input_to_ack_ms='capture start to VDMA store confirmation',
                ready_to_ack_ms='camera frame ready to VDMA store confirmation')
        state['ui_snapshots'] = sorted(ui_saved_pages)
        if buttons is not None:
            state['button_counts'] = list(getattr(buttons, 'previous', ()))
        write_json(out / 'ui_display_events.json', ui_events)
        write_json(out / 'frames.json', stats)
        state['finished_unix'] = time.time()
        write_json(out / 'status.json', state)
        write_json(out / 'summary.json', state)
        lock.release()
        faulthandler.disable()
        fault_log.close()
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
