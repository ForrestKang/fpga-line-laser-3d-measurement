"""Separate HDMI owner process for the live scan display (started by scan3d_board.py live).

Runs the unmodified ``hdmi_owner.serve`` loop (signed-overlay checks, bounded
RPC, quarantine on unconfirmed VDMA stop) with ``create_output`` returning a
ScanHDMIOutput, so rendering happens here and never in the motion process.
"""

import os
from pathlib import Path
import sys


def serve(socket_path: str, manifest: str, journal: str) -> None:
    source = Path(os.environ["SL_SCAN3D_SOURCE_ROOT"]).resolve(strict=True)
    geometry = Path(os.environ["SL_SCAN3D_GEOMETRY_ROOT"]).resolve(strict=True)
    sys.path[:0] = [str(source / "src" / "python"), str(geometry / "src" / "python")]
    from structured_light.p2 import hdmi_owner
    from structured_light.scan3d.hdmi import install_scan_output_factory

    install_scan_output_factory()
    hdmi_owner.serve(socket_path, manifest, journal)


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--serve":
        serve(*sys.argv[2:])
    else:
        raise SystemExit("internal live-scan HDMI owner; started by scan3d_board.py live")
