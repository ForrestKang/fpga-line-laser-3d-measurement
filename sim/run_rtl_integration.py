"""Adapt PATHS ONLY in a restored historical test; assertions stay unchanged."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--work', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--iverilog', default='iverilog')
    ap.add_argument('--vvp', default='vvp')
    ap.add_argument('--cases', nargs='+', default=['empty'])
    a = ap.parse_args()
    root = a.work.resolve()/'fpga'
    if a.output.exists():
        raise FileExistsError(a.output)
    iver, vvp = shutil.which(a.iverilog), shutil.which(a.vvp)
    if not iver or not vvp:
        raise SystemExit('Install Icarus Verilog and vvp, or pass explicit executable paths')
    source = root/'scripts/integration_verify_final.py'
    s = source.read_text(encoding='utf-8')
    lines = s.splitlines()
    def replace_line(prefix, replacement):
        matches = [i for i,l in enumerate(lines) if l.startswith(prefix)]
        if len(matches) != 1:
            raise ValueError('Unexpected frozen script structure: '+prefix)
        lines[matches[0]] = replacement
    replace_line('VERIFY = ', "VERIFY = ROOT / 'vendor/verification'")
    replace_line('R36 = ', "R36 = ROOT")
    replace_line("sys.path.insert(0, str(VERIFY / 'r36_runtime'", "sys.path.insert(0, str(ROOT / 'runtime'))")
    replace_line('IVER = ', 'IVER = None  # executable paths configured by portable wrapper')
    replace_line('os.environ.update(TEMP=', "os.environ.update(TEMP=str(OUT / 'temp'), TMP=str(OUT / 'temp'))")
    s = '\n'.join(lines)+'\n'
    old = "'D:/Workspace/CodexWork/scan3d_r37_cdc_threshold_20261026_01/stageA13/boardprep21_session_board31/board/r37_r41g_live02/raw_000015.gray8'"
    if old not in s:
        raise ValueError('Unexpected raw fixture path')
    s=s.replace(old,"'r37_r41g_live02/raw_000015.gray8'")
    old="Path('D:\\\\Workspace\\\\CodexWork\\\\scan3d_r42_buttons_boot_20261029_01\\\\tests\\\\xpm_stubs_sim.v')"
    if old not in s:
        raise ValueError('Unexpected XPM mock path')
    s=s.replace(old,"(ROOT / 'tests/xpm_stubs_sim.v')")
    s=s.replace("IVER / 'iverilog.exe'",repr(iver)).replace("IVER / 'vvp.exe'",repr(vvp))
    adapted = root/'scripts/integration_verify_submission.py'
    with adapted.open('x',encoding='utf-8') as f:
        f.write(s)
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1')
    return subprocess.run([sys.executable,'-B',str(adapted),str(a.output.resolve()),*a.cases],
                          cwd=root,env=env).returncode


if __name__ == '__main__':
    raise SystemExit(main())
