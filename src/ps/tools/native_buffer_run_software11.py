"""Explicit cold-start full-chain experiment; production defaults stay untouched."""
import argparse,json,sys,time
from pathlib import Path
RUNTIME=Path(__file__).resolve().parents[1]
for p in (RUNTIME,RUNTIME/'structured_light_system_algorithm_v1/src/python',RUNTIME/'structured_light_system_geometry_v1/src/python'):
 if str(p) not in sys.path:sys.path.insert(0,str(p))

def main(argv=None):
 p=argparse.ArgumentParser(add_help=False)
 p.add_argument('--experiment-receipt',type=Path,required=True)
 a,args=p.parse_known_args(argv)
 if a.experiment_receipt.exists():raise FileExistsError(a.experiment_receipt)
 # NEW executable only; spawn imports this module without executing main().
 from candidate.native_buffer_experiment_r26 import install_cold_start,MAX_CAPACITY
 buffer=install_cold_start(acknowledge_new_process_no_calls_or_aliases=True)
 from tools.r26_scan import main as scan_main
 started=time.perf_counter();code=None;error=None
 try:
  code=scan_main(args)
 except BaseException as exc:
  error=type(exc).__name__+': '+str(exc)
  raise
 finally:
  receipt=dict(experiment='EXPLICIT_COLD_NATIVE_OUTPUT_BUFFER',production_default_changed=False,
   original_eager_buffer_bytes=MAX_CAPACITY,actual_output_buffer_bytes=buffer.allocated_bytes,
   native_calls=buffer.calls,growth_events=buffer.growth_events,all_protocol_checks_retained=True,
   copied_immutable_returns=True,exit=code,error=error,wall_s=time.perf_counter()-started,
   evidence='ACTUAL_FULL_CHAIN_RESULTS_MUST_BE_REVIEWED_SEPARATELY',automatic_retry=False)
  with a.experiment_receipt.open('x') as f:json.dump(receipt,f,indent=2)
 return code

if __name__=='__main__':raise SystemExit(main())
