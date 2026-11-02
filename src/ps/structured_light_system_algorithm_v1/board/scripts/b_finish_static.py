"""Finite static B1/B2 checks on the already loaded B Overlay.

No motor ARM/START and no PL download. Visual acknowledgements are bound to
the exact run/phase nonce and cannot be inferred from software DMA success.
"""
from __future__ import annotations
from pathlib import Path
import argparse,csv,json,sys,time,uuid
from dataclasses import asdict

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src/python'))

def capture_metrics(result):
    rows=[r for r in result.diagnostics.rows if r.valid and r.candidate_count==1]
    return {'request_id':result.request_id,'epoch':result.epoch,
            'comparison_passed':bool(result.comparison and result.comparison.passed),
            'raw_bytes':len(result.raw_bytes),'valid_single_rows':len(rows),
            'noninteger_valid_rows':sum(r.xc_fixed%4096!=0 for r in rows)}

def laser_gate(records):
    ids={(r['epoch'],r['request_id']) for r in records}
    coverage=any(r['valid_single_rows']>=50 and r['noninteger_valid_rows']>0 for r in records)
    return {'passed':len(records)==10 and len(ids)==10 and coverage and all(r['comparison_passed'] and r['raw_bytes']==360960 for r in records),
            'capture_count':len(records),'distinct_ids':len(ids),'coverage_met':coverage,
            'valid_rows_total':sum(r['valid_single_rows'] for r in records),
            'noninteger_valid_rows_total':sum(r['noninteger_valid_rows'] for r in records)}

def validate_reply(value,*,phase,nonce):
    if (not isinstance(value,dict) or value.get('phase')!=phase or value.get('nonce')!=nonce
            or value.get('confirmed') is not True or not str(value.get('statement','')).strip()):
        raise ValueError('visual reply must match the current phase/nonce and explicit user statement')
    return True

def validate_cleanup(value):
    if (not isinstance(value,dict) or value.get('closed') is not True
            or not isinstance(value.get('capture_handoff'),str)
            or not value['capture_handoff'].strip()):
        raise RuntimeError('capture success requires confirmed resource cleanup and a new capture handoff')
    return True


def validate_pattern_evidence(value,*,artifacts,boot_id):
    """Reuse a real colorbar confirmation for unchanged signed artifacts."""
    import hashlib
    if value.get('boot_id')!=boot_id or value.get('artifacts')!=artifacts:
        raise ValueError('pattern evidence boot/artifact binding differs')
    source=Path(value['source_file']);raw=source.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=value['source_sha256']:
        raise ValueError('pattern confirmation source hash differs')
    reply=json.loads(raw)
    if (reply.get('phase')!='pattern' or reply.get('confirmed') is not True
            or not str(reply.get('nonce','')).strip() or not str(reply.get('statement','')).strip()):
        raise ValueError('pattern source lacks actual human confirmation')
    return True

def save_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as f:
        f.write(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n');f.flush()
        import os;os.fsync(f.fileno())

def wait_reply(output,phase,*,timeout_s=1800):
    nonce=uuid.uuid4().hex
    state={'phase':phase,'nonce':nonce,'waiting':True,'timeout_s':timeout_s}
    status=output/'live_status.json';temp=output/'live_status.tmp'
    temp.write_text(json.dumps(state),encoding='utf-8');temp.replace(status)
    print('WAIT '+json.dumps(state),flush=True)
    deadline=time.monotonic()+timeout_s;reply=output/(phase+'_reply.json')
    while time.monotonic()<deadline:
        if reply.exists():
            value=json.loads(reply.read_text(encoding='utf-8'))
            validate_reply(value,phase=phase,nonce=nonce)
            save_json(output/(phase+'_accepted.json'),value);return value
        time.sleep(.25)
    raise TimeoutError('operator confirmation deadline: '+phase)

def export_result(result,output,index):
    from structured_light.p2.camera_evidence import export_gray
    from structured_light.motion.pynq_runtime import StageBHDMIAdapter
    from PIL import Image
    import numpy as np
    folder=output/f'frame_{index:03d}'
    stats=export_gray(result.raw_bytes,width=752,height=480,output=folder,request_id=result.request_id)
    canvas=StageBHDMIAdapter.build_canvas(result)
    bgr=np.frombuffer(canvas,np.uint8).reshape(720,1280,3)
    Image.fromarray(bgr[:,:,::-1]).save(folder/'centers_overlay.png')
    rows=[asdict(r) for r in result.diagnostics.rows]
    with (folder/'row_diagnostics.csv').open('x',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    record={**capture_metrics(result),'image_statistics':stats,'input_sha256':result.input_sha256}
    save_json(folder/'capture_metrics.json',record)
    return record

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=('smoke','first','laser','hdmi','hdmi_existing_pattern'),required=True)
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--receipt',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--stripe-roi',nargs=4,type=int,default=(0,0,752,480))
    p.add_argument('--settle-roi',nargs=4,type=int,default=(0,0,752,480))
    p.add_argument('--threshold',type=int,default=30)
    p.add_argument('--settle-protocol',default='legacy_3s_v1')
    p.add_argument('--pattern-proof',type=Path)
    return p

def main(argv=None):
    args=parser().parse_args(argv)
    from structured_light.motion.pynq_runtime import create_stage_b_runtime
    from structured_light.p2.hdmi_output import colorbar_bgr
    out=args.output;r=None
    result={'mode':args.mode,'motion_started':False,'overlay_loaded':False,'passed':False}
    hdmi=args.mode in ('hdmi','hdmi_existing_pattern')
    try:
        r=create_stage_b_runtime(args.manifest,stage='static',attach=True,
            loaded_overlay_receipt=args.receipt,output=out,
            stripe_roi=tuple(args.stripe_roi),roi=tuple(args.settle_roi),
            threshold=args.threshold,no_display=not hdmi,
            settle_protocol=args.settle_protocol)
        result['settle_protocol_requested']=args.settle_protocol
        result['settle_protocol_actual']=r.session.settle_config.protocol_id
        if result['settle_protocol_actual']!=args.settle_protocol:
            raise RuntimeError('runtime settling protocol differs from the requested protocol')
        before=r.guard.status();result['guard_before']=before
        if before['active'] or before['step_pin'] or not before['enable_pin']:
            raise RuntimeError('motor/guard is not in direct-wire disabled idle')
        if args.mode=='smoke':
            result['smoke']=r.static(count=20,mode='profile');result['passed']=True
        else:
            if hdmi:
                display=r.display.display
                display.configure();r.display.configured=True
                if args.mode=='hdmi_existing_pattern':
                    if args.pattern_proof is None:raise ValueError('historical pattern proof required')
                    proof=json.loads(args.pattern_proof.read_text())
                    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                    validate_pattern_evidence(proof,artifacts=r.artifacts['files'],boot_id=boot)
                    result['pattern_evidence_inherited']=proof
                    with (out/'historical_pattern_source.json').open('xb') as f:
                        f.write(Path(proof['source_file']).read_bytes())
                else:display.show(colorbar_bgr());wait_reply(out,'pattern')
            records=[]
            count=1 if args.mode=='first' else 10
            for i in range(count):
                pair=r.session.run_static(raw=False)
                record=export_result(pair,out,i);records.append(record)
                print('CAPTURE '+json.dumps(record),flush=True)
                if hdmi and i==4:wait_reply(out,'target_change')
            result['captures']=records
            if args.mode=='laser':result['laser_gate']=laser_gate(records);result['passed']=result['laser_gate']['passed']
            else:result['passed']=all(x['comparison_passed'] for x in records)
            if hdmi:
                result['laser_gate']=laser_gate(records)
                result['visual_confirmation']=wait_reply(out,'real_frame')
                result['hdmi_static_verified']=True
        after=r.guard.status();result['guard_after']=after
        if after['active'] or after['step_pin'] or not after['enable_pin']:raise RuntimeError('guard did not remain idle')
    except BaseException as exc:
        import traceback
        result.update(passed=False,error=type(exc).__name__+': '+str(exc),traceback=traceback.format_exc())
        print(result['traceback'],flush=True)
    finally:
        if r is not None:
            try:
                result['cleanup']=r.close()
                validate_cleanup(result['cleanup'])
            except BaseException as exc:result.update(passed=False,cleanup_error=str(exc))
        out.mkdir(parents=True,exist_ok=True)
        save_json(out/'finish_static_result.json',result)
        print('RESULT '+json.dumps(result,default=str),flush=True)
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
