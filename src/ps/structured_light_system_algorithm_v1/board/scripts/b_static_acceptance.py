"""Load the signed direct-wire candidate with 12V OFF; camera-only acceptance."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src/python'))
from structured_light.motion.pynq_runtime import create_stage_b_runtime

def save(path,value):
    with path.open('x',encoding='utf-8') as f:
        f.write(json.dumps(value,indent=2,default=str)+'\n');f.flush()
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--power-off-proof',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--count',type=int,default=1000)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    power=json.loads(a.power_off_proof.read_text())
    bus=power['snapshot']['0x31']['bus_voltage_v']
    if power.get('motion_command_count') != 0 or not 0<=bus<6 or power['snapshot']['0x31']['speed_rpm'] != 0:
        raise RuntimeError('Fresh driver proof does not establish power-off stationary state')
    runtime=None;started=time.monotonic();result={'hardware':True,'motor_12v_on':False,'motion_started':False,'hidden_retry':False}
    try:
        runtime=create_stage_b_runtime(a.manifest,stage='signals',load=True,power_off_confirmation={'physical_power_off_confirmed':True,'read_only_driver_proof_sha256':hashlib.sha256(a.power_off_proof.read_bytes()).hexdigest()},output=a.output)
        guard=runtime.guard.status()
        if guard['active'] or guard['step_pin'] or guard['heartbeat_pin'] or not guard['enable_pin']:
            raise RuntimeError('BSG1 reset pins are not direct-wire idle')
        from pynq import PL
        loaded={'loaded':True,'overlay_mode':'structured_light_profile_hdmi','active_bitfile':str(PL.bitfile_name),'boot_identity':{'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'machine_id':Path('/etc/machine-id').read_text().strip()},'guard_idle':guard,**{f'{k}_sha256':runtime.artifacts['files'][k]['sha256'] for k in ('bit','hwh','manifest')}}
        save(a.output/'loaded_overlay_receipt.json',loaded)
        print('B_LOADED_IDLE '+json.dumps(guard),flush=True)
        signals=runtime.signals();print('B_SIGNALS_PASS '+str(signals['signals']),flush=True)
        frame=runtime.first_frame();print(f'B_FIRST_PAIR_PASS request={frame.request_id} comparison={frame.comparison.passed}',flush=True)
        for mode in ('raw','profile'):
            summary=runtime.static(count=a.count,mode=mode)
            result[mode]=summary
            print('B_STATIC_BATCH_PASS '+json.dumps(summary),flush=True)
        result['passed']=True
    except BaseException as e:
        result.update(passed=False,error=type(e).__name__+': '+str(e),traceback=traceback.format_exc())
        raise
    finally:
        if runtime is not None:
            try:
                result['cleanup']=runtime.close()
            except BaseException as e:
                result.update(passed=False,cleanup_error=type(e).__name__+': '+str(e))
        result['elapsed_s']=time.monotonic()-started
        save(a.output/'acceptance_summary.json',result)
        print('B_ACCEPTANCE_END '+json.dumps(result),flush=True)
if __name__=='__main__':main()
