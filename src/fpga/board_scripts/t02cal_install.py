"""Board side (sudo): runtime_t02cal = runtime_timing with the T02-calibrated laser plane and
turntable axis; restart scan3d-live-demo on it. No FPGA load, no motor command.

The loaded firmware must already be the timing bit (live stream). The demo starts only from an
idle state; the motor stays under the BTN0 standing permission as before.
"""
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import time

from live_trial import PY, R, S, latest_status, regs, say
from live_trial2 import restart

HERE = pathlib.Path(__file__).resolve().parent
FILES = ('camera_intrinsics_r41.json', 'laser_plane_r41.json', 'turntable_axis.json')


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def main():
    expected = json.loads((HERE / 'EXPECTED.json').read_text())
    base, rt, rel = (S / expected[k] for k in ('base_runtime', 'runtime', 'release'))
    for name, h in expected['base'].items():
        assert sha(base / 'calibration_r41' / name) == h, ('runtime_timing calibration differs', name)
    for name in FILES:
        assert sha(HERE / name) == expected['new'][name], ('upload differs', name)
    if not rt.exists():
        shutil.copytree(base, rt, symlinks=True)
        for name in FILES:
            shutil.copyfile(HERE / name, rt / 'calibration_r41' / name)
        rel.mkdir()
        (rel / 'runtime').symlink_to(rt, target_is_directory=True)
    for name in FILES:
        assert sha(rt / 'calibration_r41' / name) == expected['new'][name], ('runtime_t02cal differs', name)

    unit, data = 'scan3d-live-demo', S / 'live_demo_data'
    for name, folder in (('scan3d-calib', R / 'calib_data'), (unit, data), ('scan3d-demo', R / 'demo_data')):
        if subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode:
            continue
        run, st = latest_status(folder)
        scan = (st.get('scan') or {}).get('state')
        assert scan not in ('SCANNING', 'HOMING', 'MOVING'), ('scanning', name, scan)
        r0 = regs()
        assert not r0['motor']['enabled'] and not r0['guard']['enabled'], r0
        subprocess.run(['systemctl', 'stop', name], timeout=150)
        say('stopped', name)
    manifest = R / 'deploy_timing/firmware/manifest.json'
    restart(unit, rel, manifest, data, True)
    time.sleep(45)
    run, st = latest_status(data)
    cal = json.loads((run / 'calibration_live.json').read_text()) if run else {}
    say(unit, subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True).stdout.strip(),
        'run', run and run.name, {k: st.get(k) for k in ('phase', 'error', 'frames')},
        'axis', cal.get('axis_status'), 'plane', cal.get('laser_plane_camera'))
    say('T02CAL_INSTALL_DONE')


if __name__ == '__main__':
    sys.exit(main())
