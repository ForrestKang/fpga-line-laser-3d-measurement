"""Restore and rebuild with the original Vivado flow. Does not load any board."""
import argparse
from pathlib import Path
import shutil
import subprocess
from restore_sources import restore

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--work', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--vivado', default='vivado')
    a = ap.parse_args()
    if a.output.exists():
        raise FileExistsError(a.output)
    exe = shutil.which(a.vivado)
    if not exe:
        raise SystemExit('Vivado unavailable; install Vivado 2025.2 and its xc7z020 device support')
    work = restore(a.work)
    cmd = [exe, '-mode', 'batch', '-source', str(work/'fpga/scripts/build_final.tcl'),
           '-tclargs', str(a.output.resolve()), '2', '0', '0', '0', '0']
    print('Invoking original flow; reports and failures stay in the new output directory.')
    subprocess.run(cmd, cwd=work/'fpga', check=True)
