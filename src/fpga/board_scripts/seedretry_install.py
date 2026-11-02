"""Board side (sudo): solidify the seed-retry version into /boot. No FPGA load, no motor command.

1. runtime_seedretry (built by seedretry_trial.py) still holds the patch and the calibration;
2. install_boot.py check-only, then --apply with the authority file (boot.py backed up first);
   firmware folder given by argv[1] (deploy_seedretry/firmware: seed retry, full width);
3. stop an idle demo. The next power-up loads the bit and starts the demo from /boot.
"""
import hashlib
import json
import pathlib
import subprocess
import sys

from live_trial import PY, R, S, latest_status, regs, say
from seedretry_trial import CALIBRATION

HERE = pathlib.Path(__file__).resolve().parent


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def main():
    firmware = pathlib.Path(sys.argv[1]).resolve(strict=True)
    rt, rel = S / 'runtime_seedretry', S / 'release_seedretry'
    expected = json.loads((R / 'deploy_seedretry/EXPECTED.json').read_text())
    for name, h in expected['new'].items():
        assert sha(rt / name) == h, ('runtime_seedretry differs', name)
    for name, h in CALIBRATION.items():
        assert sha(rt / 'calibration_r41' / name) == h, ('calibration differs', name)
    assert (rel / 'runtime').resolve() == rt.resolve(), 'release_seedretry'
    m = json.loads((firmware / 'manifest.json').read_text())
    assert sha(firmware / 'final.bit') == m['files']['bit']['sha256'], 'bit'
    say('firmware', firmware, 'seed_retry', m['pfo3'].get('same_run_seed_retry'), 'bit', m['files']['bit']['sha256'])

    args = [PY, '-B', str(HERE / 'install_boot.py'), '--mode', 'auto_demo',
            '--deploy', str(firmware), '--release', str(rel),
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
    say('SEEDRETRY_INSTALL_DONE')


if __name__ == '__main__':
    sys.exit(main())
