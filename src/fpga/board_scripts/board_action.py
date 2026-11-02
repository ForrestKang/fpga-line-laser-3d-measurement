"""Bounded, identity checked deployment/replay transport; no camera/motor API.

Invoke from a process with safely loaded PYNQ_PASSWORD. Parent exclusively schedules mutations. Never emits authentication values. Each action
creates a new local receipt. Artifact upload is exclusive and SHA verified.
"""
import argparse, hashlib, json, os, pathlib, shlex, sys, time
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(pathlib.Path('D:/Workspace/CodexWork/scan3d_r35_implementation_20261025_01/staging/runtime_r35')))
from tools.board_readonly_r26 import connect
REMOTE='/home/xilinx/scan3d_final_timingfix_20261030_01'
MACHINE='c82cf5bcd3a14b95935d65bfb529d72d'

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['upload','exec','download'])
    p.add_argument('--receipt',required=True,type=pathlib.Path)
    p.add_argument('--local',type=pathlib.Path);p.add_argument('--remote')
    p.add_argument('--expected-boot',required=True);p.add_argument('--command');p.add_argument('--timeout',type=int,default=300)
    p.add_argument('--sudo',action='store_true',help='Supply SSH credential over stdin, never in shell text')
    a=p.parse_args()
    if a.receipt.exists():raise FileExistsError(a.receipt)
    c,key=connect('192.168.2.99','xilinx',os.environ['PYNQ_PASSWORD'],'192.168.2.100')
    c.get_transport().set_keepalive(10)
    r={'action':a.action,'hostkey':key,'started_ns':time.time_ns(),'automatic_retry':False}
    def execute(cmd,timeout=30,sudo=False):
        if sudo:cmd='sudo -S -p '+shlex.quote('')+' -- sh -c '+shlex.quote(cmd)
        inp,out,err=c.exec_command(cmd,timeout=timeout)
        if sudo:
            inp.write(os.environ['PYNQ_PASSWORD']+'\n');inp.flush();inp.channel.shutdown_write()
        channel=out.channel;data=bytearray();error=bytearray();deadline=time.monotonic()+timeout
        while True:
            while channel.recv_ready():data.extend(channel.recv(65536))
            while channel.recv_stderr_ready():error.extend(channel.recv_stderr(65536))
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():break
            if time.monotonic()>deadline:
                r['partial_command_stdout']=bytes(data).decode('utf8','replace')[-16000:]
                r['partial_command_stderr']=bytes(error).decode('utf8','replace')[-16000:]
                raise TimeoutError('remote command deadline; completion unconfirmed')
            time.sleep(.02)
        data=bytes(data);error=bytes(error);code=channel.recv_exit_status()
        if code:
            r['failed_command_stdout']=data.decode('utf8','replace')[-16000:]
            r['failed_command_stderr']=error.decode('utf8','replace')[-16000:]
            raise RuntimeError('remote exit '+str(code)+': '+error.decode('utf8','replace')[-5000:])
        return data
    try:
        identity=execute('cat /etc/machine-id /proc/sys/kernel/random/boot_id').decode().split()
        if identity[0]!=MACHINE:raise RuntimeError('machine differs')
        r['machine_id'],r['boot_id']=identity
        if r['boot_id']!=a.expected_boot:raise RuntimeError('boot differs from current admitted boot')
        if a.action in ('upload','download'):
            target=pathlib.PurePosixPath(a.remote)
            if not str(target).startswith(REMOTE+'/') or '..' in target.parts:
                raise ValueError('path outside candidate')
            s=c.open_sftp()
            try:
                if a.action=='upload':
                    if not a.local.is_dir():raise ValueError('exclusive directory upload required')
                    entries={str(x.relative_to(a.local)).replace('\\','/'):hashlib.sha256(x.read_bytes()).hexdigest()
                             for x in a.local.rglob('*') if x.is_file()}
                    code="import pathlib; p=pathlib.Path(%r); p.mkdir(parents=True,exist_ok=False)"%str(target)
                    execute('python3 -B -c '+shlex.quote(code))
                    for name,sha in entries.items():
                        src=a.local/name;dest=str(target/name)
                        execute('mkdir -p -- '+shlex.quote(str(pathlib.PurePosixPath(dest).parent)))
                        with src.open('rb') as inp,s.open(dest,'wx') as out:
                            while True:
                                block=inp.read(1024**2)
                                if not block:break
                                out.write(block)
                        code="import pathlib,hashlib,os; p=pathlib.Path(%r); f=p.open('rb'); digest=hashlib.sha256(f.read()).hexdigest(); os.fsync(f.fileno()); print(digest)"%dest
                        observed=execute('python3 -B -c '+shlex.quote(code)).decode().strip()
                        if observed!=sha:raise RuntimeError('upload SHA differs '+name)
                    r['files']=entries
                else:
                    if a.local.exists():raise FileExistsError(a.local)
                    a.local.mkdir(parents=True)
                    # Recursive inventory and download, preserving legal empty files.
                    # Hash large frame journals in bounded chunks rather than
                    # allocating an entire 130 MiB journal on a 512 MiB board.
                    code="""import pathlib,hashlib,json
root=pathlib.Path(%r)
def describe(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return dict(bytes=p.stat().st_size,sha256=h.hexdigest())
print(json.dumps({p.relative_to(root).as_posix():describe(p) for p in root.rglob('*') if p.is_file()}))
"""%str(target)
                    entries=json.loads(execute('python3 -B -c '+shlex.quote(code)))
                    for name,info in entries.items():
                        rel=pathlib.PurePosixPath(name)
                        if rel.is_absolute() or '..' in rel.parts:raise ValueError('unsafe download')
                        dst=a.local/name;dst.parent.mkdir(parents=True,exist_ok=True)
                        with s.open(str(target/rel),'rb') as inp,dst.open('xb') as out:
                            while True:
                                block=inp.read(1024**2)
                                if not block:break
                                out.write(block)
                        if dst.stat().st_size!=info['bytes'] or hashlib.sha256(dst.read_bytes()).hexdigest()!=info['sha256']:
                            raise RuntimeError('archive mismatch '+name)
                    r['files']=entries;r['archive_verified_size_sha']=True
            finally:s.close()
        else:
            if not a.command:raise ValueError('command required')
            r['command']=a.command
            t=time.perf_counter();r['stdout']=execute(a.command,a.timeout,a.sudo).decode('utf8','replace')
            r['wall_s']=time.perf_counter()-t
        after=execute('cat /proc/sys/kernel/random/boot_id').decode().strip()
        if after!=r['boot_id']:raise RuntimeError('boot changed')
        r['passed']=True
    except BaseException as e:
        r.update(passed=False,error=(type(e).__name__+': '+str(e)).replace(os.environ['PYNQ_PASSWORD'],'<redacted>'))
        raise
    finally:
        c.close();a.receipt.parent.mkdir(parents=True,exist_ok=True)
        with a.receipt.open('x',encoding='utf8') as f:json.dump(r,f,indent=2)
        print(json.dumps({k:v for k,v in r.items() if k not in ('files','stdout')}))
if __name__=='__main__':main()
