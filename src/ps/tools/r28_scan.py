"""R28 DDR replay release entry; camera and motion remain outside this scope.

The original R26 entry remains available for explicit historical comparisons.
The boot-bound attach manifest is created by the admitted firmware load, never
copied from an older board boot. No loading or hardware command occurs here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))


def release_arguments(argv):
    args = list(argv)
    supplied = {item.split('=', 1)[0] for item in args if item.startswith('--')}
    defaults = {
        '--manifest': 'runtime/r28_prep/attach.json',
        '--journal-buffer-kib': '2048',
        '--display-backend': 'process',
        '--geometry-backend': 'prepared_serial',
        '--writer-group-size': '8',
        '--writer-max-delay-ms': '50',
        '--raw-codec': 'zlib1',
        '--feature-workers': '1',
    }
    for flag, value in defaults.items():
        if flag not in supplied:
            args.extend((flag, value))
    for flag in ('--display-native-finite-validation-only',
                 '--display-release-storage-on-close'):
        if flag not in supplied:
            args.append(flag)
    return args


def main(argv=None):
    # The fused column conversion decodes final PL observations. It never
    # extracts pixels, links image candidates or computes raw-image quality.
    os.environ.setdefault('SCAN3D_R28_OBSERVATIONS', 'native')
    from tools.r26_scan import main as run
    return run(release_arguments(sys.argv[1:] if argv is None else argv))


if __name__ == '__main__':
    raise SystemExit(main())
