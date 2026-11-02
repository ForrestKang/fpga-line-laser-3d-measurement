"""Install boot_loader.py as /boot/boot.py with its config. Default: check only, no changes.

--apply needs root and an authority file bound to this machine, boot, loader, manifest
and demo script. The original boot.py is backed up first and restored if a write fails.
Never loads the FPGA, starts the demo, reboots, or touches the camera or motor.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

BOARD_ROOT = Path('/home/xilinx/scan3d_final_timingfix_20261030_01')
DEPLOY = BOARD_ROOT / 'release02/deploy/final'
RELEASE = BOARD_ROOT / 'release04'
DATA = BOARD_ROOT / 'demo_data'
PYTHON = '/usr/local/share/pynq-venv/bin/python3'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_write(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.scan3d-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def build_config(mode, demo_script, motor_standing=None, release=RELEASE, data=DATA, deploy=DEPLOY,
                 live_stream=False):
    config = {'schema': 'SCAN3D_BOOT_CONFIG_V1', 'mode': mode, 'deploy': str(deploy),
              'manifest_sha256': sha(deploy / 'manifest.json')}
    if mode == 'auto_demo':
        config['demo'] = {'python': PYTHON, 'script': str(demo_script), 'script_sha256': sha(demo_script),
                          'release': str(release), 'data': str(data)}
        if motor_standing is not None:
            config['demo']['motor_standing'] = {'path': str(motor_standing), 'sha256': sha(motor_standing)}
        if live_stream:
            # the camera feeds PFO3 directly; the firmware must declare it
            if not (json.loads((deploy / 'manifest.json').read_text()).get('live_stream') or {}).get('present'):
                raise ValueError('deployment declares no live camera stream')
            config['demo']['live_stream'] = True
    return config


def check_authority(auth, identity, result):
    keys = ('machine_id', 'boot_id', 'loader_sha256', 'manifest_sha256', 'demo_sha256', 'mode',
            'motor_standing_sha256')
    if (auth.get('schema') != 'SCAN3D_BOOT_INSTALL_AUTHORITY_V1'
            or auth.get('boot_configuration_authorized') is not True or not auth.get('user_statement')
            or any(auth.get(k) != (identity | result).get(k) for k in keys)):
        raise ValueError('authority must match this machine, boot, loader, manifest, demo script and mode')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=('auto_demo', 'bit_only'), required=True)
    p.add_argument('--loader', type=Path, default=Path(__file__).with_name('boot_loader.py'))
    p.add_argument('--demo', type=Path, default=Path(__file__).with_name('demo_service.py'))
    p.add_argument('--motor-standing', type=Path, help='standing BTN0 permission file (enables the motor)')
    p.add_argument('--release', type=Path, default=RELEASE, help='release folder holding runtime/')
    p.add_argument('--data', type=Path, default=DATA, help='demo run folder')
    p.add_argument('--deploy', type=Path, default=DEPLOY, help='firmware folder holding manifest.json')
    p.add_argument('--live-stream', action='store_true', help='demo samples the PL live camera stream')
    p.add_argument('--authority', type=Path)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--output', required=True, type=Path)
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError('receipt already exists')
    identity = {k: Path(f).read_text().strip() for k, f in
                (('machine_id', '/etc/machine-id'), ('boot_id', '/proc/sys/kernel/random/boot_id'))}
    standing = a.motor_standing.resolve(strict=True) if a.motor_standing else None
    if not (a.release / 'runtime/r33/live.py').is_file():
        raise ValueError('release has no runtime/r33/live.py')
    config = build_config(a.mode, a.demo.resolve(strict=True), standing, a.release, a.data,
                          a.deploy.resolve(strict=True), a.live_stream)
    spec = importlib.util.spec_from_file_location('scan3d_boot_checked', a.loader)
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    loader.checked_image(config)                        # hashes only; nothing is loaded
    if a.mode == 'auto_demo':
        loader.demo_command(config)
    boot = Path('/boot/boot.py')
    target = Path('/boot/scan3d_boot_config.json')
    if boot.is_symlink() or not boot.is_file() or target.is_symlink():
        raise ValueError('unexpected boot file type')
    result = {'schema': 'SCAN3D_BOOT_INSTALL_V1', **identity, 'mode': a.mode, 'loader_sha256': sha(a.loader),
              'manifest_sha256': config['manifest_sha256'],
              'demo_sha256': sha(a.demo) if a.mode == 'auto_demo' else None,
              'motor_standing_sha256': sha(standing) if standing else None,
              'previous_boot_sha256': sha(boot), 'config': config, 'applied': False,
              'fpga_loads': 0, 'camera_commands': 0, 'motor_commands': 0}
    if a.apply:
        if a.authority is None:
            raise ValueError('authority file required')
        check_authority(json.loads(a.authority.read_text(encoding='utf8')), identity, result)
        if os.geteuid() != 0:
            raise PermissionError('sudo is required')
        stamp = time.strftime('%Y%m%d_%H%M%S', time.gmtime()) + '_' + identity['boot_id'][:8]
        backup = Path('/boot/boot.py.orig_' + stamp)
        before = boot.read_bytes()
        with backup.open('xb') as out:
            out.write(before)
            out.flush()
            os.fsync(out.fileno())
        if sha(backup) != result['previous_boot_sha256']:
            raise ValueError('backup verification failed')
        old_config = target.read_bytes() if target.exists() else None
        try:
            atomic_write(target, (json.dumps(config, indent=2) + '\n').encode())
            atomic_write(boot, a.loader.read_bytes())
            if sha(boot) != result['loader_sha256']:
                raise ValueError('installed loader readback mismatch')
        except Exception:
            atomic_write(boot, before)
            if old_config is not None:
                atomic_write(target, old_config)
            elif target.exists():
                target.unlink()
            raise
        result.update(applied=True, backup=str(backup), backup_sha256=sha(backup), config_sha256=sha(target))
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open('x') as out:
        json.dump(result, out, indent=2)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
