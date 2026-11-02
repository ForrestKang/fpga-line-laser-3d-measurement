"""Hash-bound deployment and evidence retrieval for P2 closeout on pinned Z2.

Credentials are passed in memory or via PYNQ_PASSWORD, never stored in receipts.
start loads hardware only in the explicitly selected board runner; status/pull
do not issue MMIO, reset, or camera commands.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import tarfile
import time
import uuid

from live_preview_host import connect
from deploy_gray_candidate import execute

ROOT = Path(__file__).resolve().parents[2]
BOARD_PYTHON = '/usr/local/share/pynq-venv/bin/python3'
SOURCE_CONFIG = ROOT / 'board/configs/p2_closeout_reference.yaml'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, default=str)+'\n', encoding='utf-8')


def deploy(stage, *, count, password, threshold=200, roi=None):
    import yaml
    if stage not in {'raw', 'profile', 'recovery'}:
        raise ValueError('unknown closeout stage')
    if not 1 <= count <= 10000:
        raise ValueError('count outside bounded acceptance range')
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not 0 <= threshold <= 255:
        raise ValueError('threshold must be an 8-bit DN integer')
    selected_roi = [0,0,752,480] if stage == 'raw' else [0,120,752,300]
    if roi is not None:
        if stage == 'raw':
            raise ValueError('raw closeout always receives the full frame')
        if len(roi) != 4 or any(not isinstance(v, int) or isinstance(v, bool) for v in roi):
            raise ValueError('ROI requires four integer coordinates')
        x0, y0, x1, y1 = roi
        if not (0 <= x0 < x1 <= 752 and 0 <= y0 < y1 <= 480):
            raise ValueError('ROI must fit the confirmed 752x480 frame')
        selected_roi = list(roi)
    mode = 'camera_raw_monitor' if stage == 'raw' else 'structured_light_profile_hdmi'
    release = ROOT/'board/overlays'/mode
    manifest = json.loads((release/'manifest.json').read_text())
    if manifest.get('preboard_passed') is not True:
        raise ValueError('actual preboard audit must pass before deployment')
    for kind in ('bit', 'hwh'):
        if sha(release/f'{mode}.{kind}') != manifest['files'][kind]['sha256']:
            raise ValueError('artifact bytes disagree with manifest')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
    local = ROOT/'board/results/closeout'/stage/run_id
    local.mkdir(parents=True, exist_ok=False)
    remote = '/home/xilinx/sl_p2_closeout_'+run_id
    client = connect(password)
    ftp = client.open_sftp(); ftp.get_channel().settimeout(10)
    try:
        config = yaml.safe_load(SOURCE_CONFIG.read_text(encoding='utf-8'))
        # Copy the exact prior geometry evidence rather than inventing a gate.
        ftp.get(config['gate_a_evidence'], str(local/'prior_signals.json'))
        if sha(local/'prior_signals.json') != config['gate_a_sha256']:
            raise ValueError('prior Gate A readback fingerprint changed')
        config['overlay_mode'] = mode
        for key, name in [('bitfile', mode+'.bit'), ('hwh', mode+'.hwh'), ('manifest', 'manifest.json')]:
            config[key] = remote+'/board/overlays/'+mode+'/'+name
        config['identity_contract'] = remote+'/board/configs/p2_target_identity.json'
        config['gate_a_evidence'] = remote+'/prior_signals.json'
        config['threshold'] = threshold
        config['roi'] = selected_roi
        # The closeout runner deliberately has its own acceptance defaults.
        # Publish the selected ROI through that explicit override as well.
        config['closeout_roi'] = selected_roi
        (local/'run_config.yaml').write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
        paths = list((ROOT/'src/python').rglob('*.py')) + list((ROOT/'src/contracts').glob('*.json'))
        paths += [ROOT/'board/configs/p2_target_identity.json']
        paths += [ROOT/'board/scripts'/name for name in ('verify_p2_board.py','gray_capture_board.py','p2_closeout_board.py')]
        paths += [release/name for name in ('manifest.json', mode+'.bit', mode+'.hwh')]
        expected = {}
        archive = local/'deployment.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            for path in paths:
                name = path.relative_to(ROOT).as_posix()
                bundle.add(path, arcname=name); expected[name] = sha(path)
            for name in ('run_config.yaml','prior_signals.json'):
                bundle.add(local/name, arcname=name); expected[name] = sha(local/name)
        archive_hash = sha(archive)
        ftp.mkdir(remote); ftp.put(str(archive), remote+'/deployment.tar.gz')
        unpack = "import pathlib,tarfile,json,hashlib;r=pathlib.Path("+repr(remote)+");a=r/'deployment.tar.gz';assert hashlib.sha256(a.read_bytes()).hexdigest()=="+repr(archive_hash)+";t=tarfile.open(a);assert all(not m.issym() and not m.islnk() and (r/m.name).resolve().is_relative_to(r) for m in t.getmembers());t.extractall(r);e=json.loads("+repr(json.dumps(expected))+");assert all(hashlib.sha256((r/k).read_bytes()).hexdigest()==v for k,v in e.items());print('UPLOAD_HASH_VERIFIED')"
        rc, out, err = execute(client, BOARD_PYTHON+' -c '+shlex.quote(unpack), password=password, timeout_s=45)
        if rc or 'UPLOAD_HASH_VERIFIED' not in out:
            raise RuntimeError('uploaded bytes not verified: '+err)
        command = [BOARD_PYTHON, remote+'/board/scripts/p2_closeout_board.py', '--stage', stage, '--config', remote+'/run_config.yaml', '--output', remote+'/results', '--count', str(count)]
        if stage == 'recovery' and count == 32:
            command.append('--include-host-timeout')
        launch = "import os,subprocess;e=dict(os.environ);e['XILINX_XRT']='/usr';f=open("+repr(remote+'/run.log')+",'ab');p=subprocess.Popen("+repr(command)+",stdin=subprocess.DEVNULL,stdout=f,stderr=f,env=e,start_new_session=True);print(p.pid)"
        rc, out, err = execute(client, "sudo -S -p '' "+BOARD_PYTHON+' -c '+shlex.quote(launch), password=password, timeout_s=10)
        if rc:
            raise RuntimeError('runner launch failed: '+err)
        record = {'remote_root':remote, 'stage':stage, 'requested_count':count,
                  'requested_parameters':{'threshold':threshold,'roi':selected_roi},
                  'pid':int(out.strip()), 'host_started_utc':datetime.now(timezone.utc).isoformat(), 'deployment_sha256':archive_hash, 'files':expected, 'overlay_mode':mode, 'artifact_sha256':{kind:sha(release/f'{mode}.{kind}') for kind in ('bit','hwh')}}
        save(local/'host_run.json', record)
        return local
    finally:
        ftp.close(); client.close()


def _record(local):
    local = Path(local).resolve(strict=True)
    record = json.loads((local/'host_run.json').read_text())
    remote = record['remote_root']
    if not remote.startswith('/home/xilinx/sl_p2_closeout_') or '/' in remote[len('/home/xilinx/'):]:
        raise ValueError('unexpected remote receipt path')
    return local, record, remote


def status(local, *, password):
    local, record, remote = _record(local)
    client = connect(password); ftp = client.open_sftp(); ftp.get_channel().settimeout(5)
    try:
        result = {}
        for name in ('status.json','summary.json','session_initialization.json'):
            try:
                with ftp.open(remote+'/results/'+name, 'rb') as stream:
                    raw = stream.read(2*1024*1024)
                (local/name).write_bytes(raw); result[name] = json.loads(raw)
            except FileNotFoundError:
                pass
        with ftp.open(remote+'/run.log', 'rb') as stream:
            size = stream.stat().st_size; stream.seek(max(0,size-12000)); raw = stream.read(12000)
        (local/'run_log_tail.txt').write_bytes(raw)
        result['log_tail'] = raw.decode(errors='replace')
        return result
    finally:
        ftp.close(); client.close()


def pull(local, *, password):
    """Retrieve only a completed runner's evidence; preserve per-file hashes."""
    local, record, remote = _record(local)
    state = status(local, password=password)
    if 'summary.json' not in state:
        raise RuntimeError('runner has not published a final summary')
    if not state['summary.json'].get('completed_utc'):
        raise RuntimeError('runner cleanup has not completed; retain DMA owner')
    client = connect(password); ftp = client.open_sftp(); ftp.get_channel().settimeout(15)
    try:
        make = "import pathlib,tarfile,hashlib,json; r=pathlib.Path("+repr(remote)+");fs=sorted(p for p in (r/'results').rglob('*') if p.is_file());assert len(fs)<20000; assert sum(p.stat().st_size for p in fs)<8*1024**3; e={p.relative_to(r).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in fs};(r/'evidence_hashes.json').write_text(json.dumps(e,sort_keys=True));t=tarfile.open(r/'evidence.tar.gz','w:gz',compresslevel=1);t.add(r/'results',arcname='results');t.add(r/'evidence_hashes.json',arcname='evidence_hashes.json');t.close();(r/'archive_sha256.txt').write_text(hashlib.sha256((r/'evidence.tar.gz').read_bytes()).hexdigest());print('EVIDENCE_ARCHIVE_READY')"
        # File compression has a separate asynchronous process and no MMIO.
        launch = "import subprocess;p=subprocess.Popen(["+repr(BOARD_PYTHON)+",'-c',"+repr(make)+"],stdout=open("+repr(remote+'/archive.log')+",'wb'),stderr=subprocess.STDOUT,start_new_session=True);print(p.pid)"
        rc, out, err = execute(client, BOARD_PYTHON+' -c '+shlex.quote(launch), password=password, timeout_s=10)
        if rc: raise RuntimeError(err)
        # A full batch contains hundreds of MB. Archive generation is a
        # bounded background task on the ARM CPU, separate from acquisition.
        deadline = time.monotonic()+600
        while time.monotonic()<deadline:
            try:
                with ftp.open(remote+'/archive_sha256.txt','rb') as stream: expected = stream.read().decode().strip()
                break
            except FileNotFoundError: time.sleep(1)
        else: raise TimeoutError('archive generation deadline exceeded')
        archive = local/'evidence.tar.gz'; ftp.get(remote+'/evidence.tar.gz', str(archive))
        if sha(archive) != expected: raise RuntimeError('archive readback hash mismatch')
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            if any(m.issym() or m.islnk() or not (local/m.name).resolve().is_relative_to(local) for m in members):
                raise ValueError('unsafe evidence archive member')
            if any((local/m.name).is_file() for m in members):
                raise FileExistsError('evidence already retrieved; do not overwrite it')
            bundle.extractall(local)
        hashes = json.loads((local/'evidence_hashes.json').read_text())
        for name, fingerprint in hashes.items():
            if sha(local/name) != fingerprint: raise RuntimeError('evidence file mismatch: '+name)
        receipt = {'retrieved_utc':datetime.now(timezone.utc).isoformat(), 'archive_sha256':expected, 'verified_files':len(hashes), 'verified_bytes':sum((local/name).stat().st_size for name in hashes), 'passed':True}
        save(local/'readback.json', receipt)
        return receipt
    finally:
        ftp.close(); client.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', choices=('start','status','pull'), default='status')
    parser.add_argument('--stage', choices=('raw','profile','recovery'))
    parser.add_argument('--count', type=int, default=1000)
    parser.add_argument('--threshold', type=int, default=200)
    parser.add_argument('--roi', type=int, nargs=4, metavar=('X0','Y0','X1','Y1'))
    parser.add_argument('--run', type=Path)
    args=parser.parse_args()
    password=os.environ.get('PYNQ_PASSWORD')
    if not password: raise RuntimeError('PYNQ_PASSWORD is required; credentials are not persisted')
    if args.action=='start':
        if args.stage is None: parser.error('--stage is required for start')
        result={'local_run':str(deploy(args.stage,count=args.count,password=password,
                                       threshold=args.threshold,roi=args.roi))}
    else:
        if args.run is None: parser.error('--run is required')
        result=status(args.run,password=password) if args.action=='status' else pull(args.run,password=password)
    print(json.dumps(result,indent=2,default=str))


if __name__=='__main__':
    main()
