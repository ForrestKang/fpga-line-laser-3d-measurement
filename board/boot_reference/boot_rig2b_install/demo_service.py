"""Demo loop started at boot: camera, three HDMI pages, BTN1-3, and BTN0 scans when permitted.

Runs r33.live in fixed-length runs and starts the next one when a run ends. A run that
fails early is retried with a growing pause. Raw frames are not saved; only the newest
demo runs are kept. SIGTERM stops the current run through live's own shutdown.

With --motor-standing (the user's standing BTN0 permission) every run gets its own motor
authority file, so one BTN0 press starts one bounded revolution and a second press stops it.
live allows one scan per run: after a scan ends the run is closed once the result has been
shown for --rescan-after-s, and the next run is ready for BTN0 again.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

RUN_NAME = re.compile(r'^run_\d{8}_\d{6}$')
START_FREE_BYTES = 2 * 1024**3 + 256 * 1024**2     # start a run only above this
STOP_FREE_BYTES = 2 * 1024**3 + 64 * 1024**2       # ask the run to stop below this


def live_command(python, release, manifest, out, run_s, motor_authority=None, live_stream=False, full_width=False):
    runtime = release / 'runtime'
    motor = []
    if motor_authority:
        motor = ['--allow-motor', '--motor-authority', str(motor_authority), '--scan-deadline-s', '1200',
                 '--scan-start-request', str(out / 'scan_start.request.json')]
    return [python, '-B', '-m', 'r33.live', *motor,
            '--manifest', str(manifest),
            '--intrinsics', str(runtime / 'calibration_r41/camera_intrinsics_r41.json'),
            '--laser-plane', str(runtime / 'calibration_r41/laser_plane_r41.json'),
            *(['--turntable-axis', str(runtime / 'calibration_r41/turntable_axis.json')]
              if (runtime / 'calibration_r41/turntable_axis.json').is_file() else []),
            '--output', str(out),
            '--pipeline', '--duration-s', str(run_s), '--save-raw-every', '0',
            '--section-backend', 'pl', '--pages', 'A,B,C', '--initial-page', 'A',
            # camera settings validated in the btn0 full-scan work (exposure, PL AE); 2x binning unless
            # the firmware runs the same-run seed retry, which takes full-width rows only
            '--exposure-rows', '64', '--gain-code', '16', '--pl-ae', *([] if full_width else ['--pfo3-bin2']),
            '--ae-target-low', '224',
            # less ARM work per frame: no change detection, sampled ARM section check, slower page redraw
            '--profile-reference-frames', '0', '--section-compare-every', '30', '--ui-refresh-s', '2',
            '--scan-invalid-retries', '2',
            # camera straight into PFO3 (needs a bit that declares live_stream)
            *(['--live-stream'] if live_stream else [])]


def motor_authority(standing, data, run_name):
    """Per-run authority for live, derived from the standing BTN0 permission."""
    st = json.loads(Path(standing).read_text(encoding='utf8'))
    if (st.get('schema') != 'SCAN3D_MOTOR_STANDING_V1' or st.get('btn0_scan') is not True
            or st.get('motor_12v_connected') is not True or not str(st.get('user_statement', '')).strip()):
        raise ValueError('standing BTN0 permission is incomplete')
    ident = {k: Path(f).read_text().strip() for k, f in
             (('machine_id', '/etc/machine-id'), ('boot_id', '/proc/sys/kernel/random/boot_id'))}
    if st.get('machine_id') != ident['machine_id']:
        raise ValueError('standing permission belongs to another board')
    path = data / (run_name + '.motor.json')
    path.write_text(json.dumps(dict(ident, run_id=run_name, allow_motor=True, motor_12v_connected=True,
                                    user_statement=st['user_statement'], standing_permission=True)),
                    encoding='utf8')
    return path


def scan_state(out):
    try:
        return json.loads((out / 'status.json').read_text())['scan']['state']
    except Exception:
        return None


def free_bytes(path):
    return shutil.disk_usage(path).free


def prune(data, keep):
    """Remove the oldest demo run folders (only names this service creates)."""
    runs = sorted(p for p in data.iterdir() if p.is_dir() and RUN_NAME.match(p.name))
    removed = []
    for old in runs[:max(0, len(runs) - keep)]:
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


def cpu_ticks():
    try:
        v = [int(x) for x in Path('/proc/stat').read_text().split('\n')[0].split()[1:9]]
    except OSError:
        return 0, 0
    return sum(v), v[3] + v[4]


def mem_available_mib():
    try:
        lines = Path('/proc/meminfo').read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) / 1024
    return None


def pause_after(duration_s, code, fails):
    """Seconds to wait before the next run, and the new count of early failures."""
    if code != 0 and duration_s < 60:
        return min(300, 10 * 2 ** fails), fails + 1
    return 3, 0


def summary_fields(out):
    try:
        s = json.loads((out / 'summary.json').read_text())
    except Exception:
        return {}
    keys = ('frames', 'fps', 'stop_reason', 'phase', 'error', 'display_median_ms', 'page_changes')
    return {k: s.get(k) for k in keys if k in s} | {'ui_render_ms': (s.get('ui') or {}).get('render_ms')}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--release', type=Path, required=True)
    ap.add_argument('--manifest', type=Path, required=True)
    ap.add_argument('--data', type=Path, required=True)
    ap.add_argument('--python', default=sys.executable)
    ap.add_argument('--run-s', type=float, default=1800.)
    ap.add_argument('--keep', type=int, default=3)
    ap.add_argument('--cycles', type=int, default=0, help='stop after this many runs (0: forever)')
    ap.add_argument('--motor-standing', type=Path, help='standing BTN0 permission (enables the motor)')
    ap.add_argument('--rescan-after-s', type=float, default=90.)
    ap.add_argument('--live-stream', action='store_true', help='PL processes every camera frame')
    a = ap.parse_args(argv)
    a.data.mkdir(parents=True, exist_ok=True)
    # a seed-retry firmware decodes full-width rows only (manifest pfo3.same_run_seed_retry)
    full_width = bool(json.loads(a.manifest.read_text(encoding='utf8')).get('pfo3', {}).get('same_run_seed_retry'))
    stop = []
    child = [None]

    def on_signal(signum, _frame):
        stop.append(signum)
        if child[0] is not None and child[0].poll() is None:
            child[0].send_signal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(a.release / 'runtime'))
    log = a.data / 'demo_service.jsonl'
    runs = fails = 0
    while not stop and (a.cycles == 0 or runs < a.cycles):
        if free_bytes(a.data) < START_FREE_BYTES:
            with log.open('a') as f:
                f.write(json.dumps({'event': 'low_disk', 'free_bytes': free_bytes(a.data), 't': time.time()}) + '\n')
            time.sleep(60)
            continue
        out = a.data / time.strftime('run_%Y%m%d_%H%M%S')
        if out.exists():
            time.sleep(1)
            continue
        started = time.monotonic()
        total0, idle0 = cpu_ticks()
        mem_min = mem_available_mib()
        authority = motor_authority(a.motor_standing, a.data, out.name) if a.motor_standing else None
        scan_end = None
        with (a.data / (out.name + '.log')).open('w') as console:
            child[0] = subprocess.Popen(live_command(a.python, a.release, a.manifest, out, a.run_s, authority,
                                                     a.live_stream, full_width),
                                        cwd=a.release, env=env, stdout=console, stderr=subprocess.STDOUT)
            while child[0].poll() is None:
                time.sleep(5)
                if authority and scan_state(out) in ('DONE', 'STOPPED', 'FAILED'):
                    scan_end = scan_end or time.monotonic()
                    if time.monotonic() - scan_end >= a.rescan_after_s:
                        (out / 'stop.request').touch(exist_ok=True)
                m = mem_available_mib()
                mem_min = m if mem_min is None or (m is not None and m < mem_min) else mem_min
                if free_bytes(a.data) < STOP_FREE_BYTES and out.is_dir():
                    (out / 'stop.request').touch(exist_ok=True)
        code = child[0].returncode
        child[0] = None
        duration = time.monotonic() - started
        total1, idle1 = cpu_ticks()
        busy = 1 - (idle1 - idle0) / max(1, total1 - total0)
        runs += 1
        pause, fails = pause_after(duration, code, fails)
        removed = prune(a.data, a.keep)
        for name in removed:
            for suffix in ('.log', '.motor.json'):
                stale = a.data / (name + suffix)
                if stale.exists():
                    stale.unlink()
        record = {'event': 'run_end', 'run': out.name, 'exit_code': code, 'duration_s': round(duration, 1),
                  'motor_enabled': authority is not None, 'scan_state': scan_state(out),
                  'cpu_percent': round(100 * busy, 1), 'mem_available_min_mib': mem_min,
                  'pruned': removed, 'next_pause_s': pause, 't': time.time(), **summary_fields(out)}
        with log.open('a') as f:
            f.write(json.dumps(record) + '\n')
        if not stop and (a.cycles == 0 or runs < a.cycles):
            time.sleep(pause)
    return 0


if __name__ == '__main__':
    sys.exit(main())
