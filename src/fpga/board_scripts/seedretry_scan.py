"""Board side (sudo): run the BTN0 demo on the seed-retry bit with the given manifest and make one
bounded T02 revolution (user: "你自己旋转一下，T02"), then wait until the scan ends.

argv[1]: manifest (deploy_seedretry/firmware/manifest.json = full width + seed retry,
manifest_bin.json = 2x binning, single seed). The motor moves only for this one scan.
"""
import json
import pathlib
import subprocess
import sys
import time

from live_trial import R, S, latest_status, regs, say
from live_trial2 import restart

UNIT = 'scan3d-live-demo'


def main():
    manifest = pathlib.Path(sys.argv[1]).resolve(strict=True)
    data, rel = S / 'live_demo_data', S / 'release_seedretry'
    for name in ('scan3d-demo', UNIT, 'scan3d-calib'):
        if subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode:
            continue
        run, st = latest_status(data)
        assert (st.get('scan') or {}).get('state') not in ('SCANNING', 'HOMING', 'MOVING'), st.get('scan')
        subprocess.run(['systemctl', 'stop', name], timeout=150)
        say('stopped', name)
    r0 = regs()
    assert not r0['motor']['enabled'] and not r0['guard']['enabled'], r0
    before = {p.name for p in data.glob('run_*')}
    restart(UNIT, rel, manifest, data, True)
    run = None
    for _ in range(90):
        time.sleep(2)
        new = sorted((p for p in data.glob('run_*') if p.is_dir() and p.name not in before
                      and (p / 'status.json').exists()), key=lambda p: p.stat().st_mtime)
        if new:
            st = json.loads((new[-1] / 'status.json').read_text())
            if (st.get('scan') or {}).get('state') == 'IDLE' and st.get('frames', 0) > 5:
                run = new[-1]
                break
    assert run is not None, 'demo run not ready'
    st = json.loads((run / 'status.json').read_text())
    say('ready', run.name, {k: st.get(k) for k in ('frames', 'fps', 'pfo3_binning', 'live_stream')})
    req = dict(action='start', machine_id=pathlib.Path('/etc/machine-id').read_text().strip(),
               boot_id=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(), run_id=run.name)
    tmp = run / 'scan_start.request.tmp'
    tmp.write_text(json.dumps(req))
    tmp.rename(run / 'scan_start.request.json')
    t0 = time.monotonic()
    sc = {}
    while time.monotonic() - t0 < 1300:
        time.sleep(10)
        sc = json.loads((run / 'status.json').read_text()).get('scan') or {}
        if sc.get('state') in ('DONE', 'STOPPED', 'FAILED'):
            break
    say('SCAN_END', run.name, round(time.monotonic() - t0), json.dumps(
        {k: sc.get(k) for k in ('state', 'phase', 'visited_views', 'steps', 'angle_deg', 'message')}, ensure_ascii=False))
    r1 = regs()
    say('motor_after', r1)
    say('SEEDRETRY_SCAN_DONE')


if __name__ == '__main__':
    main()
