"""Board side (sudo): rerun the camera-only live trial on the already loaded live bit.

No FPGA load. Stops scan3d-live-demo only while idle, runs 60 s with --live-stream and
no motor, then restarts scan3d-live-demo (live stream only if the trial passed).
"""
import json
import mmap
import os
import pathlib
import subprocess
import sys
import time

from live_trial import PY, D, R, S, latest_status, regs, say

HERE = pathlib.Path(__file__).resolve().parent


def pfo3_live_ctrl():
    fd = os.open('/dev/mem', os.O_RDONLY | os.O_SYNC)
    with mmap.mmap(fd, 4096, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ, offset=0x40000000) as mm:
        v = [int.from_bytes(mm[o:o + 4], 'little') for o in (0, 0x24, 0x28, 0x2c)]
    os.close(fd)
    return v


def restart(unit, rel, manifest, data, live):
    subprocess.run(['systemctl', 'reset-failed', unit], capture_output=True)
    cmd = ['systemd-run', '--unit=' + unit, '--property=KillMode=mixed', '--property=TimeoutStopSec=90',
           '--setenv=XILINX_XRT=/usr', PY, '-u', '-B', str(HERE / 'demo_service.py'),
           '--release', str(rel), '--manifest', str(manifest), '--data', str(data),
           '--motor-standing', str(R / 'boot_autostart_btn0/motor_standing.json')] + (['--live-stream'] if live else [])
    subprocess.run(cmd, check=True, timeout=60)


def main():
    unit = 'scan3d-live-demo'
    rel, manifest, data = S / 'release_live', D / 'firmware/manifest.json', S / 'live_demo_data'
    run, st = latest_status(data)
    scan = (st.get('scan') or {}).get('state')
    assert scan not in ('SCANNING', 'HOMING', 'MOVING'), ('demo is scanning', scan)
    r0 = regs()
    assert not r0['motor']['enabled'] and not r0['guard']['enabled'], r0
    say('demo run', run and run.name, 'scan', scan, 'motor', r0)
    subprocess.run(['systemctl', 'stop', unit], timeout=150)
    for _ in range(60):
        if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode:
            break
        time.sleep(2)
    assert subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode, 'demo still active'
    ctrl = pfo3_live_ctrl()
    say('pfo3 magic/live/packets/errors before', [hex(ctrl[0])] + ctrl[1:])
    assert ctrl[0] == 0x57334650 and ctrl[1] & 1 == 0, 'PFO3 state'

    sys.path.insert(0, str(HERE))
    import demo_service
    trial = data / time.strftime('trial_%Y%m%d_%H%M%S')
    cmd = demo_service.live_command(PY, rel, manifest, trial, 60, None, True)
    assert '--allow-motor' not in cmd and '--scan-start-request' not in cmd
    env = dict(os.environ, XILINX_XRT='/usr', PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(rel / 'runtime'))
    t0 = time.monotonic()
    with (data / (trial.name + '.log')).open('w') as console:
        code = subprocess.run(cmd, cwd=rel, env=env, stdout=console, stderr=subprocess.STDOUT, timeout=300).returncode
    wall = time.monotonic() - t0
    ctrl = pfo3_live_ctrl()
    s = {}
    for name in ('summary.json', 'status.json'):
        if (trial / name).exists():
            s = json.loads((trial / name).read_text())
            break
    ls = s.get('live_stream') or {}
    elapsed = ls.get('elapsed_s') or 0
    passed = code == 0 and not s.get('error') and ls.get('packets', 0) > 0 and not ls.get('errors')
    say('trial', trial.name, 'exit', code, 'wall_s', round(wall, 1), 'error', s.get('error'), 'stop', s.get('stop_reason'))
    say('arm', {k: s.get(k) for k in ('frames', 'fps', 'display_median_ms', 'phase')})
    say('pl', ls, 'pl_fps', round(ls.get('packets', 0) / elapsed, 1) if elapsed else None)
    say('pfo3 after (live/packets/errors)', ctrl[1:])
    say('last', {k: (s.get('last') or {}).get(k) for k in ('capture_ms', 'pl_ms', 'decode_ms', 'xyz', 'xyz_measurement')})
    say('summary_keys', sorted(s)[:60])
    if not passed:
        say('LOG_TAIL', (data / (trial.name + '.log')).read_text()[-4000:])

    restart(unit, rel, manifest, data, passed)
    time.sleep(60)
    run, st = latest_status(data)
    say(unit, subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True).stdout.strip(),
        'live_stream', passed, 'run', run and run.name, {k: st.get(k) for k in ('phase', 'error', 'frames', 'fps')},
        'scan', (st.get('scan') or {}).get('state'), 'pl', st.get('live_stream'))
    say('LIVE_TRIAL_PASS' if passed else 'LIVE_TRIAL_FAIL')


if __name__ == '__main__':
    main()
