"""Gate final fixed-RTL configurations on positive OOC, then complete matrices."""
from pathlib import Path
import concurrent.futures,json,subprocess,time,sys
R=Path(__file__).resolve().parents[1];E=R/'evidence';PY=sys.executable
configs=[dict(tag='l8d0b1r1t0',lanes=8,dispatch=0,balanced=0,sweep='LANE_SWEEP_R43_08_STATIC_BANKED',control='LANE_CONTROL_R43_03_STATIC_BANKED'),dict(tag='l4d1b1r1t1',lanes=4,dispatch=1,balanced=1,sweep='LANE_SWEEP_R43_09_BALANCED4',control='LANE_CONTROL_R43_04_BALANCED4')]
def run(c):
 d=E/'LANE_OOC_R43_07'/c['tag'];deadline=time.monotonic()+600
 while not (d/'RESULT.txt').exists():
  if time.monotonic()>deadline:raise TimeoutError(str(d))
  time.sleep(2)
 ls=(d/'timing_synth.rpt').read_text().splitlines();i=next(i for i,v in enumerate(ls) if 'WNS(ns)' in v);vals=ls[i+2].split();c.update(wns=float(vals[0]),whs=float(vals[4]))
 if c['wns']<=0 or c['whs']<=0:c['admitted']=False;return c
 c['admitted']=True;print('ADMITTED',c,flush=True)
 cmd=[PY,'-I','-B','-X','utf8',str(R/'scripts/lane_sweep_r43.py'),str(E/c['sweep']),'--dispatch',str(c['dispatch']),'--banked','1','--banked-results','1','--balanced',str(c['balanced']),'--lanes',str(c['lanes']),'--cases','T05,T06,noise,cluster16,dense16,wide,wide_sharp,slots17,empty','--baseline',str(E/'LANE_SWEEP_R43_01')]
 with (E/(c['sweep']+'_launcher.log')).open('w') as log:subprocess.run(cmd,cwd=R,stdout=log,stderr=subprocess.STDOUT,check=True)
 cmd=[PY,'-I','-B','-X','utf8',str(R/'scripts/lane_sweep_control_r43.py'),str(E/c['control']),str(E/'LANE_SWEEP_R43_01'),f"{c['lanes']},{c['dispatch']},1,1,{c['balanced']}"]
 with (E/(c['control']+'_launcher.log')).open('w') as log:subprocess.run(cmd,cwd=R,stdout=log,stderr=subprocess.STDOUT,check=True)
 c['matrix_pass']=json.loads((E/c['sweep']/'RESULT.json').read_text())['passed'];c['control_pass']=json.loads((E/c['control']/'RESULT.json').read_text())['passed'];return c
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,configs))
(E/'LANE_FINAL_CONFIGS_R43.json').write_text(json.dumps(results,indent=2));print(results,flush=True)
