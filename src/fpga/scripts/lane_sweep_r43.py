"""R43 full-size frontend lane sweep; RTL simulation only, no board effects."""
from pathlib import Path
import argparse,hashlib,json,os,re,subprocess,sys,time
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('output');parser.add_argument('--dispatch',type=int,choices=(0,1),default=0);parser.add_argument('--quick',action='store_true');parser.add_argument('--extended',action='store_true');parser.add_argument('--cases');parser.add_argument('--baseline');parser.add_argument('--lanes',default='2,4,8');parser.add_argument('--banked',type=int,choices=(0,1),default=0);parser.add_argument('--banked-results',type=int,choices=(0,1),default=0);parser.add_argument('--balanced',type=int,choices=(0,1),default=0);args=parser.parse_args();lane_values=[int(n) for n in args.lanes.split(',')];assert lane_values and all(n in (2,4,8) for n in lane_values);base_lane=lane_values[0]
OUT=Path(args.output).resolve();OUT.mkdir(parents=True,exist_ok=False)
os.environ.update(TEMP='D:/Workspace/CodexTemp/r43_lane_sweep',TMP='D:/Workspace/CodexTemp/r43_lane_sweep')
Path(os.environ['TEMP']).mkdir(parents=True,exist_ok=True)
IVER=Path('D:/Tools/structured_light_iverilog/bin')
SIM=ROOT/'vendor/lane_sweep/sim'
SOURCES=[ROOT/'rtl/r41_window_filter.sv',ROOT/'baseline_rtl/r33_direction.sv',ROOT/'rtl/r35_direction.sv',ROOT/'rtl/r35_row.sv',ROOT/'rtl/r35_frontend.sv',ROOT/'vendor/lane_sweep/rtl/pfo3_tdp_ram.sv',ROOT/'vendor/lane_sweep/rtl/pfo3_banked_ram.sv',ROOT/'tests/lane_sweep_tb.sv']
# Freeze the actual sources before compile, including all lane variants.
src=OUT/'sources';src.mkdir()
shas={}
for p in SOURCES:
 b=p.read_bytes();(src/p.name).write_bytes(b);shas[str(p.relative_to(ROOT))]=hashlib.sha256(b).hexdigest()
# Wide stripes and distributed narrow peaks exercise a row's detector/queue
# without importing CAD or an expected object shape into the algorithm.
y,x=np.indices((480,752));wide=np.full((480,752),12,dtype=np.uint8)
for c,w in [(80,7),(250,12),(435,18),(650,25)]:
 d=x-(c+8*np.sin(y/43.0));wide=np.maximum(wide,(12+200*np.exp(-(d/w)**2/2)).astype(np.uint8))
wide_path=OUT/'wide.hex';wide_path.write_text('\n'.join(f'{n:02x}' for n in wide.ravel())+'\n')
# 16 separated smooth narrow stripes, spread across every column partition.
# Stress all lane queues while staying within the row-qualified contract.
dense=np.full((480,752),8,dtype=np.uint8)
for c in range(24,752,46):
 d=x-(c+2*np.sin(y/29.0));dense=np.maximum(dense,(8+220*np.exp(-(d/1.6)**2/2)).astype(np.uint8))
dense_path=OUT/'dense16.hex';dense_path.write_text('\n'.join(f'{n:02x}' for n in dense.ravel())+'\n')
sharp=np.full((480,752),12,dtype=np.uint8)
for c,w in [(70,12),(235,17),(440,18),(645,25)]:sharp[np.abs(x-c)<=w/2]=232
sharp_path=OUT/'wide_sharp.hex';sharp_path.write_text('\n'.join(f'{n:02x}' for n in sharp.ravel())+'\n')
cluster=np.full((480,752),8,dtype=np.uint8)
for c in range(16,160,9):cluster=np.maximum(cluster,(8+220*np.exp(-((x-c)/0.8)**2/2)).astype(np.uint8))
cluster_path=OUT/'cluster16.hex';cluster_path.write_text('\n'.join(f'{n:02x}' for n in cluster.ravel())+'\n')
cases={'wide_sharp':sharp_path,'cluster16':cluster_path,'T05':SIM/'T05_000.hex','T06':SIM/'T06_000.hex','noise':SIM/'noise.hex','wide':wide_path,'dense16':dense_path,'empty':SIM/'empty.hex','slots17':SIM/'slots17.hex'}
if args.quick:cases={k:cases[k] for k in ('T05','T06','noise','wide')}
if args.extended:cases={k:cases[k] for k in ('dense16','empty','slots17','wide_sharp','cluster16')}
if args.cases:cases={k:cases[k] for k in args.cases.split(',')}
rows=[];beg=time.time()
def run(args,log):
 with log.open('w') as f:subprocess.run([str(a) for a in args],cwd=OUT,stdout=f,stderr=subprocess.STDOUT,check=True,timeout=1800)
for lanes in lane_values:
 vvp=OUT/f'l{lanes}.vvp'
 run([IVER/'iverilog.exe','-g2012','-s','lane_sweep_tb',f'-Plane_sweep_tb.LANES={lanes}',f'-Plane_sweep_tb.DISPATCH={args.dispatch}',f'-Plane_sweep_tb.BANKED_REQUESTS={args.banked}',f'-Plane_sweep_tb.BANKED_RESULTS={args.banked_results}',f'-Plane_sweep_tb.BALANCED_DISPATCH={args.balanced}','-o',vvp,*[src/p.name for p in SOURCES]],OUT/f'compile_l{lanes}.log')
 for name,inp in cases.items():
  out=OUT/f'{name}_l{lanes}.records';geo=OUT/f'{name}_l{lanes}.geometry';log=OUT/f'{name}_l{lanes}.log'
  t=time.time();run([IVER/'vvp.exe',vvp,f'+INPUT={inp}',f'+OUTPUT={out}',f'+GEOMETRY={geo}'],log)
  txt=log.read_text();assert 'TEST_PASS' in txt,txt
  m=re.search(r'RESULT (.+)',txt);metric={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',m.group(1))}
  metric.update(dispatch=args.dispatch,banked_requests=args.banked,banked_results=args.banked_results,balanced_dispatch=args.balanced,case=name,elapsed_s=time.time()-t,input_sha256=hashlib.sha256(inp.read_bytes()).hexdigest(),record_sha256=hashlib.sha256(out.read_bytes()).hexdigest(),geometry_sha256=hashlib.sha256(geo.read_bytes()).hexdigest(),lane_utilization=[dict(index=int(i),busy=int(b),starts=int(s),done=int(d)) for i,b,s,d in re.findall(r'LANE (\d+) busy=(\d+) starts=(\d+) done=(\d+)',txt)])
  if lanes!=base_lane:
   assert out.read_bytes()==(OUT/f'{name}_l{base_lane}.records').read_bytes(),f'record mismatch {name}/{lanes}'
   assert geo.read_bytes()==(OUT/f'{name}_l{base_lane}.geometry').read_bytes(),f'geometry mismatch {name}/{lanes}'
  if args.baseline and (Path(args.baseline)/f'{name}_l2.records').is_file():
   assert out.read_bytes()==(Path(args.baseline)/f'{name}_l2.records').read_bytes(),f'external baseline record mismatch {name}/{lanes}'
   assert geo.read_bytes()==(Path(args.baseline)/f'{name}_l2.geometry').read_bytes(),f'external baseline geometry mismatch {name}/{lanes}'
  rows.append(metric)
  (OUT/'PARTIAL.json').write_text(json.dumps(rows,indent=2))
  print(json.dumps(metric),flush=True)
# Exercise deterministic downstream backpressure on both extreme lane counts.
for lanes in sorted(set([lane_values[0],lane_values[-1]])):
 out=OUT/f'T05_bp_l{lanes}.records';geo=OUT/f'T05_bp_l{lanes}.geometry';log=OUT/f'T05_bp_l{lanes}.log'
 run([IVER/'vvp.exe',OUT/f'l{lanes}.vvp',f'+INPUT={SIM / "T05_000.hex"}',f'+OUTPUT={out}',f'+GEOMETRY={geo}','+BACKPRESSURE=1'],log)
 if lanes==base_lane:
  bp_records=out.read_bytes();bp_geometry=geo.read_bytes()
 else:
  assert out.read_bytes()==bp_records,'backpressure records mismatch'
  assert geo.read_bytes()==bp_geometry,'backpressure geometry mismatch'
 if 'T05' in cases:
  assert out.read_bytes()==(OUT/f'T05_l{base_lane}.records').read_bytes(),'backpressure/unblocked records mismatch'
  assert geo.read_bytes()==(OUT/f'T05_l{base_lane}.geometry').read_bytes(),'backpressure/unblocked geometry mismatch'
result=dict(passed=True,dispatch=args.dispatch,banked_requests=args.banked,banked_results=args.banked_results,balanced_dispatch=args.balanced,evidence_kind='FULL_SIZE_RTL_SIMULATION_NOT_BOARD',resolution=[752,480],cases=list(cases),lanes=lane_values,record_and_geometry_byte_equal=True,backpressure_equivalent=True,sources=shas,results=rows,elapsed_s=time.time()-beg)
(OUT/'RESULT.json').write_text(json.dumps(result,indent=2));print('ALL_PASS',flush=True)
