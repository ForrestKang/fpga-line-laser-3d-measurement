"""Deploy/control preview on the pinned PYNQ-Z2; never falls back to Mock."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import sys
import tarfile
import time
import uuid

from deploy_gray_candidate import (HOST_KEY,execute,load_identity_contract,
    remote_identity_command,parse_remote_identity,validate_remote_identity)

ROOT=Path(__file__).resolve().parents[2]


def connect(password):
    import paramiko
    client=paramiko.SSHClient()
    class Pin(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self,_client,_hostname,key):
            if hashlib.sha256(key.asbytes()).hexdigest()!=HOST_KEY:
                raise RuntimeError('target SSH identity changed')
    client.set_missing_host_key_policy(Pin())
    sock=socket.socket();sock.settimeout(5);sock.bind(('192.168.2.100',0));sock.connect(('192.168.2.99',22))
    client.connect('192.168.2.99',username='xilinx',password=password,sock=sock,
                   look_for_keys=False,allow_agent=False,timeout=5,auth_timeout=5,banner_timeout=5)
    rc,out,err=execute(client,remote_identity_command(),password=password,timeout_s=10)
    if rc:client.close();raise RuntimeError(err)
    validate_remote_identity(parse_remote_identity(out),load_identity_contract())
    return client


def read_status(ftp,remote,local):
    with ftp.open(remote+'/results/status.json','rb') as f:
        raw=f.read(256*1024)
    state=json.loads(raw)
    (local/'status_readback.json').write_bytes(raw)
    for name in ('latest_gray.png','latest_gray.bin','summary.json'):
        try:ftp.get(remote+'/results/'+name,str(local/name))
        except FileNotFoundError:pass
    return state


def main():
    import yaml
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--action',choices=['start','status','stop'],default='status')
    p.add_argument('--run',type=Path)
    p.add_argument('--duration-s',type=int,default=1800)
    args=p.parse_args()
    if not 10<=args.duration_s<=1800:p.error('duration must be 10..1800 seconds')
    if args.action!='start' and args.run is None:p.error('--run is required')
    password=os.environ.get('PYNQ_PASSWORD')
    if not password:raise RuntimeError('PYNQ_PASSWORD is required')
    client=connect(password);ftp=client.open_sftp();ftp.get_channel().settimeout(5)
    try:
        if args.action=='start':
            accepted=ROOT/'board/results/profile_hdmi/profile-hdmi_20261011_085028_e2288f56'
            previous=json.loads((accepted/'results/summary.json').read_text(encoding='utf-8'))
            if previous.get('profile_hdmi_coexistence_verified') is not True or previous.get('display_stop_confirmed') is not True:
                raise RuntimeError('the prior paired/HDMI session must be accepted and safely closed')
            mode='structured_light_profile_hdmi';release=ROOT/'board/overlays'/mode
            manifest=json.loads((release/'manifest.json').read_text())
            if not manifest.get('preboard_passed'):raise RuntimeError('preboard is not accepted')
            for kind in ('bit','hwh'):
                digest=hashlib.sha256((release/(mode+'.'+kind)).read_bytes()).hexdigest()
                if digest!=manifest['files'][kind]['sha256'] or digest!=previous['session']['artifacts'][kind+'_sha256']:
                    raise RuntimeError('preview must use the actual accepted artifact')
            run_id=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
            local=ROOT/'board/results/live_preview'/run_id;local.mkdir(parents=True)
            remote='/home/xilinx/sl_live_'+run_id
            config=yaml.safe_load((accepted/'run_config.yaml').read_text())
            for key in ('bitfile','hwh','manifest'):
                config[key]=remote+'/board/overlays/'+mode+'/'+('manifest.json' if key=='manifest' else mode+'.'+('bit' if key=='bitfile' else 'hwh'))
            config['identity_contract']=remote+'/board/configs/p2_target_identity.json'
            (local/'run_config.yaml').write_text(yaml.safe_dump(config,sort_keys=False),encoding='utf-8')
            paths=list((ROOT/'src/python').rglob('*.py'))+list((ROOT/'src/contracts').glob('*.json'))
            paths += [ROOT/'board/configs/p2_target_identity.json']
            paths += [ROOT/'board/scripts'/name for name in ('verify_p2_board.py','gray_capture_board.py','profile_dashboard_board.py','live_preview_board.py')]
            paths += [release/name for name in ('manifest.json',mode+'.bit',mode+'.hwh')]
            expected={}
            archive=local/'deployment.tar.gz'
            with tarfile.open(archive,'w:gz') as t:
                for path in paths:
                    name=path.relative_to(ROOT).as_posix();t.add(path,arcname=name)
                    expected[name]=hashlib.sha256(path.read_bytes()).hexdigest()
                t.add(local/'run_config.yaml',arcname='run_config.yaml')
                expected['run_config.yaml']=hashlib.sha256((local/'run_config.yaml').read_bytes()).hexdigest()
            archive_hash=hashlib.sha256(archive.read_bytes()).hexdigest()
            ftp.mkdir(remote);ftp.mkdir(remote+'/results');ftp.put(str(archive),remote+'/deployment.tar.gz')
            unpack="import pathlib,tarfile,json,hashlib;r=pathlib.Path("+repr(remote)+");a=r/'deployment.tar.gz';assert hashlib.sha256(a.read_bytes()).hexdigest()=="+repr(archive_hash)+";t=tarfile.open(a);assert all(not m.issym() and not m.islnk() and (r/m.name).resolve().is_relative_to(r) for m in t.getmembers());t.extractall(r);e=json.loads("+repr(json.dumps(expected))+");assert all(hashlib.sha256((r/k).read_bytes()).hexdigest()==v for k,v in e.items());print('UPLOAD_HASH_VERIFIED')"
            rc,out,err=execute(client,'/usr/local/share/pynq-venv/bin/python3 -c '+shlex.quote(unpack),password=password,timeout_s=30)
            if rc:raise RuntimeError('upload readback failed: '+err)
            command=['/usr/local/share/pynq-venv/bin/python3',remote+'/board/scripts/live_preview_board.py','--config',remote+'/run_config.yaml','--output',remote+'/results','--duration-s',str(args.duration_s)]
            launch="import os,subprocess;e=dict(os.environ);e['XILINX_XRT']='/usr';f=open("+repr(remote+'/live.log')+",'ab');p=subprocess.Popen("+repr(command)+",stdin=subprocess.DEVNULL,stdout=f,stderr=f,env=e,start_new_session=True);print(p.pid)"
            rc,out,err=execute(client,"sudo -S -p '' /usr/local/share/pynq-venv/bin/python3 -c "+shlex.quote(launch),password=password,timeout_s=10)
            if rc:raise RuntimeError('launch failed: '+err)
            record={'remote_root':remote,'pid':int(out.strip()),'duration_s':args.duration_s,'host_started_utc':datetime.now(timezone.utc).isoformat(),'deployment_sha256':archive_hash,'files':expected,'accepted_bit_sha256':manifest['files']['bit']['sha256']}
            (local/'host_run.json').write_text(json.dumps(record,indent=2)+'\n')
            print(json.dumps({'local_run':str(local),'remote_root':remote,'pid':record['pid']},indent=2),flush=True)
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                try:
                    state=read_status(ftp,remote,local)
                    if state.get('phase')!='initializing':break
                except FileNotFoundError:pass
                time.sleep(1)
            else:raise TimeoutError('preview startup exceeded 120 seconds; inspect the preserved running process')
        else:
            local=args.run.resolve(strict=True)
            record=json.loads((local/'host_run.json').read_text());remote=record['remote_root']
            if not remote.startswith('/home/xilinx/sl_live_') or '/' in remote[len('/home/xilinx/'):]:raise ValueError('unexpected preview root')
            if args.action=='stop':
                with ftp.open(remote+'/results/stop.request','wx') as f:f.write(b'explicit host stop\n')
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    state=read_status(ftp,remote,local)
                    if state.get('phase')!='running':break
                    time.sleep(.5)
                else:raise TimeoutError('preview stop is not yet confirmed; do not kill buffer owner')
            else:state=read_status(ftp,remote,local)
        print(json.dumps({key:state.get(key) for key in ('phase','frames','fps','request_id','capture_s','update_s','error','display_stop_confirmed')},indent=2))
        if state.get('phase')=='failed':raise RuntimeError(state.get('error'))
    finally:ftp.close();client.close()


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
