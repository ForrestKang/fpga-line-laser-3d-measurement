from pathlib import Path
import argparse,hashlib,json
r=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser()
p.add_argument('receipt',type=Path)
p.add_argument('--verify',action='store_true')
p.add_argument('--build-dir',type=Path)
p.add_argument('--source-list',type=Path)
a=p.parse_args()
paths=set()
for base in (r/'source_tree',r/'ip_repo',r/'bd',r/'scripts'):
 for item in sorted(base.rglob('*')):
  if item.is_file() and '__pycache__' not in item.parts:
   paths.add(item.resolve())
if bool(a.build_dir) != bool(a.source_list):
 raise SystemExit('--build-dir and --source-list must be used together')
if a.source_list:
 paths.update(Path(line.strip()).resolve() for line in a.source_list.read_text().splitlines() if line.strip())
 for sub in ('sl_profile_hdmi.srcs','sl_profile_hdmi.gen'):
  for ext in ('*.bd','*.xci','*.xdc'):
   paths.update(item.resolve() for item in (a.build_dir/sub).rglob(ext))
d={}
for item in sorted(paths):
 if not item.is_file():raise FileNotFoundError(item)
 d[item.as_posix()]={'bytes':item.stat().st_size,'sha256':hashlib.sha256(item.read_bytes()).hexdigest()}
out=a.receipt
if a.verify:
 if json.loads(out.read_text())!=d:raise SystemExit('SOURCE CHANGED DURING BUILD')
else:
 with out.open('x') as f:json.dump(d,f,indent=2)
print(json.dumps({'files':len(d),'source_unchanged':a.verify,'includes_generated_sources':bool(a.build_dir)}))
