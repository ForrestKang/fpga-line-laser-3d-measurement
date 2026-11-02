"""Pinned PYNQ-Z2 SCCB repair deployment/probe; no DMA or motion commands."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import time
import uuid

import paramiko

ROOT = Path(__file__).resolve().parents[2]
HOST_KEY = '4c1d057c68df33381952ffeb789813a5eabf00cebcb0c0a9182e719ba3e2b7ba'

REMOTE_PROGRAM = r'''
import json,time,hashlib,sys
from pathlib import Path
from pynq import MMIO,Overlay
action=sys.argv[1];root=Path(__file__).resolve().parent
record={'target':'PYNQ-Z2','board_time_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
 'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
 'dma_started':False,'motion_enabled':False,'camera_input_verified':False,
 'camera_control_verified':False,'attempts':[]}
try:
 from pynq import PL
 previous=PL.bitfile_name
 record['previous_bitfile']=previous
 if previous and Path(previous).is_file():record['previous_bitfile_sha256']=hashlib.sha256(Path(previous).read_bytes()).hexdigest()
except (ImportError,AttributeError):pass
def save():
 (root/'result.json').write_text(json.dumps(record,indent=2)+'\n')
def sample(m):
 return {hex(o):m.read(o) for o in (0,4,8,12,0x10,0x14,0x18,0x1c,0x20,0x24,0x28,0x30,0x38,0x3c,0x44,0x50)}
try:
 m=MMIO(0x43c10000,0x10000)
 record['before']=sample(m);save()
 if action=='deploy':
  manifest=json.loads((root/'manifest.json').read_text())
  assert manifest['preboard_passed'] and manifest['overlay']=='camera_raw'
  for kind in ('bit','hwh'):
   p=root/('camera_raw.'+kind)
   assert hashlib.sha256(p.read_bytes()).hexdigest()==manifest['files'][kind]['sha256']
  record['artifact_sha256']={k:manifest['files'][k]['sha256'] for k in ('bit','hwh')};save()
  overlay=Overlay(str(root/'camera_raw.bit'),download=True)
  record['overlay_loaded']=True;save()
  assert 'processing_pipeline' in overlay.ip_dict and 'raw_dma' in overlay.ip_dict
  assert overlay.ip_dict['raw_dma']['phys_addr']==0x40400000
  record['hwh_ip_dict']={name:{k:info.get(k) for k in ('phys_addr','addr_range','type')}
                        for name,info in overlay.ip_dict.items()}
  m=MMIO(0x43c10000,0x10000)
  assert MMIO(0x43c00000,0x10000).read(0x78)==0x10000, 'Control VERSION mismatch'
 assert m.read(0)==0x10000 and m.read(4)==0x19, 'Expected camera_raw diagnostic version/capabilities'
 record['after_load']=sample(m);save()
 assert not(m.read(0x38)&1), 'SCCB already busy'
 assert not(m.read(8)&4), 'Capture is active'
 if action in ('deploy','probe'):
  if action=='deploy':
   m.write(0x3c,1);time.sleep(.001);m.write(0x3c,0);time.sleep(.1)
   record['sensor_reset_ms']=1
  assert m.read(0x3c)==0, 'Reset must be released; standby must be low'
  old_addr=m.read(0x24);old_reg=m.read(0x28)
  def read(address,register):
   assert not(m.read(0x38)&1)
   m.write(0x24,address);m.write(0x28,register)
   item={'address_7bit':address,'register':register,'status_trace':[]}
   record['attempts'].append(item);save()
   m.write(0x34,1);deadline=time.monotonic()+.5
   while time.monotonic()<deadline:
    status=m.read(0x38)
    if not item['status_trace'] or item['status_trace'][-1]!=status:item['status_trace'].append(status)
    if not(status&1) and status&0xe:
     item.update(status=status,nack=bool(status&4),timeout=bool(status&8),done=bool(status&2),pclk_hz=m.read(12))
     if status==2:item['value']=m.read(0x30)
     save();return item
    time.sleep(.0001)
   item['host_timeout']=True;save();raise TimeoutError('SCCB completion deadline')
  try:
   found=None
   for address in (0x48,0x4c,0x58,0x5c):
    item=read(address,0)
    if item.get('value')==0x1324 and item.get('status')==2:found=address;break
   if found is not None:
    record['selected_address']=found
    stable=[]
    for index in range(20):
     item=read(found,0);item['stability_index']=index
     stable.append(item.get('value')==0x1324 and item.get('status')==2)
    record['twenty_id_reads_passed']=all(stable)
    if all(stable):
     record['window_mode_registers']={hex(reg):read(found,reg) for reg in (1,2,3,4,5,6,7,0x0c)}
   record['pclk_samples_hz']=[]
   for _ in range(3):
    time.sleep(.03);record['pclk_samples_hz'].append(m.read(12))
   record['camera_control_verified']=bool(record.get('twenty_id_reads_passed') and all(x>0 for x in record['pclk_samples_hz']))
  finally:
   if not(m.read(0x38)&1):m.write(0x24,old_addr);m.write(0x28,old_reg)
 elif action in ('reset_low','reset_high'):
  value=1 if action=='reset_low' else 0
  try:
   m.write(0x3c,value)
   record['measurement_hold']={'camera_control':value,'duration_s':20,'voltage_measured_by_host':False};save()
   print('MEASURE_NOW '+action+' 20 seconds',flush=True)
   deadline=time.monotonic()+20
   while time.monotonic()<deadline:time.sleep(.2)
  finally:m.write(0x3c,0)
 record['after']=sample(m)
except Exception as exc:
 record['error']=type(exc).__name__+': '+str(exc)
finally:
 save();print(json.dumps(record),flush=True)
if record.get('error'):raise SystemExit(2)
'''


def execute(client, command, deadline_s, password=None):
    stdin, stdout, stderr = client.exec_command(command, timeout=10)
    if password is not None:
        stdin.write(password+'\n'); stdin.flush()
    ch=stdout.channel; end=time.monotonic()+deadline_s
    data=bytearray(); errors=bytearray()
    while not ch.exit_status_ready():
        while ch.recv_ready(): data.extend(ch.recv(65536))
        while ch.recv_stderr_ready(): errors.extend(ch.recv_stderr(65536))
        if time.monotonic()>end:
            ch.close();raise TimeoutError('Host command deadline; retained evidence must be inspected')
        time.sleep(.05)
    while ch.recv_ready():data.extend(ch.recv(65536))
    while ch.recv_stderr_ready():errors.extend(ch.recv_stderr(65536))
    return ch.recv_exit_status(),data.decode(errors='replace'),errors.decode(errors='replace')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action',choices=['inspect','deploy','probe','reset_low','reset_high'],default='inspect')
    args=parser.parse_args()
    password=os.environ.get('PYNQ_PASSWORD') or getpass.getpass('PYNQ password: ')
    run_id=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
    out=ROOT/'board/results/p2/camera'/('sccb_fix_'+args.action+'_'+run_id);out.mkdir(parents=True,exist_ok=False)
    remote='/home/xilinx/sccb_fix_'+run_id
    record={'host_utc':datetime.now(timezone.utc).isoformat(),'target':'PYNQ-Z2','ip':'192.168.2.99',
            'action':args.action,'remote_root':remote,'hardware_result':'not_tested',
            'remote_program_sha256':hashlib.sha256(REMOTE_PROGRAM.encode()).hexdigest()}
    client=paramiko.SSHClient()
    class Pinned(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self,client,hostname,key):
            actual=hashlib.sha256(key.asbytes()).hexdigest()
            if actual!=HOST_KEY:raise RuntimeError('Target SSH identity changed; stop before hardware access')
            record['ssh_host_key_sha256']=actual
    client.set_missing_host_key_policy(Pinned())
    sock=socket.socket();sftp=None
    try:
        sock.settimeout(5);sock.bind(('192.168.2.100',0));sock.connect(('192.168.2.99',22))
        client.connect('192.168.2.99',username='xilinx',password=password,sock=sock,
                       look_for_keys=False,allow_agent=False,timeout=5,auth_timeout=5,banner_timeout=5)
        if args.action=='deploy':
            folder=ROOT/'board/overlays/camera_raw';manifest=json.loads((folder/'manifest.json').read_text())
            assert manifest['preboard_passed'] and manifest['overlay']=='camera_raw'
            for kind in ('bit','hwh'):
                assert hashlib.sha256((folder/('camera_raw.'+kind)).read_bytes()).hexdigest()==manifest['files'][kind]['sha256']
        sftp=client.open_sftp();sftp.mkdir(remote)
        if args.action=='deploy':
            for name in ('camera_raw.bit','camera_raw.hwh','manifest.json'):sftp.put(str(folder/name),remote+'/'+name)
        with sftp.file(remote+'/board_probe.py','w') as f:f.write(REMOTE_PROGRAM)
        command="timeout --signal=TERM --kill-after=5s 55s sudo -S -p '' env XILINX_XRT=/usr /usr/local/share/pynq-venv/bin/python3 "+shlex.quote(remote+'/board_probe.py')+' '+shlex.quote(args.action)
        code,data,errors=execute(client,command,65,password)
        record.update(exit_code=code)
        (out/'stdout.txt').write_text(data,encoding='utf-8');(out/'stderr.txt').write_text(errors,encoding='utf-8')
        sftp.get(remote+'/result.json',str(out/'result.json'))
        result=json.loads((out/'result.json').read_text())
        record['observation']=result;record['hardware_result']='pass' if result.get('camera_control_verified') else 'pending_or_failed'
    except Exception as exc:record['error']=type(exc).__name__+': '+str(exc)
    finally:
        if sftp:sftp.close()
        client.close();sock.close()
        (out/'host_summary.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'evidence':str(out),'exit_code':record.get('exit_code'),'error':record.get('error'),'observation':record.get('observation')},indent=2))
    return 2 if record.get('error') or record.get('exit_code',0) else 0


if __name__=='__main__':raise SystemExit(main())
