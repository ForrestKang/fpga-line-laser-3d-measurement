"""Build the small host/ARM R33 strict assembler with the local gcc."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path)
    p.add_argument('--cc', default=os.environ.get('CC', 'gcc'))
    p.add_argument('--sanitize', action='store_true')
    a = p.parse_args()
    root = Path(__file__).resolve().parent
    source = root/'native/assemble_r33.c'
    out = a.output or root/'native'/('assemble_r33_v3.dll' if os.name == 'nt' else 'libassemble_r33.so')
    if out.exists():
        raise SystemExit('Refuse existing library; choose a new output file')
    out.parent.mkdir(parents=True, exist_ok=True)
    cc = shutil.which(a.cc)
    if not cc:
        raise SystemExit('Compiler unavailable: '+a.cc)
    flags = ['-O3', '-std=c99', '-Wall', '-Wextra', '-Werror', '-shared']
    if os.name != 'nt':
        flags += ['-fPIC']
    if a.sanitize:
        flags += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-g']
    subprocess.run([cc, *flags, str(source), '-o', str(out)], check=True)
    report = dict(schema='R33_NATIVE_ASSEMBLER_BUILD_V1', compiler=cc,
                  compiler_version=subprocess.check_output([cc, '--version'], text=True).splitlines()[0],
                  flags=flags, machine=platform.machine(), python=sys.version,
                  source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  output=out.name, output_bytes=out.stat().st_size,
                  output_sha256=hashlib.sha256(out.read_bytes()).hexdigest())
    with out.with_suffix(out.suffix+'.json').open('x') as f:
        json.dump(report, f, indent=2)
        f.write('\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
