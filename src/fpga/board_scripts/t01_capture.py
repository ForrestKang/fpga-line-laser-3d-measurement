"""Board side (sudo): 30 s static camera-only run of the T01 step target on the solidified firmware
(binning manifest, release argv[1], default release_seedretry; no motor), then the /boot demo again
(argv[1] given: started exactly as /boot/boot.py does). Prints the camera-frame
measurement points (int32 0.01 mm, zlib + base64) and one raw frame as a JPEG preview."""
import base64
import json
import os
import pathlib
import subprocess
import sys
import time
import zlib

import numpy as np

from live_trial import PY, R, S, latest_status, regs, say
from live_trial2 import restart

HERE = pathlib.Path(__file__).resolve().parent
UNIT = 'scan3d-live-demo'
MANIFEST = R / 'deploy_seedretry_bin/firmware/manifest.json'


def main():
    data = S / 'live_demo_data'
    rel = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else S / 'release_seedretry'
    for name in ('scan3d-demo', UNIT, 'scan3d-calib'):
        if subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode:
            continue
        run, st = latest_status(data)
        assert (st.get('scan') or {}).get('state') not in ('SCANNING', 'HOMING', 'MOVING'), st.get('scan')
        subprocess.run(['systemctl', 'stop', name], timeout=150)
        say('stopped', name)
    r0 = regs()
    assert not r0['motor']['enabled'] and not r0['guard']['enabled'], r0
    sys.path.insert(0, str(HERE))
    import demo_service
    out = data / time.strftime('t01_%Y%m%d_%H%M%S')
    cmd = demo_service.live_command(PY, rel, MANIFEST, out, 30, None, True, False)
    assert '--allow-motor' not in cmd and '--scan-start-request' not in cmd
    cmd[cmd.index('--save-raw-every') + 1] = '40'
    env = dict(os.environ, XILINX_XRT='/usr', PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(rel / 'runtime'))
    with (data / (out.name + '.log')).open('w') as console:
        code = subprocess.run(cmd, cwd=rel, env=env, stdout=console, stderr=subprocess.STDOUT, timeout=240).returncode
    s = json.loads((out / 'summary.json').read_text()) if (out / 'summary.json').exists() else {}
    say('RUN', out.name, 'exit', code, json.dumps({k: s.get(k) for k in ('phase', 'error', 'frames', 'fps', 'pfo3_binning')}))
    if len(sys.argv) > 1:
        import importlib.util
        spec = importlib.util.spec_from_file_location('scan3d_boot', '/boot/boot.py')
        boot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(boot)
        config = json.loads(pathlib.Path('/boot/scan3d_boot_config.json').read_text())
        subprocess.run(['systemctl', 'reset-failed', 'scan3d-demo'], capture_output=True)
        subprocess.run(boot.demo_command(config), check=True, timeout=30)
        say('demo restarted from /boot config', config['demo']['release'])
    else:
        restart(UNIT, rel, MANIFEST, data, True)
        say('demo restarted', UNIT)
    a = np.load(out / 'xyz_camera_mm.npy')
    q = np.round(a[:, :3] * 100).astype(np.int32)
    m = (a[:, 3] > .5).astype(np.int8)
    frames = json.loads((out / 'frames.json').read_text())
    say('FRAMES', json.dumps([f.get('xyz') for f in frames]))
    say('BLOB', len(a), base64.b64encode(zlib.compress(q.tobytes() + m.tobytes(), 9)).decode())
    raws = sorted(out.glob('raw_*.gray8'))
    if raws:
        import cv2
        gray = np.frombuffer(raws[-1].read_bytes(), np.uint8).reshape(480, 752)
        ok, jpg = cv2.imencode('.jpg', gray, [cv2.IMWRITE_JPEG_QUALITY, 80])
        say('PREVIEW', raws[-1].name, base64.b64encode(jpg.tobytes()).decode())
    say('T01_CAPTURE_DONE')


if __name__ == '__main__':
    main()
