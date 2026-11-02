"""Explicit native-library build on the calling computer; no board connection."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import platform
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def plan():
    result=[]
    for name in ('multibranch','quality','geometry','link_candidates'):
        revision='r22' if name=='link_candidates' else 'r21'
        source=ROOT/'candidate/native'/f'{name}_{revision}.c'
        output=source.with_name(f'{name}_{revision}.dll' if os.name=='nt' else f'lib{name}_{revision}.so')
        command=['gcc','-O3','-ffp-contract=off','-std=c99','-Wall','-Wextra','-Werror','-shared']
        if os.name!='nt':command.append('-fPIC')
        command.extend([str(source),'-o',str(output),'-lm'])
        result.append(dict(source=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                           output=str(output),command=command))
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build',action='store_true',help='actually compile locally; existing outputs are refused')
    args=parser.parse_args();rows=plan()
    if args.build:
        for row in rows:
            if Path(row['output']).exists():raise FileExistsError('preserve existing library: '+row['output'])
        temporary=ROOT/'candidate/native/build_temp';temporary.mkdir(parents=True,exist_ok=True)
        env=dict(os.environ,TEMP=str(temporary),TMP=str(temporary),TMPDIR=str(temporary))
        for row in rows:
            result=subprocess.run(row['command'],env=env,capture_output=True,text=True,check=True)
            row.update(stderr=result.stderr,output_sha256=hashlib.sha256(Path(row['output']).read_bytes()).hexdigest())
    print(json.dumps(dict(built=args.build,machine=platform.machine(),libraries=rows,board_connection=False),indent=2))

if __name__=='__main__':main()
