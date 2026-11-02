"""CPU-only ARM ASan/UBSan maximum-frame check, with retained build/run logs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
def write(path,data):
    with path.open('x',encoding='utf-8') as f:
        json.dump(data,f,indent=2);f.flush();os.fsync(f.fileno())

def main():
    p=argparse.ArgumentParser();p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args()
    out=a.output_dir.resolve()
    if ROOT not in out.parents or not out.name.startswith('asan_'):
        raise ValueError('new candidate asan output required')
    out.mkdir(parents=True,exist_ok=False)
    source=ROOT/'runtime/candidate/native/assemble_r26.c';lib=out/'libassemble_asan.so'
    cmd=['gcc','-O1','-g','-fno-omit-frame-pointer','-fno-strict-aliasing','-fPIC','-shared',
         '-fsanitize=address,undefined','-fno-sanitize-recover=all','-std=c99','-Wall','-Wextra','-Werror',str(source),'-o',str(lib),'-lz']
    b=subprocess.run(cmd,capture_output=True,text=True,timeout=120)
    write(out/'BUILD.json',dict(argv=cmd,exit_code=b.returncode,stdout=b.stdout,stderr=b.stderr,source_sha256=hashlib.sha256(source.read_bytes()).hexdigest()))
    if b.returncode:return b.returncode
    asan=subprocess.check_output(['gcc','-print-file-name=libasan.so'],text=True).strip()
    if not Path(asan).is_file():raise RuntimeError('libasan missing')
    env={**os.environ,'LD_PRELOAD':asan,'ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1:abort_on_error=1:strict_string_checks=1',
         'UBSAN_OPTIONS':'halt_on_error=1:print_stacktrace=1','OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','PYTHONDONTWRITEBYTECODE':'1'}
    cmd=[sys.executable,'-B',str(ROOT/'verification/assembler/asan_driver.py'),'--library',str(lib),'--iterations','2']
    run=subprocess.run(cmd,capture_output=True,text=True,env=env,timeout=180)
    write(out/'RUN.json',dict(argv=cmd,exit_code=run.returncode,stdout=run.stdout,stderr=run.stderr,
                             library_sha256=hashlib.sha256(lib.read_bytes()).hexdigest(),fpga_loads=0,camera_commands=0,motor_commands=0))
    print(json.dumps(dict(output_dir=str(out),exit_code=run.returncode,stdout=run.stdout,stderr=run.stderr)),flush=True)
    return 0 if run.returncode==0 else 1
if __name__=='__main__':raise SystemExit(main())
