"""R43 active abort, metadata flush, following full-frame and backpressure QA."""
from pathlib import Path
import hashlib,json,os,subprocess,sys
ROOT=Path(__file__).resolve().parents[1];OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=False)
BASE=Path(sys.argv[2]);IVER=Path('D:/Tools/structured_light_iverilog/bin')
SRC=[ROOT/'rtl/shared_window_filter.sv',ROOT/'baseline_rtl/ridge_center_solver.sv',ROOT/'rtl/direction_refiner.sv',ROOT/'rtl/stripe_extractor.sv',ROOT/'rtl/pixel_frontend.sv',ROOT/'vendor/lane_sweep/rtl/pfo3_tdp_ram.sv',ROOT/'vendor/lane_sweep/rtl/pfo3_banked_ram.sv',ROOT/'tests/lane_sweep_tb.sv']
(OUT/'sources').mkdir()
for p in SRC:(OUT/'sources'/p.name).write_bytes(p.read_bytes())
rows=[]
configs=[(2,0,0,0,0),(2,1,0,0,0),(4,1,0,0,0)] if len(sys.argv)<4 else [tuple(map(int,c.split(','))) for c in sys.argv[3].split(';')]
for config in configs:
 lanes,dispatch,banked,banked_results,balanced=(*config,0) if len(config)==4 else config
 tag=f'l{lanes}d{dispatch}b{banked}r{banked_results}t{balanced}';vvp=OUT/f'{tag}.vvp'
 with (OUT/f'{tag}_compile.log').open('w') as f:subprocess.run([str(IVER/'iverilog.exe'),'-g2012','-s','lane_sweep_tb',f'-Plane_sweep_tb.LANES={lanes}',f'-Plane_sweep_tb.DISPATCH={dispatch}',f'-Plane_sweep_tb.BANKED_REQUESTS={banked}',f'-Plane_sweep_tb.BANKED_RESULTS={banked_results}',f'-Plane_sweep_tb.BALANCED_DISPATCH={balanced}','-o',str(vvp),*[str(OUT/'sources'/p.name) for p in SRC]],stdout=f,stderr=subprocess.STDOUT,check=True)
 rec=OUT/f'{tag}.records';geo=OUT/f'{tag}.geometry';log=OUT/f'{tag}.log'
 with log.open('w') as f:subprocess.run([str(IVER/'vvp.exe'),str(vvp),f'+INPUT={ROOT / "vendor/lane_sweep/sim/T05_000.hex"}',f'+OUTPUT={rec}',f'+GEOMETRY={geo}','+CONTROL=1','+BACKPRESSURE=1'],stdout=f,stderr=subprocess.STDOUT,check=True,timeout=1800)
 txt=log.read_text();assert 'TEST_PASS' in txt and 'CONTROL_PASS' in txt,txt
 assert rec.read_bytes()==(BASE/'T05_l2.records').read_bytes()
 assert geo.read_bytes()==(BASE/'T05_l2.geometry').read_bytes()
 rows.append(dict(lanes=lanes,dispatch=dispatch,banked_requests=banked,banked_results=banked_results,balanced_dispatch=balanced,passed=True,active_abort=True,metadata_flush=True,following_full_frame_equal=True,backpressure_stability=True,record_sha256=hashlib.sha256(rec.read_bytes()).hexdigest()))
 print(rows[-1],flush=True)
(OUT/'RESULT.json').write_text(json.dumps(dict(passed=True,evidence_kind='RTL_SIMULATION_NOT_BOARD',tests=rows,sources={str(p.relative_to(ROOT)):hashlib.sha256((OUT/'sources'/p.name).read_bytes()).hexdigest() for p in SRC}),indent=2))
