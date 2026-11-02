"""Pinned SSH deployment and explicit phase replies for laser evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time
import uuid

from live_preview_host import ROOT, connect, execute
from deploy_gray_candidate import bounded_download_tree


def main():
    import yaml
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--action', choices=['start','status','confirm','download','stop'], required=True)
    p.add_argument('--mode', choices=['on_off_on','handheld','first_frame'], default='on_off_on')
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--phase', choices=['laser_off','laser_on','final_visual'])
    p.add_argument('--statement')
    p.add_argument('--threshold', type=int, default=200)
    p.add_argument('--roi', type=int, nargs=4, default=[440,0,492,480])
    args = p.parse_args()
    x0,y0,x1,y1 = args.roi
    if not 0 <= args.threshold <= 255 or not (0 <= x0 < x1 <= 752 and 0 <= y0 < y1 <= 480):
        p.error('invalid threshold/half-open ROI')
    password = os.environ['PYNQ_PASSWORD']
    client = connect(password)
    ftp = client.open_sftp(); ftp.get_channel().settimeout(5)
    try:
        if args.action == 'start':
            previous = args.run.resolve(strict=True)
            old = json.loads((previous/'host_run.json').read_text(encoding='utf-8'))
            state = json.loads((previous/'status_readback.json').read_text(encoding='utf-8'))
            if state.get('phase') != 'stopped' or state.get('display_stop_confirmed') is not True:
                raise RuntimeError('preview must be confirmed safely closed first')
            source = old['remote_root']
            run_id = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
            local = ROOT/'board/results/laser_profile'/run_id
            local.mkdir(parents=True)
            remote = '/home/xilinx/sl_laser_'+run_id
            ftp.mkdir(remote); ftp.mkdir(remote+'/results')
            config = yaml.safe_load((previous/'run_config.yaml').read_text(encoding='utf-8'))
            config.update(threshold=args.threshold, roi=args.roi, min_width=1, max_width=32, min_sum=30, min_peak=15)
            (local/'run_config.yaml').write_text(yaml.safe_dump(config,sort_keys=False),encoding='utf-8')
            script = ROOT/'board/scripts/laser_pair_sequence.py'
            ftp.put(str(script),remote+'/laser_pair_sequence.py')
            ftp.put(str(local/'run_config.yaml'),remote+'/run_config.yaml')
            hashes = dict(old['files'])
            # All code, bit/HWH and contracts bound to the actually running
            # accepted preview deployment are verified again before loading.
            validate = 'import pathlib,json,hashlib;r=pathlib.Path('+repr(source)+');e=json.loads('+repr(json.dumps(hashes))+');assert all(hashlib.sha256((r/k).read_bytes()).hexdigest()==v for k,v in e.items());'
            for path,name in [(script,'laser_pair_sequence.py'),(local/'run_config.yaml','run_config.yaml')]:
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                validate += 'assert hashlib.sha256(pathlib.Path('+repr(remote+'/'+name)+').read_bytes()).hexdigest()=='+repr(sha)+';'
            validate += "print('SOURCE_AND_UPLOAD_HASHES_VERIFIED')"
            rc,out,err = execute(client,'/usr/local/share/pynq-venv/bin/python3 -c '+shlex.quote(validate),password=password,timeout_s=30)
            if rc: raise RuntimeError(err)
            command = ['/usr/local/share/pynq-venv/bin/python3',remote+'/laser_pair_sequence.py','--source-root',source,'--config',remote+'/run_config.yaml','--output',remote+'/results','--mode',args.mode]
            launch = 'import os,subprocess;e=dict(os.environ);e["XILINX_XRT"]="/usr";f=open('+repr(remote+'/laser.log')+',"ab");p=subprocess.Popen('+repr(command)+',stdin=subprocess.DEVNULL,stdout=f,stderr=f,env=e,start_new_session=True);print(p.pid)'
            rc,out,err = execute(client,"sudo -S -p '' /usr/local/share/pynq-venv/bin/python3 -c "+shlex.quote(launch),password=password,timeout_s=10)
            if rc:raise RuntimeError(err)
            record = {'remote_root':remote,'source_root':source,'pid':int(out.strip()),'host_started_utc':datetime.now(timezone.utc).isoformat(),'script_sha256':hashlib.sha256(script.read_bytes()).hexdigest(),'source_files':hashes,
                      'operator_initial_statement':'独立电源，已亮，未接 FPGA 控制引脚', 'mode':args.mode}
            (local/'host_run.json').write_text(json.dumps(record,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
            print(json.dumps({'local_run':str(local),'remote_root':remote,'pid':record['pid']},indent=2),flush=True)
            deadline=time.monotonic()+100
            while time.monotonic()<deadline:
                try:
                    with ftp.open(remote+'/results/live_status.json','rb') as f:state=json.loads(f.read(2*1024*1024))
                    if state.get('phase')!='initializing':break
                except FileNotFoundError:pass
                time.sleep(1)
            else:raise TimeoutError('initialization deadline; inspect preserved process')
        else:
            local=args.run.resolve(strict=True)
            remote=json.loads((local/'host_run.json').read_text(encoding='utf-8'))['remote_root']
            if not remote.startswith('/home/xilinx/sl_laser_') or '/' in remote[len('/home/xilinx/'):]:raise ValueError('unexpected laser run root')
            with ftp.open(remote+'/results/live_status.json','rb') as f:state=json.loads(f.read(2*1024*1024))
            if args.action=='stop':
                if not args.statement:raise ValueError('an explicit stop reason is required')
                value={'reason':args.statement,'host_utc':datetime.now(timezone.utc).isoformat()}
                with ftp.open(remote+'/results/stop.request','wx') as f:f.write(json.dumps(value,ensure_ascii=False).encode('utf-8'))
                (local/'host_stop_reason.json').write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    with ftp.open(remote+'/results/live_status.json','rb') as f:state=json.loads(f.read(2*1024*1024))
                    if 'display_stop_confirmed' in state:break
                    time.sleep(.2)
                else:raise TimeoutError('display shutdown unconfirmed; keep buffer owner intact')
                if state.get('display_stop_confirmed') is not True:raise RuntimeError('display did not halt safely')
            if args.action=='confirm':
                if not args.phase or not args.statement or state.get('phase')!=args.phase:raise ValueError('explicit statement for the current phase is required')
                value={'nonce':state['nonce'],'phase':args.phase,'approved':True,'user_statement':args.statement,'recorded_host_utc':datetime.now(timezone.utc).isoformat()}
                target=remote+'/results/'+args.phase+'_confirmation.json'
                with ftp.open(target+'.tmp','wx') as f:f.write(json.dumps(value,ensure_ascii=False).encode('utf-8'))
                ftp.rename(target+'.tmp',target)
                (local/(args.phase+'_operator_confirmation.json')).write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
            if args.action=='download':
                transfer=bounded_download_tree(ftp,remote+'/results',local/'results',timeout_s=55,max_files=800,max_total_bytes=512*1024*1024)
                (local/'remote_results_download.json').write_text(json.dumps(transfer,indent=2)+'\n',encoding='utf-8')
                if not transfer['complete']:raise RuntimeError('evidence download incomplete')
        (local/'status_readback.json').write_text(json.dumps(state,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        print(json.dumps({'phase':state.get('phase'),'captures':len(state.get('captures',[])),'error':state.get('error'),'capture_metrics':[{k:x['metrics'].get(k) for k in ('request_id','comparison_passed','valid_single_candidate_rows','fractional_center_rows','max_xc_error_lsb')} for x in state.get('captures',[])]},indent=2))
        if state.get('phase')=='failed' and args.action!='stop':raise RuntimeError(state.get('error'))
    finally:
        ftp.close();client.close()


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
