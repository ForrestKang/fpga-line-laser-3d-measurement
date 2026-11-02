"""Pin all compiled HDL plus scripts, headers and effective input constraints."""
from pathlib import Path
import argparse,hashlib,json
ROOT=Path(__file__).resolve().parents[1]

def snapshot(source_list):
    paths={Path(line.strip()) for line in Path(source_list).read_text().splitlines() if line.strip()}
    paths.update((ROOT/'pl/r25_next/rtl').glob('*.svh'))
    paths.update((ROOT/'pl/integration').glob('*.tcl'))
    paths.update((ROOT/'host/vivado_r25_v9').glob('*.tcl'))
    paths.update((ROOT/'pl/baseline').rglob('*.xdc'))
    paths.update((ROOT/'pl/baseline/scripts').glob('constrain_*.tcl'))
    for extension in ('*.bd','*.xci','*.xdc'):
        paths.update((ROOT/'pl/build/full_project_r25_v9').rglob(extension))
    result={}
    for path in sorted(paths):
        if not path.is_file():raise FileNotFoundError(path)
        data=path.read_bytes()
        result[path.resolve().as_posix()]=dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('list',type=Path);p.add_argument('receipt',type=Path)
    p.add_argument('--verify',action='store_true');args=p.parse_args()
    current=snapshot(args.list)
    if args.verify:
        if json.loads(args.receipt.read_text())!=current:raise RuntimeError('compiled source changed during build')
    else:
        with args.receipt.open('x',encoding='utf8') as stream:json.dump(current,stream,indent=2)
    print(json.dumps(dict(files=len(current),source_unchanged=args.verify)))

if __name__=='__main__':main()
