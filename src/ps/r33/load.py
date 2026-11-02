"""Load audited PFO3 for DDR replay only, with physical12V-off proof and lock.

Does not initialize/capture the camera, ARM the guard, START a motor, or allocate
DMA buffers. This is not a formal camera/motion handoff receipt.
"""
from pathlib import Path
import argparse,hashlib,json,os,platform,sys
ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT/'structured_light_system_algorithm_v1/src/python',ROOT/'structured_light_system_geometry_v1/src/python'):
    sys.path.insert(0,str(p))

def save(path,record):
    with path.open('x',encoding='utf8') as stream:
        json.dump(record,stream,indent=2);stream.flush();os.fsync(stream.fileno())

def main():
    p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--power-off-proof',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if platform.system()!='Linux' or platform.machine() not in ('armv7l','armv7'):raise RuntimeError('actual ARM board required')
    proof=json.loads(args.power_off_proof.read_text());boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    machine=Path('/etc/machine-id').read_text().strip()
    if (proof.get('physical_power_off_confirmed') is not True or proof.get('boot_id')!=boot or
            proof.get('machine_id')!=machine or proof.get('source')!='explicit_current_user_statement' or
            not proof.get('user_statement')):raise ValueError('12V-off user authority/device/boot binding required')
    manifest=json.loads(args.manifest.read_text())
    if manifest.get('pfo3',{}).get('enabled') is not True or manifest.get('motion_enabled') is not False:
        raise ValueError('explicit static/replay PFO3 manifest required')
    from structured_light.motion.pynq_runtime import verify_stage_b_artifacts
    from structured_light.motion.lifecycle import HardwareProcessLock
    artifacts=verify_stage_b_artifacts(args.manifest)
    from candidate.pfo3_hwh_r25 import audit_hwh
    audit_hwh(artifacts['hwh'])
    args.output.mkdir(parents=True,exist_ok=False)
    lock=HardwareProcessLock(label='r33_audited_static_load_12V_disconnected').acquire()
    record=dict(schema='R33_STATIC_REPLAY_LOAD_V1',machine_id=machine,boot_id=boot,power_off_proof=proof,
        motor_START=0,guard_ARM=0,camera_commands=0,DMA_buffers_allocated=0,formal_capture_handoff=False,
        hardware_execution_verified=False,passed=False)
    try:
        from pynq import Overlay,PL,MMIO
        previous=PL.bitfile_name
        save(args.output/'ADMISSION.json',{**record,'previous_bit':previous,'artifacts':artifacts['files']})
        overlay=Overlay(artifacts['bit'],download=True)
        loaded=Path(PL.bitfile_name).resolve()
        if loaded!=Path(artifacts['bit']).resolve() or hashlib.sha256(loaded.read_bytes()).hexdigest()!=manifest['files']['bit']['sha256']:
            raise ValueError('loaded bit identity mismatch')
        if 'pfo3_frontend' not in overlay.ip_dict or 'pfo3_dma' not in overlay.ip_dict:raise ValueError('new interface absent')
        frontend=MMIO(overlay.ip_dict['pfo3_frontend']['phys_addr'],4096)
        observed={str(offset):frontend.read(offset) for offset in (0,4,8,0x14,0x18,0x1c)}
        if tuple(observed[str(offset)] for offset in (0,4,8))!=(0x57334650,0x21000,7):raise ValueError('actual PFO3 ABI mismatch')
        if observed[str(0x14)]&1:raise ValueError('new frontend unexpectedly active')
        record.update(passed=True,loaded_bit=str(loaded),bit_sha256=manifest['files']['bit']['sha256'],
            hwh_sha256=manifest['files']['hwh']['sha256'],actual_PFO3_registers=observed,
            readback_only_after_load=True,FPGA_loads=1)
        save(args.output/'LOADED_REPLAY_ONLY.json',record)
        print(json.dumps(record));return 0
    except BaseException as exc:
        record.update(error=type(exc).__name__+': '+str(exc),automatic_retry=False,passed=False)
        try:save(args.output/'ERROR.json',record)
        except BaseException:pass
        raise
    finally:
        # No DMA buffers or requests were ever allocated/armed by this loader.
        lock.release()

if __name__=='__main__':raise SystemExit(main())
