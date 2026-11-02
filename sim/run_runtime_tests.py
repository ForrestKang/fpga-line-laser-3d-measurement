"""Offline PC mock tests only; use a freshly restored working tree."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--work', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    a = ap.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    tmp = a.output.resolve()/'temp'
    tmp.mkdir()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
               TEMP=str(tmp), TMP=str(tmp))
    runtime = a.work.resolve()/'runtime'
    with (a.output/'console.log').open('x', encoding='utf-8') as log:
        result = subprocess.run([sys.executable, '-B', str(runtime/'run_tests_final_section.py'),
                                 '--report', str(a.output.resolve()/'runtime_result.json')],
                                cwd=runtime, env=env, stdout=log, stderr=subprocess.STDOUT)
    print('Result:', a.output.resolve()/'runtime_result.json', 'exit:', result.returncode)
    raise SystemExit(result.returncode)
