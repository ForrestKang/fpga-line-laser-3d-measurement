"""Read-only pinned-board archive and complete local journal validation.

Never deletes a remote file, creates a remote lock, or touches PL/MMIO. A fresh
archive directory is required. Reclaim authorization remains the caller's job.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'archive/board_space_20261030_01'
BOOT = '3ecac088-69c8-4399-a110-9121631984b2'
MACHINE = 'c82cf5bcd3a14b95935d65bfb529d72d'
R35 = Path('D:/Workspace/CodexWork/scan3d_r35_implementation_20261025_01')
R43 = Path('D:/Workspace/CodexWork/scan3d_r43_timing_board_20261029_02')
R33 = Path('D:/Workspace/CodexWork/scan3d_r33_full_pixel_20261025_01')
sys.path[:0] = [str(ROOT/'runtime'), str(R35/'staging/runtime_r35'),
               str(ROOT/'runtime/structured_light_system_algorithm_v1/src/python'),
               str(ROOT/'runtime/structured_light_system_geometry_v1/src/python')]
if os.name == 'nt':
    _DLL = os.add_dll_directory('D:/Anaconda3/Library/bin')
os.environ['R33_NATIVE_ASSEMBLER'] = str(R43/'board/host_native/assemble_r33.dll')
from tools.board_readonly_r26 import connect  # noqa: E402
from r33.storage import readback  # noqa: E402
import numpy as np  # noqa: E402


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    with path.open('x', encoding='utf8') as out:
        json.dump(value, out, indent=2)


def exec_json(client, code, timeout=240):
    _, out, err = client.exec_command('python3 -B -c '+shlex.quote(code), timeout=timeout)
    raw = out.read()
    error = err.read().decode('utf8', 'replace')
    rc = out.channel.recv_exit_status()
    if rc:
        raise RuntimeError('read-only remote command failed: '+error[-2000:])
    return json.loads(raw)


def identity(client):
    result = exec_json(client, "import pathlib,json,shutil; print(json.dumps(dict(machine_id=pathlib.Path('/etc/machine-id').read_text().strip(),boot_id=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),disk_free=shutil.disk_usage('/home/xilinx').free)))")
    if result['machine_id'] != MACHINE or result['boot_id'] != BOOT:
        raise RuntimeError('board identity/boot changed; no archive admission')
    return result


def candidates():
    groups = [
        (R43, ['r43p02_formal01_'+target+'_rep'+str(rep)
               for target in ('T05', 'T06') for rep in (1, 2, 3)] +
              ['r43p02_short_T05_01', 'r43p02_short_T06_01']),
        (R35, ['formal35_T06_rep1', 'short35_T05_serial', 'short35_T05_prefetch2',
               'r36_short_T05', 'r36_short_T06']),
        (R33, ['r33_formal_v13_T05_rep1']),
    ]
    for source, names in groups:
        for name in names:
            yield source, name, '/home/xilinx/'+source.name+'/data/'+name


def inventory(client, root):
    code = '''import pathlib,os,stat,hashlib,json
root=pathlib.Path(%r)
if not root.is_dir(): print(json.dumps(dict(exists=False)));raise SystemExit
assert root.resolve()==root and not root.is_symlink()
entries=[]
for path in [root,*sorted(root.rglob('*'))]:
 s=path.lstat();row=dict(relative=path.relative_to(root).as_posix(),mode=s.st_mode,
 size=s.st_size,device=s.st_dev,inode=s.st_ino,nlink=s.st_nlink,blocks=s.st_blocks,
 allocated_bytes=s.st_blocks*512,mtime_ns=s.st_mtime_ns,symlink=stat.S_ISLNK(s.st_mode))
 if stat.S_ISLNK(s.st_mode):row['link_target']=os.readlink(path)
 elif stat.S_ISREG(s.st_mode):
  h=hashlib.sha256()
  with path.open('rb') as f:
   for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
  row['sha256']=h.hexdigest()
 entries.append(row)
result=json.loads((root/'RESULT.json').read_text()) if (root/'RESULT.json').is_file() else None
print(json.dumps(dict(exists=True,entries=entries,result=result)))
''' % root
    return exec_json(client, code)


def format_check(path):
    if path.stat().st_size == 0:
        if path.name not in {'HDMI_OWNER.log', 'events.jsonl'}:
            raise ValueError('unrecognized empty file: '+str(path))
        return 'legal_empty_event_log'
    if path.suffix == '.json':
        json.loads(path.read_text(encoding='utf8'))
        return 'json_parse'
    if path.suffix == '.jsonl':
        for line in path.read_text(encoding='utf8').splitlines():
            if line.strip():
                json.loads(line)
        return 'jsonl_parse'
    if path.suffix == '.npy':
        a = np.load(path, allow_pickle=False)
        if not np.isfinite(a).all():
            raise ValueError('nonfinite npy')
        if path.name in ('cloud.npy', 'preview.npy') and (a.ndim != 2 or a.shape[1] != 3):
            raise ValueError('XYZ shape invalid')
        return 'npy_loaded_finite_'+str(a.shape)
    if path.suffix in ('.md', '.txt', '.log'):
        path.read_text(encoding='utf8')
        return 'utf8_text'
    return 'bytes_sha_verified'


def archive_one(client, sftp, source, name, remote, *, reuse_download=False):
    inv = inventory(client, remote)
    row = dict(remote=remote, eligible=False, inventory=inv)
    if not inv['exists']:
        row['reason'] = 'remote_absent'
        return row
    entries = inv['entries']
    if any(e['symlink'] for e in entries):
        row['reason'] = 'symlink_requires_review'
        return row
    files = [e for e in entries if stat.S_ISREG(e['mode'])]
    if any(Path(e['relative']).name in {'ERROR.json', 'OWNER_UNCONFIRMED.json', 'DISPLAY_CLEANUP_FAILED.json'} for e in files):
        row['reason'] = 'failure_evidence_preserved'
        return row
    if not inv['result'] or inv['result'].get('data_correct') is not True:
        row['reason'] = 'successful_RESULT_required'
        return row
    target = OUT/source.name/name
    target.mkdir(parents=True, exist_ok=reuse_download)
    # Existing full archives are copied only after matching the fresh remote SHA.
    choices = [source/'archive'/name, source/'board'/name]
    if name == 'formal35_T06_rep1':
        choices += [source/'board/formal35_T06_rep1_recovered02']
    validated = []
    for e in files:
        local = target/e['relative']
        local.parent.mkdir(parents=True, exist_ok=True)
        if local.exists():
            if not reuse_download or local.stat().st_size != e['size'] or sha(local) != e['sha256']:
                raise ValueError('existing archive conflict: '+str(local))
            validated.append(dict(relative=e['relative'], bytes=e['size'], sha256=e['sha256'],
                                  format_check=format_check(local), method='existing_new_archive_reverified'))
            continue
        old = next((p/e['relative'] for p in choices
                    if (p/e['relative']).is_file()
                    and (p/e['relative']).stat().st_size == e['size']
                    and sha(p/e['relative']) == e['sha256']), None)
        if old:
            shutil.copyfile(old, local)
            method = 'copied_fresh_remote_sha_matched_existing_D_archive'
        else:
            with sftp.open(remote+'/'+e['relative'], 'rb') as src, local.open('xb') as dst:
                while True:
                    b = src.read(1024*1024)
                    if not b:
                        break
                    dst.write(b)
            method = 'downloaded_readonly_sftp'
        if local.stat().st_size != e['size'] or sha(local) != e['sha256']:
            raise ValueError('archive size/SHA mismatch: '+str(local))
        validated.append(dict(relative=e['relative'], bytes=e['size'], sha256=e['sha256'],
                              format_check=format_check(local), method=method))
    rb = readback(target/'storage', strict_protocol=True)
    run = inv['result']
    if not rb['passed'] or rb['frames'] != run['frames'] or rb['incomplete_tail']:
        raise ValueError('strict frame count/tail mismatch')
    for mode, filename in [('measurement', 'cloud.npy'), ('preview', 'preview.npy')]:
        a = np.load(target/filename, allow_pickle=False)
        if len(a) != run[mode+'_points'] or len(a) != rb[mode+'_points']:
            raise ValueError('cloud point count mismatch')
        if hashlib.sha256(a.tobytes()).hexdigest() != rb[mode+'_xyz_sha256']:
            raise ValueError('cloud exported content differs from full journal')
    save(target/'ARCHIVE_STRICT_READBACK.json', rb)
    # A second inventory protects against a concurrent file replacement during transfer.
    after = inventory(client, remote)
    if inv != after:
        raise ValueError('remote directory changed during archive')
    unique = {(e['device'], e['inode']): e['allocated_bytes'] for e in entries}
    # Hardlinked payload is archived, but reclaim estimate must be conservative.
    releasable = {(e['device'], e['inode']): e['allocated_bytes'] for e in entries
                  if stat.S_ISDIR(e['mode']) or e['nlink'] == 1}
    row.update(eligible=True, local_archive=str(target), files=validated,
               file_count=len(files), logical_bytes=sum(e['size'] for e in files),
               allocated_unique_bytes=sum(unique.values()),
               conservative_reclaim_bytes=sum(releasable.values()),
               hardlinked_files=[e for e in files if e['nlink'] > 1],
               strict_readback_frames=rb['frames'], remote_reverified_unchanged=True,
               all_files_size_sha_format_passed=True, complete_payload_readback=True)
    return row


def main():
    OUT.mkdir(parents=True, exist_ok=False)
    start = time.time()
    report = dict(schema='FINAL_BOARD_ARCHIVE_SPACE_REVIEW_V1', hardware_modified=False,
                  fpga_loads=0, camera_commands=0, motor_commands=0, deleted=False, runs=[])
    client = None
    try:
        client, key = connect('192.168.2.99', 'xilinx', os.environ['PYNQ_PASSWORD'], '192.168.2.100')
        client.get_transport().set_keepalive(10)
        report.update(ssh_hostkey_sha256=key, identity_before=identity(client))
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(60)
            for source, name, remote in candidates():
                result = archive_one(client, sftp, source, name, remote)
                report['runs'].append(result)
                save(OUT/(source.name+'__'+name+'.json'), result)
                print(json.dumps(dict(run=name, eligible=result['eligible'],
                                      bytes=result.get('logical_bytes'), reason=result.get('reason'))), flush=True)
        report['identity_after'] = identity(client)
        report['passed'] = True
    except Exception as error:
        report.update(passed=False, error=type(error).__name__+': '+str(error))
        raise
    finally:
        if client:
            client.close()
        report['wall_s'] = time.time()-start
        report['reclaim_candidates'] = [r['remote'] for r in report['runs'] if r['eligible']]
        report['total_logical_bytes'] = sum(r.get('logical_bytes', 0) for r in report['runs'])
        report['total_conservative_reclaim_bytes'] = sum(r.get('conservative_reclaim_bytes', 0) for r in report['runs'])
        save(OUT/'ARCHIVE_MANIFEST.json', report)
    print('MANIFEST', OUT/'ARCHIVE_MANIFEST.json')


if __name__ == '__main__':
    main()
