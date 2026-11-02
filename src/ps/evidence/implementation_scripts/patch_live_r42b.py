from pathlib import Path
path = Path(__file__).resolve().parent / 'r33/live.py'
data = path.read_bytes().decode('utf-8').replace('\r\n', '\n')
changes = [
('''    ap.add_argument('--scan-settle-s', type=float, default=.1)''', '''    ap.add_argument('--scan-settle-s', type=float, default=.1)
    ap.add_argument('--scan-deadline-s', type=float, default=600., help='absolute finite scan deadline, 1..1800 s')'''),
('''scan = StepScan(views=args.scan_views, settle_frames=args.scan_settle_frames, settle_s=args.scan_settle_s)''',
 '''scan = StepScan(views=args.scan_views, settle_frames=args.scan_settle_frames, settle_s=args.scan_settle_s,
                        deadline_s=args.scan_deadline_s)'''),
('''    page_cache = [None, 0.]''', '''    page_cache = [None, 0., None, 0.]'''),
('''                   and time.monotonic() - started < args.duration_s):''',
 '''                   and (scan.state == 'SCANNING' or time.monotonic() - started < args.duration_s)):'''),
('''plot_cache[:], cards_cache[:], header_cache[:], page_cache[:] = [None, 0.], [None, 0.], [None, 0.], [None, 0.]''',
 '''plot_cache[:], cards_cache[:], header_cache[:], page_cache[:] = [None, 0.], [None, 0.], [None, 0.], [None, 0., None, 0.]'''),
('''| motor idle', '#60ff90')''', '''| motor {scan.state}/{scan.phase}', '#60ff90')'''),
('''                        page_cache[:] = [scan_panel(scan, object_points), now_panel]
                    panel_imgs = page_cache[0]''', '''                        cards_due = page_cache[2] is None or now_panel - page_cache[3] >= 1.
                        plot_rgb, cards_rgb = scan_panel(scan, object_points, render_cards=cards_due)
                        page_cache[0:2] = [plot_rgb, now_panel]
                        if cards_due:
                            page_cache[2:4] = [cards_rgb, now_panel]
                    panel_imgs = page_cache[0], page_cache[2]'''),
('''        finally:
            halt.set()
            for t in threads:''', '''        finally:
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
            for t in threads:'''),
('''The motor stays idle here.''', '''BTN0 can start a finite step-and-settle scan only with current-run motor authority.'''),
('''powers the motor or switches the laser.''', '''powers the motor supply or switches the laser. Motion is separately gated by --allow-motor and a run-bound authorization.'''),
]
for old,new in changes:
    if data.count(old)!=1: raise RuntimeError(repr(old))
    data=data.replace(old,new)
path.write_bytes(data.replace('\n','\r\n').encode('utf-8'))
