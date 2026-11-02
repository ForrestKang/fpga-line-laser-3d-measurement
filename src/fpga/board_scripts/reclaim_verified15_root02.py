"""Reclaim only the exact fifteen successful, fully archived board copies."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT/'archive/board_space_20261030_01/ARCHIVE_MANIFEST_FINAL.json'
EXPECTED = 'dd7bbe973186b69515a20a114f5462d541cb4a4009a933b32f3308c2255bec47'
sys.path.insert(0, str(ROOT/'board'))
from archive_space_readonly import connect, identity, sha  # noqa: E402

REMOTE_CODE = r'''
import hashlib,json,pathlib,shutil,stat,sys
d=json.load(sys.stdin)
machine=pathlib.Path('/etc/machine-id').read_text().strip()
boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
assert machine==d['machine_id'] and boot==d['boot_id'], 'identity changed'
allowed={
 '/home/xilinx/scan3d_r43_timing_board_20261029_02/data',
 '/home/xilinx/scan3d_r35_implementation_20261025_01/data',
 '/home/xilinx/scan3d_r33_full_pixel_20261025_01/data'}
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
for r in d['runs']:
 p=pathlib.Path(r['remote'])
 assert str(p.parent) in allowed and p.resolve()==p and not p.is_symlink(), 'unsafe target'
 actual={str(q.relative_to(p)):q for q in [p]+sorted(p.rglob('*'))}
 expected={e['relative']:e for e in r['entries']}
 partial = r['remote']==d['prior_partial_target']
 assert set(actual)<=set(expected) if partial else set(actual)==set(expected), 'tree changed'
 print(json.dumps(dict(phase='pre_delete_tree',remote=str(p),previously_removed=sorted(set(expected)-set(actual)))),flush=True)
 for rel,q in actual.items():
  e=expected[rel];s=q.lstat()
  assert not stat.S_ISLNK(s.st_mode), 'symlink'
  if not (partial and q.is_dir()):
   assert (s.st_mode,s.st_size,s.st_dev,s.st_ino,s.st_nlink,s.st_blocks,s.st_mtime_ns)==(e['mode'],e['size'],e['device'],e['inode'],e['nlink'],e['blocks'],e['mtime_ns']), 'stat changed'
  if q.is_file():
   assert s.st_nlink==1 and digest(q)==e['sha256'], 'hardlink or hash changed'
   assert q.name not in ('ERROR.json','OWNER_UNCONFIRMED.json','DISPLAY_CLEANUP_FAILED.json'), 'failure evidence'
 if (p/'RESULT.json').exists():
  result=json.loads((p/'RESULT.json').read_text())
  assert result.get('data_correct') is True, 'not a successful run'
 else:
  assert partial, 'missing success RESULT'
print(json.dumps(dict(phase='all_targets_reverified',count=len(d['runs']))),flush=True)
free_before=shutil.disk_usage('/home/xilinx').free
for r in d['runs']:
 p=pathlib.Path(r['remote'])
 assert p.resolve()==p and str(p.parent) in allowed
 shutil.rmtree(p)
 print(json.dumps(dict(phase='deleted_verified_copy',remote=str(p))),flush=True)
print(json.dumps(dict(phase='complete',free_before=free_before,free_after=shutil.disk_usage('/home/xilinx').free)),flush=True)
'''


def main():
    receipt = ROOT/'evidence/RECLAIM_VERIFIED15_EXECUTED02.json'
    if receipt.exists():
        raise FileExistsError('reclaim receipt already exists')
    if sha(MANIFEST) != EXPECTED:
        raise ValueError('reviewed manifest changed')
    report = json.loads(MANIFEST.read_text())
    if report.get('passed') is not True:
        raise ValueError('archive not passed')
    runs = [r for r in report['runs'] if r.get('eligible')]
    if len(runs) != 15 or sorted(r['remote'] for r in runs) != sorted(report['reclaim_candidates']):
        raise ValueError('exact reviewed fifteen targets required')
    for r in runs:
        if not all(r.get(k) is True for k in ('remote_reverified_unchanged',
                   'all_files_size_sha_format_passed', 'complete_payload_readback')) or r['hardlinked_files']:
            raise ValueError('archive gate missing')
        archive = Path(r['local_archive']).resolve()
        archive.relative_to(ROOT/'archive/board_space_20261030_01')
        for entry in r['files']:
            p = archive/entry['relative']
            if p.stat().st_size != entry['bytes'] or sha(p) != entry['sha256']:
                raise ValueError('local archive differs: '+str(p))
    result = dict(schema='FINAL_VERIFIED_SUCCESSFUL_COPY_RECLAIM_V1',
                  authority='user authorized noncore archive-then-clean; no further approval required',
                  archive_manifest_sha256=EXPECTED, started_unix=time.time(),
                  fpga_loads=0, camera_commands=0, motor_commands=0,
                  failed_runs_deleted=False, core_code_packages_deleted=False, passed=False)
    client = None
    try:
        client, key = connect('192.168.2.99', 'xilinx', os.environ['PYNQ_PASSWORD'], '192.168.2.100')
        client.get_transport().set_keepalive(10)
        before = identity(client)
        result.update(identity_before=before, hostkey_sha256=key)
        payload = dict(machine_id=before['machine_id'], boot_id=before['boot_id'],
                       runs=[dict(remote=r['remote'], entries=r['inventory']['entries']) for r in runs],
                       prior_partial_target=runs[0]['remote'])
        inp, out, err = client.exec_command('sudo -S -p '+shlex.quote('')+' -- python3 -B -c '+shlex.quote(REMOTE_CODE), timeout=240)
        inp.write(os.environ['PYNQ_PASSWORD']+'\n')
        inp.flush()
        inp.write(json.dumps(payload)+'\n')
        inp.flush()
        inp.channel.shutdown_write()
        stdout, stderr = out.read().decode(), err.read().decode()
        result.update(remote_stdout=stdout, remote_stderr=stderr,
                      returncode=out.channel.recv_exit_status())
        if result['returncode']:
            raise RuntimeError('remote reverify/reclaim failed; inspect partial receipt')
        after = identity(client)
        result.update(identity_after=after, passed=True, deleted=[r['remote'] for r in runs],
                      actual_free_increase=after['disk_free']-before['disk_free'],
                      space_admitted=after['disk_free'] >= 6459228160)
    except BaseException as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if client:
            client.close()
        result['finished_unix'] = time.time()
        with receipt.open('x', encoding='utf8') as f:
            json.dump(result, f, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != 'remote_stdout'}))


if __name__ == '__main__':
    main()
