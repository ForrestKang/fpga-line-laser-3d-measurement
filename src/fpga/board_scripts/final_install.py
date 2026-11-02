"""Board side (sudo): solidify the current version into /boot. No FPGA load, no motor command.

1. runtime_final = runtime_t02rot (timing runtime + T02 rotation calibration) with the files in
   runtime_patch/ (hash-guarded), release_final -> runtime_final;
2. install_boot.py check-only, then --apply with the authority file (boot.py backed up first);
3. stop the idle demo and start scan3d-demo with the command /boot/boot.py itself would run.
"""
import hashlib
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import time

from live_trial import PY, R, S, latest_status, regs, say

HERE = pathlib.Path(__file__).resolve().parent


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def main():
    expected = json.loads((HERE / 'EXPECTED.json').read_text())
    base, rt, rel = S / 'runtime_t02rot', S / 'runtime_final', S / 'release_final'
    for name, h in expected['base'].items():
        assert sha(base / name) == h, ('base differs', name)
    for name, h in expected['new'].items():
        assert sha(HERE / 'runtime_patch' / name) == h, ('upload differs', name)
    if not rt.exists():
        shutil.copytree(base, rt, symlinks=True)
        for name in expected['new']:
            shutil.copyfile(HERE / 'runtime_patch' / name, rt / name)
        rel.mkdir()
        (rel / 'runtime').symlink_to(rt, target_is_directory=True)
    for name, h in expected['new'].items():
        assert sha(rt / name) == h, ('runtime_final differs', name)

    args = [PY, '-B', str(HERE / 'install_boot.py'), '--mode', 'auto_demo',
            '--deploy', str(R / 'deploy_timing/firmware'), '--release', str(rel),
            '--data', str(S / 'live_demo_data'), '--live-stream',
            '--motor-standing', str(R / 'boot_autostart_btn0/motor_standing.json')]
    check = subprocess.run(args + ['--output', str(HERE / 'install_check.json')], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr[-2000:]
    say('check', json.loads((HERE / 'install_check.json').read_text())['config'])

    for name, folder in (('scan3d-live-demo', S / 'live_demo_data'), ('scan3d-calib', R / 'calib_data'),
                         ('scan3d-demo', R / 'demo_data')):
        if subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode:
            continue
        run, st = latest_status(folder)
        scan = (st.get('scan') or {}).get('state')
        assert scan not in ('SCANNING', 'HOMING', 'MOVING'), ('scanning', name, scan)
        r0 = regs()
        assert not r0['motor']['enabled'] and not r0['guard']['enabled'], r0
        subprocess.run(['systemctl', 'stop', name], timeout=150)
        say('stopped', name)

    apply = subprocess.run(args + ['--authority', str(HERE / 'authority.json'), '--apply',
                                   '--output', str(HERE / 'install_receipt.json')], capture_output=True, text=True)
    assert apply.returncode == 0, apply.stderr[-2000:]
    receipt = json.loads((HERE / 'install_receipt.json').read_text())
    say('installed', {k: receipt.get(k) for k in ('applied', 'backup', 'loader_sha256', 'config_sha256')})
    say('/boot', sha('/boot/boot.py'), sha('/boot/scan3d_boot_config.json'))

    # start the demo exactly as /boot/boot.py will after the next power-up (bit already loaded)
    spec = importlib.util.spec_from_file_location('scan3d_boot', '/boot/boot.py')
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    config = json.loads(pathlib.Path('/boot/scan3d_boot_config.json').read_text())
    boot.checked_image(config)
    subprocess.run(['systemctl', 'reset-failed', 'scan3d-demo'], capture_output=True)
    subprocess.run(boot.demo_command(config), check=True, timeout=30)
    time.sleep(50)
    run, st = latest_status(S / 'live_demo_data')
    cal = json.loads((run / 'calibration_live.json').read_text()) if run else {}
    say('scan3d-demo', subprocess.run(['systemctl', 'is-active', 'scan3d-demo'], capture_output=True, text=True).stdout.strip(),
        'run', run and run.name, {k: st.get(k) for k in ('phase', 'error', 'frames')},
        'scan', (st.get('scan') or {}).get('message'), 'axis', cal.get('axis_status'),
        'plane_d', (cal.get('laser_plane_camera') or [None] * 4)[3], 'live', st.get('live_stream'))
    say('FINAL_INSTALL_DONE')


if __name__ == '__main__':
    sys.exit(main())
