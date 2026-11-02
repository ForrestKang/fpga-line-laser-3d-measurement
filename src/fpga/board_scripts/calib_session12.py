"""Board side (sudo): checkerboard session with the user. Camera only unless BTN0 is pressed.

start   stop the idle demo, run r33.live (release04 runtime, loaded release02 firmware) as the
        transient unit scan3d-calib: HDMI page A for placing the board, every Nth raw frame
        saved; --motor adds the BTN0 standing permission for an axis scan
detect  checkerboard (11x8 inner corners, 6.0 mm) in the newest raw frames: PnP with the
        calibrated intrinsics, reprojection error, pose, and the laser line on the board
        against the calibrated laser plane; prints one preview JPEG as base64.
        --save-pose NAME keeps the laser points on the board plane (camera mm) of every found frame;
        --save-view NAME keeps the corners of the sharpest found frame for the intrinsics
auto    watch new raw frames and save a view whenever the board is found, still for two
        frames and away from every saved view (centre, distance or tilt), up to --views
intrinsics  calibrateCamera over saved views (k3 fixed), leave-one-out per view, write the result;
        later detect/plane use the newest result in calib_data, else release04's intrinsics
axis    rotation axis from a finished motor scan: PnP at every accepted (stopped) view whose raw
        frame was saved, axis direction from the relative rotations, axis point by least squares,
        commanded vs measured angles and corner residuals of the axis model; writes the result
plane   fit the laser plane to saved poses (SVD), report each held-out pose, write the result
stop    stop scan3d-calib and start the /boot demo again exactly as /boot/boot.py does
"""
import argparse
import base64
import importlib.util
import json
import pathlib
import subprocess
import sys
import time

R = pathlib.Path('/home/xilinx/scan3d_final_timingfix_20261030_01')
S = R / 'btn0_full_scan_20261030_01'
# 2026-10-31 rig moved: recalibrate on the solidified seed-retry firmware (binning manifest) and runtime
RELEASE = S / 'release_seedretry'
MANIFEST = R / 'deploy_seedretry_bin/firmware/manifest.json'
DATA = R / 'calib_data_20261031'
STANDING = R / 'boot_autostart_btn0/motor_standing.json'
PY = '/usr/local/share/pynq-venv/bin/python3'
UNIT = 'scan3d-calib'
W, H, COLS, ROWS, SQUARE = 752, 480, 11, 8, 6.0


def say(*a):
    print(*a, flush=True)


def active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0


def stop_idle(unit, data):
    if not active(unit):
        return
    files = sorted(data.glob('run_*/status.json'), key=lambda p: p.stat().st_mtime)
    scan = (json.loads(files[-1].read_text()).get('scan') or {}).get('state') if files else None
    assert scan not in ('SCANNING', 'HOMING', 'MOVING'), (unit, 'is scanning')
    subprocess.run(['systemctl', 'stop', unit], timeout=150)
    for _ in range(60):
        if not active(unit):
            say('stopped', unit, 'scan', scan)
            return
        time.sleep(2)
    raise AssertionError(unit + ' still active')


def start(a):
    stop_idle('scan3d-demo', S / 'live_demo_data')
    stop_idle('scan3d-live-demo', S / 'live_demo_data')
    assert not active(UNIT), UNIT + ' already running'
    import shutil
    free = shutil.disk_usage(R).free
    assert free > a.min_free_gb * 2 ** 30, ('free space below the session minimum', free)
    DATA.mkdir(exist_ok=True)
    out = DATA / time.strftime('run_%Y%m%d_%H%M%S')
    release = pathlib.Path(a.release)
    runtime = release / 'runtime'
    cmd = [PY, '-B', '-m', 'r33.live', '--manifest', str(MANIFEST),
           '--intrinsics', str(runtime / 'calibration_r41/camera_intrinsics_r41.json'),
           '--laser-plane', str(runtime / 'calibration_r41/laser_plane_r41.json'),
           '--output', str(out), '--pipeline', '--duration-s', str(a.duration_s),
           '--save-raw-every', str(a.every), '--section-backend', 'pl', '--pages', 'A',
           '--initial-page', 'A', '--exposure-rows', str(a.exposure_rows), '--gain-code', '16',
           '--profile-reference-frames', '0', '--ui-refresh-s', '2']
    if a.motor:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import demo_service
        authority = demo_service.motor_authority(STANDING, DATA, out.name)
        cmd[4:4] = ['--allow-motor', '--motor-authority', str(authority), '--scan-deadline-s', '1200',
                    '--scan-start-request', str(out / 'scan_start.request.json')]
    subprocess.run(['systemctl', 'reset-failed', UNIT], capture_output=True)
    subprocess.run(['systemd-run', '--unit=' + UNIT, '--collect', '-p', 'KillMode=mixed',
                    '-p', 'TimeoutStopSec=90', '--setenv=XILINX_XRT=/usr',
                    '--setenv=PYTHONPATH=' + str(runtime), '--setenv=PYTHONDONTWRITEBYTECODE=1',
                    '--working-directory=' + str(release)] + cmd, check=True, timeout=60)
    time.sleep(20)
    st = out / 'status.json'
    s = json.loads(st.read_text()) if st.exists() else {}
    say('calib run', out.name, 'active', active(UNIT), {k: s.get(k) for k in ('phase', 'error', 'fps', 'frames')},
        'exposure', s.get('exposure'), 'motor', bool(a.motor))


def scan(a):
    """Start the finite BTN0 scan of the running motor session, as one run-bound request."""
    run = sorted(DATA.glob('run_*'), key=lambda p: p.stat().st_mtime)[-1]
    assert active(UNIT), UNIT + ' is not running'
    st = json.loads((run / 'status.json').read_text())
    assert (st.get('scan') or {}).get('state') == 'IDLE', ('scan is not idle', st.get('scan'))
    request = dict(action='start', machine_id=pathlib.Path('/etc/machine-id').read_text().strip(),
                   boot_id=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(), run_id=run.name)
    tmp = run / 'scan_start.request.tmp'
    tmp.write_text(json.dumps(request))
    tmp.rename(run / 'scan_start.request.json')
    time.sleep(8)
    sc = json.loads((run / 'status.json').read_text()).get('scan') or {}
    say('scan', run.name, {k: sc.get(k) for k in ('state', 'phase', 'visited_views', 'steps', 'message')})


def intrinsics_path():
    found = sorted(DATA.glob('camera_intrinsics_*.json'))
    return found[-1] if found else RELEASE / 'runtime/calibration_r41/camera_intrinsics_r41.json'


def intrinsics():
    import numpy as np
    d = json.loads(intrinsics_path().read_text())
    p = json.loads((RELEASE / 'runtime/calibration_r41/laser_plane_r41.json').read_text())
    return np.array(d['camera_matrix']), np.array(d['distortion_coefficients']), np.array(p['plane_coefficients'], float)


def laser_on_board(gray, corners, k, dist, rvec, tvec, plane, keep=None):
    """Laser centre per row inside the board, intersected with the laser plane, vs the PnP board plane.

    keep: list that receives the same rays intersected with the PnP board plane (camera mm).
    """
    import cv2
    import numpy as np
    hull = cv2.convexHull(corners.reshape(-1, 2).astype(np.float32))
    mask = np.zeros(gray.shape, np.uint8)
    cv2.fillConvexPoly(mask, hull.astype(np.int32), 1)
    pts = []
    for y in range(H):
        xs = np.flatnonzero(mask[y])
        if len(xs) < 20:
            continue
        row = gray[y, xs].astype(np.float64)
        top = row.max()
        if top < 120:
            continue
        hot = row >= top - 25
        span = np.flatnonzero(hot)
        if span[-1] - span[0] > 12:      # a white square, not a narrow laser line
            continue
        wgt = row[hot] - (top - 26)
        pts.append((xs[span[0]] + (np.arange(len(span)) * wgt).sum() / wgt.sum(), y))
    if len(pts) < 10:
        return dict(rows=len(pts))
    uv = cv2.undistortPoints(np.array(pts, np.float64).reshape(-1, 1, 2), k, dist).reshape(-1, 2)
    rays = np.column_stack((uv, np.ones(len(uv))))
    n, d0 = plane[:3], plane[3]
    xyz = rays * (-d0 / (rays @ n))[:, None]
    rot = cv2.Rodrigues(rvec)[0]
    normal = rot[:, 2]
    dist_mm = (xyz - tvec.reshape(3)) @ normal
    if keep is not None:
        keep.append(rays * ((tvec.reshape(3) @ normal) / (rays @ normal))[:, None])
    return dict(rows=len(pts), plane_err_mm_median=round(float(np.median(dist_mm)), 3),
                plane_err_mm_rms=round(float(np.sqrt(np.mean(dist_mm ** 2))), 3),
                line_length_mm=round(float(np.linalg.norm(xyz[-1] - xyz[0])), 2))


def detect(a):
    import cv2
    import numpy as np
    runs = sorted(DATA.glob('run_*'), key=lambda p: p.stat().st_mtime)
    assert runs, 'no calibration run'
    run = runs[-1]
    raws = sorted(run.glob('raw_*.gray8'))[-a.last:]
    k, dist, plane = intrinsics()
    obj = np.zeros((COLS * ROWS, 3))
    obj[:, :2] = np.mgrid[0:COLS, 0:ROWS].T.reshape(-1, 2) * SQUARE
    best, kept, views = None, [], []
    say('intrinsics', intrinsics_path().name)
    for f in raws:
        gray = np.frombuffer(f.read_bytes(), np.uint8).reshape(H, W)
        ok, corners = cv2.findChessboardCorners(gray, (COLS, ROWS), flags=cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE)
        rec = dict(frame=f.stem, mean=round(float(gray.mean()), 1), sat=int((gray >= 250).sum()), found=bool(ok))
        if ok:
            corners = cv2.cornerSubPix(gray, corners, (5, 5), (-1, -1),
                                       (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 1e-3))
            _, rvec, tvec = cv2.solvePnP(obj, corners, k, dist)
            proj = cv2.projectPoints(obj, rvec, tvec, k, dist)[0]
            err = np.linalg.norm(proj.reshape(-1, 2) - corners.reshape(-1, 2), axis=1)
            normal = cv2.Rodrigues(rvec)[0][:, 2]
            hull = cv2.convexHull(corners.reshape(-1, 2).astype(np.float32)).astype(np.int32)
            inside = np.zeros(gray.shape, np.uint8)
            cv2.fillConvexPoly(inside, hull, 1)
            sharp = float(cv2.Laplacian(gray, cv2.CV_64F)[inside > 0].var())
            views.append((sharp, corners))
            rec.update(sharpness=round(sharp, 1), centre_px=[round(float(v), 1) for v in corners.reshape(-1, 2).mean(0)],
                       reproj_rms_px=round(float(np.sqrt(np.mean(err ** 2))), 3),
                       distance_mm=round(float(np.linalg.norm(tvec)), 1),
                       tilt_deg=round(float(np.degrees(np.arccos(abs(normal[2])))), 1),
                       square_px=round(float(np.median(np.linalg.norm(np.diff(corners.reshape(ROWS, COLS, 2), axis=1), axis=2))), 1),
                       laser=laser_on_board(gray, corners, k, dist, rvec, tvec, plane,
                                            kept if a.save_pose else None))
            best = (gray, corners, f.stem)
        say(json.dumps(rec))
    if a.save_view:
        assert views, 'no board found'
        view = DATA / 'views' / (a.save_view + '.npy')
        view.parent.mkdir(exist_ok=True)
        np.save(view, max(views, key=lambda v: v[0])[1].reshape(-1, 2))
        say('saved view', view.name, 'views now', len(list(view.parent.glob('*.npy'))))
    if a.save_pose:
        assert kept, 'no laser points on a found board'
        pose = DATA / 'poses' / (a.save_pose + '.npy')
        pose.parent.mkdir(exist_ok=True)
        np.save(pose, np.concatenate(kept))
        say('saved pose', pose.name, sum(len(x) for x in kept), 'points')
    gray, corners, name = best if best else (np.frombuffer(raws[-1].read_bytes(), np.uint8).reshape(H, W), None, raws[-1].stem)
    img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if corners is not None:
        cv2.drawChessboardCorners(img, (COLS, ROWS), corners, True)
    ok, jpg = cv2.imencode('.jpg', cv2.resize(img, (W // 2, H // 2)), [cv2.IMWRITE_JPEG_QUALITY, 70])
    say('PREVIEW', name, base64.b64encode(jpg.tobytes()).decode())


def auto(a):
    import cv2
    import numpy as np
    run = sorted(DATA.glob('run_*'), key=lambda p: p.stat().st_mtime)[-1]
    k, dist, _ = intrinsics()
    obj = np.zeros((COLS * ROWS, 3))
    obj[:, :2] = np.mgrid[0:COLS, 0:ROWS].T.reshape(-1, 2) * SQUARE
    folder = DATA / 'views'
    folder.mkdir(exist_ok=True)
    saved = []                               # (centre, distance, normal)
    for f in sorted(folder.glob('*.npy')):
        c = np.load(f)
        _, r, t = cv2.solvePnP(obj, c, k, dist)
        saved.append((c.mean(0), float(np.linalg.norm(t)), cv2.Rodrigues(r)[0][:, 2]))
    seen, last, end = set(f.name for f in run.glob('raw_*.gray8')), None, time.monotonic() + a.timeout_s
    say('auto start', len(saved), 'views')
    while len(saved) < a.views and time.monotonic() < end:
        new = sorted(f for f in run.glob('raw_*.gray8') if f.name not in seen)
        if not new:
            time.sleep(.3)
            continue
        f = new[-1]
        seen.update(x.name for x in new)
        time.sleep(.05)
        gray = np.frombuffer(f.read_bytes(), np.uint8).reshape(H, W)
        ok, c = cv2.findChessboardCorners(gray, (COLS, ROWS), flags=cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE)
        if not ok:
            last = None
            continue
        c = cv2.cornerSubPix(gray, c, (5, 5), (-1, -1), (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 1e-3)).reshape(-1, 2)
        still = last is not None and float(np.abs(c - last).max()) < 2.5
        last = c
        if not still:
            continue
        _, r, t = cv2.solvePnP(obj, c, k, dist)
        centre, d, n = c.mean(0), float(np.linalg.norm(t)), cv2.Rodrigues(r)[0][:, 2]
        novel = all(np.linalg.norm(centre - c0) > 70 or abs(d - d0) > 40
                    or np.degrees(np.arccos(min(1., abs(float(n @ n0))))) > 12 for c0, d0, n0 in saved)
        if not novel:
            continue
        name = 'v%02d_%s' % (len(saved) + 1, f.stem)
        np.save(folder / (name + '.npy'), c)
        saved.append((centre, d, n))
        say('saved', name, 'centre', centre.round(0).tolist(), 'distance_mm', round(d, 1),
            'tilt_deg', round(float(np.degrees(np.arccos(abs(n[2])))), 1), 'mean', round(float(gray.mean()), 1))
        last = None
    say('auto done', len(saved), 'views')


def solve_intrinsics(a):
    import cv2
    import numpy as np
    views = {p.stem: np.load(p).astype(np.float32) for p in sorted((DATA / 'views').glob('*.npy'))}
    assert len(views) >= 8, 'need at least 8 views'
    obj = np.zeros((COLS * ROWS, 3), np.float32)
    obj[:, :2] = np.mgrid[0:COLS, 0:ROWS].T.reshape(-1, 2) * SQUARE
    flags = cv2.CALIB_FIX_K3

    def calib(names):
        return cv2.calibrateCamera([obj] * len(names), [views[n].reshape(-1, 1, 2) for n in names],
                                   (W, H), None, None, flags=flags)
    names = sorted(views)
    rms, k, dist, rvecs, tvecs = calib(names)
    for n, r, t in zip(names, rvecs, tvecs):
        e = np.linalg.norm(cv2.projectPoints(obj, r, t, k, dist)[0].reshape(-1, 2) - views[n], axis=1)
        say('view', n, 'rms_px', round(float(np.sqrt(np.mean(e ** 2))), 3), 'distance_mm', round(float(np.linalg.norm(t)), 1))
    holdout, focal = [], []
    for n in names:                          # leave-one-out: intrinsics without the view, PnP on it
        _, k1, d1, _, _ = calib([m for m in names if m != n])
        focal.append(float(k1[0, 0]))
        _, r, t = cv2.solvePnP(obj, views[n], k1, d1)
        e = np.linalg.norm(cv2.projectPoints(obj, r, t, k1, d1)[0].reshape(-1, 2) - views[n], axis=1)
        holdout.append(float(np.sqrt(np.mean(e ** 2))))
    old = json.loads((RELEASE / 'runtime/calibration_r41/camera_intrinsics_r41.json').read_text())
    out = dict(schema='camera_intrinsics_r41_v1', image_size=[W, H], camera_matrix=k.tolist(),
               distortion_coefficients=dist.ravel().tolist(), distortion_model='opencv_k1_k2_p1_p2_k3(k3=0 fixed)',
               rms_px=float(rms), holdout_rms_px_median=float(np.median(holdout)), holdout_rms_px_max=float(max(holdout)),
               loo_focal_spread_pct=100 * (max(focal) - min(focal)) / float(k[0, 0]),
               fit_flags='fix_k3', inner_corners=[COLS, ROWS], square_size_mm=SQUARE, views=names,
               previous_focal_px=[old['camera_matrix'][0][0], old['camera_matrix'][1][1]],
               source='checkerboard session ' + time.strftime('%Y%m%d_%H%M%S'))
    path = DATA / ('camera_intrinsics_' + time.strftime('%Y%m%d_%H%M%S') + '.json')
    path.write_text(json.dumps(out, indent=2))
    say('INTRINSICS', path.name, json.dumps(out))


def rot(axis, angle):
    import numpy as np
    x, y, z = axis
    k = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    return np.eye(3) + np.sin(angle) * k + (1 - np.cos(angle)) * (k @ k)


def solve_axis(a):
    import cv2
    import numpy as np
    run = DATA / a.run
    frames = json.loads((run / 'frames.json').read_text())
    k, dist, _ = intrinsics()
    obj = np.zeros((COLS * ROWS, 3))
    obj[:, :2] = np.mgrid[0:COLS, 0:ROWS].T.reshape(-1, 2) * SQUARE
    # the last saved frame at each stopped motor position: settled, laser on or off
    last = {}
    for f in frames:
        if f.get('motor_steps') and (run / ('raw_%06d.gray8' % f['frame'])).exists():
            last[int(f['motor_steps'])] = f
    poses = []
    for f in last.values():
        raw = run / ('raw_%06d.gray8' % f['frame'])
        gray = np.frombuffer(raw.read_bytes(), np.uint8).reshape(H, W)
        # board turned away: the fast check rejects the frame without the slow full search
        ok, c = cv2.findChessboardCorners(gray, (COLS, ROWS), flags=cv2.CALIB_CB_ADAPTIVE_THRESH
                                          | cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_FAST_CHECK)
        if not ok:
            continue
        c = cv2.cornerSubPix(gray, c, (5, 5), (-1, -1), (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 1e-3))
        _, r, t = cv2.solvePnP(obj, c, k, dist)
        e = np.linalg.norm(cv2.projectPoints(obj, r, t, k, dist)[0].reshape(-1, 2) - c.reshape(-1, 2), axis=1)
        if np.sqrt(np.mean(e ** 2)) > 1.0:
            continue
        poses.append(dict(steps=int(f['motor_steps']), R=cv2.Rodrigues(r)[0], t=t.reshape(3), c=c.reshape(-1, 2)))
    assert len(poses) >= 6, ('too few board views', len(poses))
    poses.sort(key=lambda q: q['steps'])
    ref = poses[len(poses) // 2]
    rad = 2 * np.pi / 3200
    dirs, measured, commanded, rows, rhs = [], [], [], [], []
    for q in poses:
        dth = (q['steps'] - ref['steps']) * rad
        if abs(dth) < np.radians(3):
            continue
        rel = q['R'] @ ref['R'].T
        v = cv2.Rodrigues(rel)[0].reshape(3)
        dirs.append(v / np.linalg.norm(v) * np.sign(dth))
        measured.append(float(np.linalg.norm(v)))
        commanded.append(abs(dth))
        rows.append(np.eye(3) - rel)
        rhs.append(q['t'] - rel @ ref['t'])
    d = np.mean(dirs, 0)
    d /= np.linalg.norm(d)
    centre = (ref['R'] @ obj.mean(0)) + ref['t']
    A = np.vstack(rows + [d.reshape(1, 3)])
    b = np.concatenate(rhs + [[d @ centre]])     # pin the axis point level with the board centre
    p = np.linalg.lstsq(A, b, rcond=None)[0]
    err_mm, err_px = [], []
    base = (ref['R'] @ obj.T).T + ref['t']
    for q in poses:
        g = rot(d, (q['steps'] - ref['steps']) * rad)
        pred = (base - p) @ g.T + p
        meas = (q['R'] @ obj.T).T + q['t']
        err_mm.append(float(np.sqrt(np.mean(np.sum((pred - meas) ** 2, 1)))))
        px = cv2.projectPoints(pred, np.zeros(3), np.zeros(3), k, dist)[0].reshape(-1, 2)
        err_px.append(float(np.sqrt(np.mean(np.sum((px - q['c']) ** 2, 1)))))
    ratio = np.array(measured) / np.array(commanded)
    radial = np.array([1., 0., 0.]) - d * d[0]
    out = dict(schema='turntable_axis_v1', coordinate_frame='camera', units='mm',
               axis_point_camera_mm=p.tolist(), axis_direction_camera=d.tolist(),
               radial_reference_camera=(radial / np.linalg.norm(radial)).tolist(), rotation_angle_sign=1,
               convention='a point fixed on the turntable at motor angle theta = steps*2pi/3200 is at '
                         'rot(axis_direction, theta) (X0 - axis_point) + axis_point',
               views=len(poses), steps_range=[poses[0]['steps'], poses[-1]['steps']],
               angle_ratio_measured_over_commanded=dict(median=float(np.median(ratio)), min=float(ratio.min()), max=float(ratio.max())),
               direction_spread_deg=float(np.degrees(np.max(np.arccos(np.clip(np.array(dirs) @ d, -1, 1))))),
               corner_residual_mm=dict(median=float(np.median(err_mm)), max=float(max(err_mm))),
               corner_residual_px=dict(median=float(np.median(err_px)), max=float(max(err_px))),
               intrinsics=intrinsics_path().name, run=run.name, source='checkerboard on turntable ' + time.strftime('%Y%m%d_%H%M%S'))
    path = DATA / ('turntable_axis_' + time.strftime('%Y%m%d_%H%M%S') + '.json')
    path.write_text(json.dumps(out, indent=2))
    say('AXIS', path.name, json.dumps(out))


def fit(points):
    import numpy as np
    c = points.mean(0)
    n = np.linalg.svd(points - c)[2][-1]
    if n[2] < 0:
        n = -n
    return np.append(n, -n @ c)


def plane(a):
    import numpy as np
    poses = {p.stem: np.load(p) for p in sorted((DATA / 'poses').glob('*.npy'))}
    assert len(poses) >= 3, 'need at least 3 poses'
    for name in poses:                       # leave-one-out
        rest = np.concatenate([v for k, v in poses.items() if k != name])
        p = fit(rest)
        d = poses[name] @ p[:3] + p[3]
        say('holdout', name, 'points', len(poses[name]), 'rms_mm', round(float(np.sqrt(np.mean(d ** 2))), 3),
            'depth_mm', round(float(poses[name][:, 2].mean()), 1))
    allp = np.concatenate(list(poses.values()))
    p = fit(allp)
    d = allp @ p[:3] + p[3]
    _, _, old = intrinsics()
    out = dict(schema='laser_plane_r41_v1', coordinate_frame='camera', units='mm', plane_coefficients=p.tolist(),
               fit_rms_mm=float(np.sqrt(np.mean(d ** 2))), poses=sorted(poses), points=int(len(allp)),
               angle_to_previous_deg=float(np.degrees(np.arccos(min(1., abs(p[:3] @ old[:3]))))),
               camera_intrinsics='camera_intrinsics_r41.json', source='checkerboard session ' + time.strftime('%Y%m%d_%H%M%S'))
    path = DATA / ('laser_plane_' + time.strftime('%Y%m%d_%H%M%S') + '.json')
    path.write_text(json.dumps(out, indent=2))
    say('PLANE', json.dumps(out))


def stop(a):
    if active(UNIT):
        subprocess.run(['systemctl', 'stop', UNIT], timeout=150)
    spec = importlib.util.spec_from_file_location('scan3d_boot', '/boot/boot.py')
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    config = json.loads(pathlib.Path('/boot/scan3d_boot_config.json').read_text())
    if not active('scan3d-demo'):
        subprocess.run(['systemctl', 'reset-failed', 'scan3d-demo'], capture_output=True)
        subprocess.run(boot.demo_command(config), check=True, timeout=30)
    time.sleep(5)
    say('calib active', active(UNIT), 'scan3d-demo active', active('scan3d-demo'))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('start')
    s.add_argument('--exposure-rows', type=int, default=160)
    s.add_argument('--duration-s', type=float, default=1200.)
    s.add_argument('--every', type=int, default=5)
    s.add_argument('--motor', action='store_true')
    s.add_argument('--min-free-gb', type=float, default=2.)
    s.add_argument('--release', default=str(RELEASE), help='release folder holding runtime/ (motor scans need scan-start-request)')
    d = sub.add_parser('detect')
    d.add_argument('--last', type=int, default=4)
    d.add_argument('--save-pose')
    d.add_argument('--save-view')
    u = sub.add_parser('auto')
    u.add_argument('--views', type=int, default=20)
    u.add_argument('--timeout-s', type=float, default=150.)
    sub.add_parser('scan')
    x = sub.add_parser('axis')
    x.add_argument('--run', required=True)
    sub.add_parser('intrinsics')
    sub.add_parser('plane')
    sub.add_parser('stop')
    a = p.parse_args()
    dict(start=start, detect=detect, auto=auto, scan=scan, axis=solve_axis, intrinsics=solve_intrinsics, plane=plane, stop=stop)[a.cmd](a)


if __name__ == '__main__':
    main()
