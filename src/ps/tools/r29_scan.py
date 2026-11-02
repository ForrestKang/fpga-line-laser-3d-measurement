"""R29 complete actual-PL replay; never starts a camera, laser or motor.

R28 remains an explicit comparison entry.  R29 selects the content-bound
immutable shared-geometry path; all protocol, durability and capacity gates
remain in the common release implementation.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))


def release_arguments(argv):
    args=list(argv)
    if not any(x.split('=',1)[0]=='--writer-max-delay-ms' for x in args):
        args.extend(('--writer-max-delay-ms','180'))
    return args


def main(argv=None):
    os.environ.setdefault('SCAN3D_R29_GEOMETRY', 'shared')
    os.environ.setdefault('SCAN3D_R29_OBSERVATIONS', 'fused')
    os.environ.setdefault('SCAN3D_R29_TX_CACHE', 'baseline')
    os.environ['SCAN3D_RELEASE_PROFILE'] = 'R29_FULL_CHAIN_V1'
    from tools.r28_scan import main as run
    return run(release_arguments(sys.argv[1:] if argv is None else argv))


if __name__ == '__main__':
    raise SystemExit(main())
