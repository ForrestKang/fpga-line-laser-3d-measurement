"""Pinned-target recovery. Default inspect never loads or accesses MMIO."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import sys
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
BIT_SHA = 'c5c57b084d31242a93dbb04c6aff8d2a8ad3528f7ab741beed984d22ccc4c343'
HWH_SHA = 'de451c41db516d304a8815fb8eefe710008c6668a25b0a3e6ec270104f978e38'
STATE = Path('/home/xilinx/camera_recovery_session.json')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def verify_artifacts(directory):
    directory = Path(directory)
    hashes = {key: sha(directory / ('camera_raw.' + key)) for key in ('bit', 'hwh')}
    if hashes != {'bit': BIT_SHA, 'hwh': HWH_SHA}:
        raise RuntimeError('artifacts differ from historical successful camera_raw')
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if (manifest.get('part') != 'xc7z020clg400-1' or
            manifest.get('overlay_mode') != 'camera_raw' or not manifest.get('preboard_passed')):
        raise RuntimeError('original camera_raw manifest mismatch')
    if any(manifest['files'][key]['sha256'] != value for key, value in hashes.items()):
        raise RuntimeError('manifest artifact SHA mismatch')
    ranges = ET.parse(directory / 'camera_raw.hwh').findall('.//MEMRANGE')
    found = {(p.get('INSTANCE'), p.get('SLAVEBUSINTERFACE'), p.get('BASEVALUE')) for p in ranges}
    for item in (('processing_pipeline', 'S_AXI_CONTROL', '0x43C00000'),
                 ('processing_pipeline', 'S_AXI_DIAG', '0x43C10000')):
        if item not in found:
            raise RuntimeError('HWH control/diagnostic address contract mismatch')
    hashes['manifest'] = sha(directory / 'manifest.json')
    return hashes


def session_gate(previous, boot_id, bitfile):
    """Fail before any MMIO if session ownership cannot be demonstrated."""
    if previous.get('camera_control_recovered') is not True:
        raise RuntimeError('validate requires a successful recovery record')
    if previous.get('boot_id') != boot_id:
        raise RuntimeError('Linux boot changed; explicit recover required')
    if previous.get('loaded_bitfile') != bitfile:
        raise RuntimeError('current PL metadata differs from owned recovery session')
    if previous.get('artifact_sha256', {}).get('bit') != BIT_SHA or sha(bitfile) != BIT_SHA:
        raise RuntimeError('session bitstream hash mismatch')
    if sha(Path(bitfile).with_suffix('.hwh')) != HWH_SHA:
        raise RuntimeError('session HWH hash mismatch')
    if previous.get('capture_started_by_this_session') is not False:
        raise RuntimeError('session does not prove no START was sent')


def board_worker(args):
    # Same script runs on ARM; host transport dependencies are never imported here.
    directory = Path(args.on_board)
    record = {'schema_version': 1, 'run_id': directory.name, 'action': args.action,
              'status': 'running', 'board_utc': datetime.now(timezone.utc).isoformat(),
              'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'capture_started_by_this_session': False,
              'physical_measurements': {'vcc': 'not_measured', 'rst': 'not_measured', 'stb': 'not_measured'},
              'root_cause_identified': False}
    result = directory / 'results/result.json'
    save = lambda: write_json(result, record)
    save()
    started = time.monotonic()
    diag = control = None
    lock = None
    try:
        import fcntl
        lock = open('/tmp/structured_light_camera_recovery.lock', 'a')
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        from pynq import PL
        record['pl_metadata_bitfile'] = PL.bitfile_name
        save()
        if args.action == 'inspect':
            record.update(status='software_inspection_complete', mmio_accessed=False,
                          note='PL metadata is not proof of live hardware identity; no MMIO or camera command issued')
            save()
            return 0
        import camera_recovery as core
        if args.action == 'recover':
            record['artifact_sha256'] = verify_artifacts(directory)
            save()
            # Check independent uploaded-source hashes before any FPGA operation.
            sources = json.loads((directory / 'upload_manifest.json').read_text())
            for name, expected in sources.items():
                if sha(directory / name) != expected:
                    raise RuntimeError('uploaded source checksum mismatch: ' + name)
            from pynq import Overlay
            overlay = Overlay(str(directory / 'camera_raw.bit'), download=True)
            record['loaded_bitfile'] = str(directory / 'camera_raw.bit')
            record['overlay_load_completed'] = True
            save()
            if PL.bitfile_name != record['loaded_bitfile']:
                raise RuntimeError('PL metadata after load mismatch')
        else:
            previous = json.loads((directory / 'session_evidence.json').read_text())
            state = json.loads(STATE.read_text())
            if state != previous:
                raise RuntimeError('session evidence differs from published board ownership record')
            session_gate(previous, record['boot_id'], PL.bitfile_name)
            record['loaded_bitfile'] = previous['loaded_bitfile']
            record['artifact_sha256'] = previous['artifact_sha256']
            save()
        from pynq import MMIO
        control = MMIO(0x43c00000, 0x10000)
        diag = MMIO(0x43c10000, 0x10000)
        record['mmio_accessed'] = True
        core.verify_runtime(diag, control)
        ownership_before = (int(diag.read(0x44)), int(diag.read(0x48)))
        if args.action == 'validate':
            if (int(diag.read(0x44)), int(diag.read(0x48))) != (previous['epoch'], previous['last_request_id']):
                raise RuntimeError('capture epoch/request changed; recovery ownership lost')
        else:
            core.recover(diag, control, record, save)
        core.validate(diag, control, record, save)
        if (int(diag.read(0x44)), int(diag.read(0x48))) != ownership_before:
            record['camera_control_recovered'] = False
            raise RuntimeError('capture epoch/request changed during validation')
        if int(diag.read(0x3c)) != 0:
            record['camera_control_recovered'] = False
            raise RuntimeError('camera control changed during validation')
        record.update(status='camera_control_recovered', epoch=int(diag.read(0x44)),
                      last_request_id=int(diag.read(0x48)))
        save()
        return 0
    except Exception as error:
        record['status'] = 'failed'
        record['error'] = {'type': type(error).__name__, 'message': str(error)}
        if diag is not None:
            try:
                record['failure_snapshot'] = core.snapshot(diag, control)
            except Exception as snapshot_error:
                record['snapshot_error'] = str(snapshot_error)
            try:
                record['failure_classification'] = core.classify_failure(record)
            except Exception as classification_error:
                record['classification_error'] = str(classification_error)
        save()
        return 1
    finally:
        record['elapsed_monotonic_s'] = time.monotonic() - started
        save()
        if record['status'] == 'camera_control_recovered':
            write_json(STATE, record)
        if lock is not None:
            lock.close()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--action', choices=('inspect', 'recover', 'validate'), default='inspect')
    result.add_argument('--session-evidence', type=Path)
    result.add_argument('--on-board', help=argparse.SUPPRESS)
    return result


def host_main(args):
    import deploy_gray_candidate as deploy
    import paramiko
    import getpass
    run_id = args.action + '_' + datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%Sz') + '_' + uuid.uuid4().hex[:8]
    output = ROOT / 'board/results/camera_recovery' / run_id
    output.mkdir(parents=True)
    record = {'run_id': run_id, 'action': args.action, 'host_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'running', 'root_cause_identified': False}
    save = lambda: write_json(output / 'host.json', record)
    save()
    client = None
    sftp = None
    remote = '/home/xilinx/camera_recovery/' + run_id
    exit_code = 1
    try:
        contract = deploy.load_identity_contract()
        if args.action == 'validate' and args.session_evidence is None:
            raise ValueError('validate requires --session-evidence pointing to successful result.json')
        release = ROOT / 'board/overlays/camera_raw'
        record['artifact_sha256'] = verify_artifacts(release)
        password = os.environ.get('PYNQ_PASSWORD') or getpass.getpass('PYNQ SSH password: ')
        connection = socket.socket()
        connection.settimeout(5)
        connection.bind(('192.168.2.100', 0))
        connection.connect(('192.168.2.99', 22))
        client = paramiko.SSHClient()
        class PinnedPolicy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                deploy.validate_ssh_host_key_sha256(hashlib.sha256(key.asbytes()).hexdigest(), contract)
        client.set_missing_host_key_policy(PinnedPolicy())
        client.connect('192.168.2.99', username='xilinx', password=password, sock=connection,
                       look_for_keys=False, allow_agent=False, auth_timeout=5, banner_timeout=5)
        actual = hashlib.sha256(client.get_transport().get_remote_server_key().asbytes()).hexdigest()
        deploy.validate_ssh_host_key_sha256(actual, contract)
        record['ssh_host_key_sha256'] = actual
        rc, out, err = deploy.execute(client, deploy.remote_identity_command(), password=password, timeout_s=10)
        if rc:
            raise RuntimeError('remote identity read failed: ' + err)
        identity = deploy.parse_remote_identity(out)
        deploy.validate_remote_identity(identity, contract)
        record['identity'] = identity
        save()
        rc, out, err = deploy.execute(client, 'mkdir -p ' + shlex.quote(remote), password=password, timeout_s=10)
        if rc:
            raise RuntimeError('cannot create remote evidence directory: ' + err)
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(3)
        files = {Path(__file__): 'diagnose_camera_recovery.py',
                 ROOT / 'src/python/structured_light/p2/camera_recovery.py': 'camera_recovery.py'}
        if args.action == 'recover':
            for name in ('camera_raw.bit', 'camera_raw.hwh', 'manifest.json'):
                files[release / name] = name
        if args.session_evidence is not None:
            files[args.session_evidence.resolve(strict=True)] = 'session_evidence.json'
        hashes = {name: sha(source) for source, name in files.items()}
        write_json(output / 'upload_manifest.json', hashes)
        files[output / 'upload_manifest.json'] = 'upload_manifest.json'
        for source, name in files.items():
            sftp.put(str(source), remote + '/' + name)
        verify = "import pathlib,json,hashlib; p=pathlib.Path(" + repr(remote) + "); m=json.loads((p/'upload_manifest.json').read_text()); assert all(hashlib.sha256((p/n).read_bytes()).hexdigest()==v for n,v in m.items()); print('upload_sha_verified')"
        rc, out, err = deploy.execute(client, 'python3 -c ' + shlex.quote(verify), password=password, timeout_s=10)
        if rc or out.strip() != 'upload_sha_verified':
            raise RuntimeError('remote upload verification failed: ' + out + err)
        record['remote_source_hashes'] = hashes
        save()
        command = ('timeout --signal=TERM --kill-after=5s 80s sudo -S -p ' + shlex.quote('') +
                   ' env XILINX_XRT=/usr /usr/local/share/pynq-venv/bin/python3 ' +
                   shlex.quote(remote + '/diagnose_camera_recovery.py') + ' --on-board ' + shlex.quote(remote) +
                   ' --action ' + args.action)
        rc, out, err = deploy.execute(client, command, password=password, timeout_s=90)
        (output / 'stdout.txt').write_text(out, encoding='utf-8')
        (output / 'stderr.txt').write_text(err, encoding='utf-8')
        record['remote_exit_code'] = rc
        record['status'] = 'complete' if rc == 0 else 'failed'
        exit_code = 0 if rc == 0 else 1
    except Exception as error:
        record.update(status='failed', error={'type': type(error).__name__, 'message': str(error)})
        if isinstance(error, deploy.RemoteCommandTimeout):
            (output / 'stdout.txt').write_bytes(error.stdout)
            (output / 'stderr.txt').write_bytes(error.stderr)
        exit_code = 1
    finally:
        if sftp is not None:
            try:
                record['result_download'] = deploy.bounded_download_tree(sftp, remote + '/results', output / 'remote')
            except Exception as pull_error:
                record['result_download'] = {'complete': False, 'error': str(pull_error)}
            if not record['result_download']['complete']:
                record['status'] = 'failed_evidence_download'
                exit_code = 1
            else:
                try:
                    board_result = json.loads((output / 'remote/result.json').read_text(encoding='utf-8'))
                    if board_result['run_id'] != run_id or board_result['action'] != args.action:
                        raise RuntimeError('downloaded result correlation mismatch')
                    record['board_status'] = board_result['status']
                except Exception as error:
                    record.update(status='failed_result_correlation', result_error=str(error))
                    exit_code = 1
            try:
                sftp.close()
            except Exception as close_error:
                record['sftp_close_error'] = str(close_error)
        if client is not None:
            client.close()
        save()
        print(output)
    return exit_code


if __name__ == '__main__':
    args = parser().parse_args()
    raise SystemExit(board_worker(args) if args.on_board else host_main(args))
