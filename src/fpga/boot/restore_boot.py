"""Put the original /boot/boot.py back from its verified backup. Default: check only.

Also stops a running scan3d-demo unit when --apply is given. Does not reboot or load a bit.
"""
import argparse
import json
import os
import subprocess
from pathlib import Path

from install_boot import atomic_write, sha


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backup', type=Path, required=True)
    p.add_argument('--backup-sha256', required=True)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError('receipt exists')
    b = a.backup
    if (b.parent != Path('/boot') or not b.name.startswith('boot.py.orig_') or b.is_symlink()
            or sha(b) != a.backup_sha256):
        raise ValueError('exact original boot backup with matching SHA required')
    target = Path('/boot/boot.py')
    result = {'schema': 'SCAN3D_BOOT_RESTORE_V1', 'backup': str(b), 'backup_sha256': a.backup_sha256,
              'previous_sha256': sha(target), 'applied': False}
    if a.apply:
        if os.geteuid() != 0:
            raise PermissionError('sudo required')
        subprocess.run(['systemctl', 'stop', 'scan3d-demo'], check=False, timeout=120)
        atomic_write(target, b.read_bytes())
        if sha(target) != a.backup_sha256:
            raise ValueError('restore readback differs')
        result['applied'] = True
    with a.output.open('x') as out:
        json.dump(result, out, indent=2)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
