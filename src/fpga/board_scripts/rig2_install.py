"""Board side (sudo): solidify the current-rig calibration (candidate B, 2026-11-01) into /boot.
No FPGA load, no motor command.

1. runtime_rig2 = runtime_seedretry with calibration_r41/laser_plane_r41.json and
   turntable_axis.json replaced (hash-guarded); release_rig2 -> runtime_rig2;
2. install_boot.py check-only, then --apply with the authority file (boot.py backed up first);
   firmware unchanged (deploy_seedretry_bin);
3. stop an idle demo. The next power-up starts the demo from /boot.
"""
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

from live_trial import PY, R, S, latest_status, regs, say

HERE = pathlib.Path(__file__).resolve().parent
FIRMWARE = R / 'deploy_seedretry_bin/firmware'


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def main():
    expected = json.loads((HERE / 'EXPECTED.json').read_text())
    base, rt, rel = S / 'runtime_seedretry', S / 'runtime_rig2', S / 'release_rig2'
    for name, h in expected['base'].items():
        assert sha(base / 'calibration_r41' / name) == h, ('runtime_seedretry calibration differs', name)
    for name, h in expected['new'].items():
        assert sha(HERE / name) == h, ('upload differs', name)
    if not rt.exists():
        shutil.copytree(base, rt, symlinks=True)
        for name in expected['new']:
            assert not (rt / 'calibration_r41' / name).is_symlink(), ('link', name)
            shutil.copyfile(HERE / name, rt / 'calibration_r41' / name)
        rel.mkdir()
        (rel / 'runtime').symlink_to(rt, target_is_directory=True)
    for name, h in expected['new'].items():
        assert sha(rt / 'calibration_r41' / name) == h, ('runtime_rig2 differs', name)
    say('runtime_rig2', {n: sha(rt / 'calibration_r41' / n)[:12] for n in
                         ('camera_intrinsics_r41.json', 'laser_plane_r41.json', 'turntable_axis.json')})

    args = [PY, '-B', str(HERE / 'install_boot.py'), '--mode', 'auto_demo',
            '--deploy', str(FIRMWARE), '--release', str(rel),
            '--data', str(S / 'live_demo_data'), '--live-stream',
            '--motor-standing', str(R / 'boot_autostart_btn0/motor_standing.json')]
    check = subprocess.run(args + ['--output', str(HERE / 'install_check.json')], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr[-2000:]
    say('check', json.loads((HERE / 'install_check.json').read_text())['config'])

    for name in ('scan3d-live-demo', 'scan3d-calib', 'scan3d-demo'):
        if subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode:
            continue
        run, st = latest_status(S / 'live_demo_data')
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
    say('RIG2_INSTALL_DONE')


if __name__ == '__main__':
    sys.exit(main())
