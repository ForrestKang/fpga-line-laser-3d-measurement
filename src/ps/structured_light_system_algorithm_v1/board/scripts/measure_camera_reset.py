"""One explicit 20-second low/high measurement; no Overlay load or SCCB."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import sys
import time

import diagnose_camera_recovery as entry


def worker(directory):
    directory = Path(directory)
    previous = json.loads((directory / 'evidence.json').read_text())
    result = {'run_id': directory.name, 'status': 'running', 'phases': [],
              'physical_voltages': 'awaiting_user_measurement', 'overlay_loaded_by_this_probe': False,
              'sccb_commands_sent': False}
    path = directory / 'results/reset_window.json'
    save = lambda: entry.write_json(path, result)
    diag = None
    allow_control = False
    lock = None
    started = time.monotonic()
    save()
    def interrupted(signum, frame): raise TimeoutError('measurement interrupted; restore RST')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        import fcntl
        lock = open('/tmp/structured_light_camera_recovery.lock', 'a')
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        from pynq import PL, MMIO
        if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != previous['boot_id']:
            raise RuntimeError('boot changed; measurement not authorized against this session')
        if previous.get('overlay_load_completed') is not True or previous['loaded_bitfile'] != PL.bitfile_name:
            raise RuntimeError('owned loaded Overlay is not current')
        if entry.sha(PL.bitfile_name) != entry.BIT_SHA or entry.sha(Path(PL.bitfile_name).with_suffix('.hwh')) != entry.HWH_SHA:
            raise RuntimeError('owned artifact hash mismatch')
        import camera_recovery as core
        diag = MMIO(0x43c10000, 0x10000)
        control = MMIO(0x43c00000, 0x10000)
        core.verify_runtime(diag, control)
        if diag.read(0x44) != 0 or diag.read(0x48) != 0 or diag.read(0x38) & 1:
            raise RuntimeError('transaction ownership lost or SCCB busy')
        allow_control = True
        for name, value in (('LOW_ACTIVE', 1), ('RELEASED_ACTIVE', 0)):
            diag.write(0x3c, value)
            observed = int(diag.read(0x3c))
            if observed != value: raise RuntimeError('CAM_CTRL readback mismatch')
            phase = {'phase': name, 'camera_control': observed, 'host_observation_monotonic_s': time.monotonic() - started}
            result['phases'].append(phase)
            save()
            print(json.dumps(phase), flush=True)
            time.sleep(20)
            phase['window_elapsed_s'] = time.monotonic() - started - phase['host_observation_monotonic_s']
            save()
        result['status'] = 'control_windows_completed_physical_readings_pending'
        return 0
    except Exception as error:
        result['status'] = 'failed'
        result['error'] = {'type': type(error).__name__, 'message': str(error)}
        return 1
    finally:
        if allow_control:
            try:
                diag.write(0x3c, 0)
                result['final_camera_control'] = int(diag.read(0x3c))
                result['final_pclk_meter_hz'] = int(diag.read(12))
            except Exception as restore_error:
                result['restore_error'] = str(restore_error)
        result['elapsed_monotonic_s'] = time.monotonic() - started
        save()
        if lock is not None: lock.close()


def main(args):
    import deploy_gray_candidate as deploy
    import paramiko
    evidence = args.evidence.resolve(strict=True)
    previous = json.loads(evidence.read_text())
    if previous.get('overlay_load_completed') is not True or previous.get('action') != 'recover':
        raise ValueError('measurement requires an actual owned recovery-load record')
    run_id = 'rst_window_' + datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%Sz')
    out = entry.ROOT / 'board/results/camera_recovery' / run_id
    out.mkdir(parents=True)
    remote = '/home/xilinx/camera_recovery/' + run_id
    record = {'run_id': run_id, 'evidence_sha256': entry.sha(evidence), 'host_utc': datetime.now(timezone.utc).isoformat()}
    password = os.environ.get('PYNQ_PASSWORD')
    if not password: raise RuntimeError('set transient PYNQ_PASSWORD for explicit measurement')
    client = paramiko.SSHClient()
    sftp = None
    exit_code = 1
    contract = deploy.load_identity_contract()
    captured_out = bytearray(); captured_err = bytearray()
    class PinnedPolicy(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            deploy.validate_ssh_host_key_sha256(hashlib.sha256(key.asbytes()).hexdigest(), contract)
    client.set_missing_host_key_policy(PinnedPolicy())
    try:
        connection = socket.socket(); connection.settimeout(5); connection.bind(('192.168.2.100', 0)); connection.connect(('192.168.2.99', 22))
        client.connect('192.168.2.99', username='xilinx', password=password, sock=connection, look_for_keys=False, allow_agent=False,
                       banner_timeout=5, auth_timeout=5)
        deploy.validate_ssh_host_key_sha256(hashlib.sha256(client.get_transport().get_remote_server_key().asbytes()).hexdigest(), contract)
        rc, text, err = deploy.execute(client, deploy.remote_identity_command(), password=password, timeout_s=10)
        identity = deploy.parse_remote_identity(text); deploy.validate_remote_identity(identity, contract)
        if rc or identity['boot_id'] != previous['boot_id']: raise RuntimeError('measurement identity/boot mismatch')
        record['identity'] = identity
        rc, _, err = deploy.execute(client, 'mkdir -p ' + shlex.quote(remote), password=password, timeout_s=10)
        if rc: raise RuntimeError(err)
        sftp = client.open_sftp(); sftp.get_channel().settimeout(3)
        files = {Path(__file__): 'measure_camera_reset.py', Path(entry.__file__): 'diagnose_camera_recovery.py',
                 entry.ROOT / 'src/python/structured_light/p2/camera_recovery.py': 'camera_recovery.py', evidence: 'evidence.json'}
        hashes = {name: entry.sha(source) for source, name in files.items()}
        for source, name in files.items(): sftp.put(str(source), remote + '/' + name)
        check = "import pathlib,hashlib; p=pathlib.Path(" + repr(remote) + "); m=" + repr(hashes) + "; assert all(hashlib.sha256((p/n).read_bytes()).hexdigest()==v for n,v in m.items())"
        rc, _, err = deploy.execute(client, 'python3 -c ' + shlex.quote(check), password=password, timeout_s=10)
        if rc: raise RuntimeError('measurement upload hash mismatch: ' + err)
        record['source_sha256'] = hashes
        command = "timeout --signal=TERM --kill-after=5s 80s sudo -S -p '' env XILINX_XRT=/usr /usr/local/share/pynq-venv/bin/python3 " + shlex.quote(remote + '/measure_camera_reset.py') + ' --on-board ' + shlex.quote(remote)
        stdin, stdout, stderr = client.exec_command(command, timeout=10)
        stdin.write(password + '\n'); stdin.flush()
        channel = stdout.channel
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if channel.recv_ready():
                chunk = channel.recv(8192); captured_out.extend(chunk); print(chunk.decode(errors='replace'), end='', flush=True)
            if channel.recv_stderr_ready(): captured_err.extend(channel.recv_stderr(8192))
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                exit_code = channel.recv_exit_status(); break
            time.sleep(.05)
        else:
            channel.close(); raise TimeoutError('measurement remote deadline')
        record['remote_exit_code'] = exit_code
    except Exception as error:
        record['error'] = str(error)
    finally:
        if sftp is not None:
            try:
                record['result_download'] = deploy.bounded_download_tree(sftp, remote + '/results', out / 'remote')
            except Exception as error:
                record['result_download'] = {'complete': False, 'error': str(error)}
            if not record['result_download']['complete']: exit_code = 1
            try: sftp.close()
            except Exception as error: record['close_error'] = str(error)
        client.close()
        (out / 'stdout.txt').write_bytes(captured_out); (out / 'stderr.txt').write_bytes(captured_err)
        entry.write_json(out / 'host.json', record); print(out)
    return exit_code


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--on-board', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.on_board and args.evidence is None: parser.error('--evidence is required')
    raise SystemExit(worker(args.on_board) if args.on_board else main(args))
