from pathlib import Path
root = Path(__file__).resolve().parent / 'r33'

def edit(name, replacements):
    path = root / name
    data = path.read_bytes().decode('utf-8').replace('\r\n', '\n')
    for old, new in replacements:
        if data.count(old) != 1:
            raise RuntimeError('%s replacement count %d: %r' % (name, data.count(old), old[:100]))
        data = data.replace(old, new)
    path.write_bytes(data.replace('\n', '\r\n').encode('utf-8'))

edit('pl_view.py', [(
    '    def stop_overlay(self):\n        self._write(0x00, CTRL_INSERT)\n',
    '''    def set_overlay(self, enabled):
        """Change only OVERLAY. Never pulse CLEAR or change ingest/preview/rotation."""
        ctrl = self._read(0x00) & ~CTRL_CLEAR
        ctrl = (ctrl | CTRL_OVERLAY) if enabled else (ctrl & ~CTRL_OVERLAY)
        self._write(0x00, ctrl)
        self._ctrl = ctrl

    def stop_overlay(self):
        self.set_overlay(False)
''')])

edit('live.py', [
('    args = ap.parse_args(argv)', '''    ap.add_argument('--pages', default='A,B,C', help='enabled pages: A=live, B=profile, C=object scan')
    ap.add_argument('--button-pages', default='A,B,C', help='BTN1,BTN2,BTN3 direct page mapping')
    ap.add_argument('--initial-page', choices=('A', 'B', 'C'), default=None)
    ap.add_argument('--allow-motor', action='store_true', help='requires separate current-run authority JSON')
    ap.add_argument('--motor-authority', type=Path, default=None)
    ap.add_argument('--scan-views', type=int, choices=(200, 320), default=200)
    ap.add_argument('--scan-settle-frames', type=int, default=2)
    ap.add_argument('--scan-settle-s', type=float, default=.1)
    args = ap.parse_args(argv)
    from r33.buttons import Pages
    from r33.scan_live import StepScan
    try:
        pages = Pages(tuple(args.pages.split(',')), tuple(args.button_pages.split(',')),
                      args.initial_page or ('B' if args.profile_mode else args.pages.split(',')[0]))
        scan = StepScan(views=args.scan_views, settle_frames=args.scan_settle_frames, settle_s=args.scan_settle_s)
    except ValueError as error:
        ap.error(str(error))
    if args.allow_motor and args.motor_authority is None:
        ap.error('--allow-motor requires --motor-authority for this machine, boot and output run')
    if args.motor_authority is not None and not args.allow_motor:
        ap.error('--motor-authority requires explicit --allow-motor')'''),
('    if args.profile_mode:\n', "    if args.profile_mode or 'B' in pages.pages:\n"),
('        args.pl_view = False\n', ''),
('    stats, points = [], []', '    stats, points, object_points, scan_frames = [], [], [], []\n    buttons = None\n    page_cache = [None, 0.]'),
("        overlay = Overlay(manifest['bit_absolute'], download=False)", '''        overlay = Overlay(manifest['bit_absolute'], download=False)
        if (manifest.get('buttons') or {}).get('present'):
            from r33.buttons import Buttons
            buttons = Buttons(manifest, MMIO, overlay.ip_dict['pfo3_frontend']['phys_addr'])
        if args.allow_motor:
            from r33.scan_live import hardware_motor
            scan.backend = hardware_motor(overlay, manifest, args.motor_authority, out.name)
            scan.message = 'BTN0 ready: finite step-and-settle scan; axis uncalibrated'
        state['buttons'] = dict(present=buttons is not None, pages=list(pages.pages), mapping=list(pages.mapping))
        state['scan'] = scan.status()'''),
("        pfo3.configure_geometry(calibration, 0.0, 1)", '''        pfo3.configure_geometry(calibration, 0.0, 1)
        # PL emits camera coordinates at angle zero; only settled frames are unrotated
        # by the existing PS TurntableModel. No guessed angle is written into PL geometry.
        from structured_light.scan3d.turntable import assumed_turntable
        calibration_json = json.loads((out / 'calibration_live.json').read_text())
        turntable = assumed_turntable(calibration_json['axis_point_camera_mm'],
                                      calibration_json['axis_direction_camera'], source='R42 provisional axis')
        write_json(out / 'turntable_preview.json', turntable.to_json())'''),
('''            if (manifest.get('pl_cloud_view') or {}).get('register_window'):
                # The PL view's overlay would cover the profile panel; ingest stays, overlay off.
                from r33.pl_view import PLCloudView
                PLCloudView(MMIO, overlay.ip_dict['pfo3_frontend']['phys_addr']).stop_overlay()
                state['profile']['pl_view_overlay'] = 'off'
''', ''),
('        if args.pl_view:\n', "        if args.pl_view and (manifest.get('pl_cloud_view') or {}).get('register_window'):\n"),
('            view.start(auto_rotate=True, include_preview=args.view_preview)', '''            view.start(auto_rotate=True, include_preview=args.view_preview)
            view.set_overlay(pages.current == 'A')'''),
('''                if args.pipeline:
                    frame = None
''', '''                delta = buttons.poll() if buttons is not None else (0, 0, 0, 0)
                if pages.poll(delta):
                    if view is not None:
                        view.set_overlay(pages.current == 'A')
                    plot_cache[:], cards_cache[:], header_cache[:], page_cache[:] = [None, 0.], [None, 0.], [None, 0.], [None, 0.]
                scan.button(delta[0])
                if args.pipeline:
                    frame = None
'''),
('''                identity = dict(frame_id=i, epoch=1, cfg_version=33, exposure_version=0, motor_id=0, motor_epoch=0,
                                motor_steps=0, FV_ticks=0, exposure_start=0, exposure_end=0, source=RAW_SOURCE)''', '''                motor_identity = scan.identity()
                identity = dict(frame_id=i, epoch=1, cfg_version=33, exposure_version=0,
                                FV_ticks=0, exposure_start=0, exposure_end=0, source=RAW_SOURCE, **motor_identity)'''),
('''                fps = len(stats) / max(1e-9, time.monotonic() - started)''', '''                accepted_scan = scan.frame(valid=bool(xyz_meas))
                if accepted_scan:
                    # identity used for this frame predates poll completion, so a frame
                    # completing a move is discarded by StepScan. Accepted frames must
                    # bind exactly the known stopped motor generation.
                    if motor_identity != scan.identity():
                        raise RuntimeError('scan frame identity changed during acceptance')
                    object_xyz = turntable.to_object_frame(xyz, scan.steps)
                    object_points.append(np.column_stack((object_xyz, meas[show].astype(np.float64))))
                scan_frames.append(dict(frame=i, accepted=accepted_scan, angle_deg=360.*motor_identity['motor_steps']/3200,
                                        phase=scan.phase, **motor_identity))
                fps = len(stats) / max(1e-9, time.monotonic() - started)'''),
("                         ('XYZ: measured camera/laser candidates, provisional axis (not calibrated)', '#ffd060')]", "                         ('XYZ: measured camera/laser candidates, AXIS UNCALIBRATED (indicative only)', '#ffd060')]\n                lines.append((f'PAGE {pages.current} | BTN1 live / BTN2 profile / BTN3 object scan | BTN0 start/STOP', '#80c8ff'))"),
('''                if args.pipeline:
                    put_latest(disp_q, (gray, centres, header_rgb(lines), render_panel(prof)), 'display')
                else:
                    t = time.perf_counter()
                    display.show(gray, centres, header_rgb(lines), render_panel(prof))''', '''                panel_imgs = None
                if pages.current == 'B':
                    panel_imgs = render_panel(prof)
                elif pages.current == 'C':
                    now_panel = time.monotonic()
                    if page_cache[0] is None or now_panel - page_cache[1] >= .33:
                        from r33.scan_live import scan_panel
                        page_cache[:] = [scan_panel(scan, object_points), now_panel]
                    panel_imgs = page_cache[0]
                # Page A must also clear the old PS panel, including when no PL overlay exists.
                if panel_imgs is None:
                    from r33.profile_panel import PLOT_H, PLOT_W, CARD_H
                    panel_imgs = (np.zeros((PLOT_H, PLOT_W, 3), np.uint8), np.zeros((CARD_H, 1280, 3), np.uint8))
                if args.pipeline:
                    put_latest(disp_q, (gray, centres, header_rgb(lines), panel_imgs), 'display')
                else:
                    t = time.perf_counter()
                    display.show(gray, centres, header_rgb(lines), panel_imgs)'''),
('''                                  raw_sha256=frame.sha256, primary_bytes=len(primary)))''', '''                                  raw_sha256=frame.sha256, primary_bytes=len(primary),
                                  page=pages.current, scan_accepted=accepted_scan, **motor_identity))'''),
('''                i += 1
                now = time.monotonic()''', '''                # Start the next finite move only after saving this frame's identity/result.
                scan.advance()
                state['scan'] = scan.status()
                state['page'] = pages.current
                i += 1
                now = time.monotonic()'''),
('''    except Exception as error:
        state.update(phase='failed', error_type=type(error).__name__, error=str(error))''', '''    except Exception as error:
        try:
            scan.fail(str(error))
        except Exception as cleanup_error:
            state['scan_cleanup_error'] = str(cleanup_error)
        state.update(phase='failed', error_type=type(error).__name__, error=str(error))'''),
('''    finally:
        try:
            if ae is not None:''', '''    finally:
        try:
            if scan.state == 'SCANNING':
                scan.stop('live run stopped')
            else:
                scan.close()
        except Exception as error:
            state['scan_cleanup_error'] = str(error)
            rc = 1
        state['scan'] = scan.status()
        try:
            if ae is not None:'''),
("        write_json(out / 'frames.json', stats)", '''        if object_points:
            np.save(out / 'xyz_object_mm.npy', np.concatenate(object_points))
        write_json(out / 'scan_frames.json', scan_frames)
        write_json(out / 'frames.json', stats)'''),
])
