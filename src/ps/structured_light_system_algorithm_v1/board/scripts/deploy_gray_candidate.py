"""Deploy a hash-bound candidate to the pinned PYNQ-Z2 over direct Ethernet."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import socket
import tarfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
HOST_KEY = '4c1d057c68df33381952ffeb789813a5eabf00cebcb0c0a9182e719ba3e2b7ba'
IDENTITY_CONTRACT = ROOT/'board/configs/p2_target_identity.json'
GATE_A_MODES = {'camera_raw_monitor', 'camera_raw_hdmi'}
SFTP_IO_TIMEOUT_S = 3.0
RESULTS_PULL_TIMEOUT_S = 20.0
RESULTS_MAX_DEPTH = 8
RESULTS_MAX_FILES = 256
RESULTS_MAX_ENTRIES = 2048
RESULTS_MAX_FILE_BYTES = 64 * 1024 * 1024
RESULTS_MAX_TOTAL_BYTES = 512 * 1024 * 1024


class RemoteCommandTimeout(TimeoutError):
    """A remote command exceeded its deadline, retaining received output."""

    def __init__(self, message: str, *, timeout_s: float, stdout: bytes, stderr: bytes):
        super().__init__(message)
        self.timeout_s = timeout_s
        self.stdout = stdout
        self.stderr = stderr


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _set_sftp_timeout(sftp, timeout_s: float = SFTP_IO_TIMEOUT_S) -> None:
    if timeout_s <= 0:
        raise ValueError('SFTP I/O timeout must be positive')
    channel = sftp.get_channel()
    channel.settimeout(timeout_s)


def bounded_download_tree(sftp, remote_dir: str, local_dir: Path, *,
                          timeout_s: float = RESULTS_PULL_TIMEOUT_S,
                          max_depth: int = RESULTS_MAX_DEPTH,
                          max_files: int = RESULTS_MAX_FILES,
                          max_entries: int = RESULTS_MAX_ENTRIES,
                          max_file_bytes: int = RESULTS_MAX_FILE_BYTES,
                          max_total_bytes: int = RESULTS_MAX_TOTAL_BYTES) -> dict:
    """Best-effort bounded pull of already-produced evidence; never runs capture."""
    import stat

    if min(timeout_s, max_depth, max_files, max_entries, max_file_bytes, max_total_bytes) <= 0:
        raise ValueError('download bounds must be positive')
    local_dir = local_dir.resolve()
    local_dir.mkdir(parents=True, exist_ok=True)
    report = {'remote_dir': remote_dir, 'attempted': True, 'downloaded_files': [],
              'downloaded_bytes': 0, 'skipped': [], 'errors': [], 'complete': False,
              'limits': {'timeout_s': timeout_s, 'max_depth': max_depth,
                         'max_files': max_files, 'max_entries': max_entries,
                         'max_file_bytes': max_file_bytes,
                         'max_total_bytes': max_total_bytes}}
    deadline = time.monotonic() + timeout_s
    visited = 0
    entries_seen = 0
    stop_walk = False

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError('bounded results download deadline')
        _set_sftp_timeout(sftp, min(SFTP_IO_TIMEOUT_S, value))
        return value

    def walk(source: str, target: Path, depth: int):
        nonlocal visited, entries_seen, stop_walk
        if stop_walk:
            return
        if depth > max_depth:
            report['skipped'].append({'path': source, 'reason': 'maximum directory depth exceeded'})
            return
        remaining()
        try:
            iterator = sftp.listdir_iter(source, read_aheads=1)
        except Exception as exc:
            report['errors'].append({'path': source, 'error': type(exc).__name__ + ': ' + str(exc)})
            return
        try:
            for entry in iterator:
                if stop_walk:
                    return
                remaining()
                entries_seen += 1
                if entries_seen > max_entries:
                    report['skipped'].append({'path': source, 'reason': 'directory-entry limit exceeded'})
                    stop_walk = True
                    return
                name = entry.filename
                if not name or name in {'.', '..'} or '/' in name or '\\' in name:
                    report['skipped'].append({'path': source, 'reason': 'invalid remote filename'})
                    continue
                remote_path = source.rstrip('/') + '/' + name
                local_path = (target / name).resolve()
                try:
                    local_path.relative_to(local_dir)
                except ValueError:
                    report['skipped'].append({'path': remote_path, 'reason': 'local path escaped result root'})
                    continue
                mode = getattr(entry, 'st_mode', 0)
                if stat.S_ISDIR(mode):
                    walk(remote_path, local_path, depth + 1)
                    continue
                if not stat.S_ISREG(mode):
                    report['skipped'].append({'path': remote_path, 'reason': 'non-regular remote entry'})
                    continue
                size = getattr(entry, 'st_size', None)
                if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                    report['skipped'].append({'path': remote_path, 'reason': 'invalid remote file size'})
                    continue
                if size > max_file_bytes:
                    report['skipped'].append({'path': remote_path, 'reason': 'per-file byte limit exceeded', 'size': size})
                    continue
                if report['downloaded_bytes'] + size > max_total_bytes:
                    report['skipped'].append({'path': remote_path, 'reason': 'total byte limit exceeded', 'size': size})
                    continue
                if visited >= max_files:
                    report['skipped'].append({'path': remote_path, 'reason': 'file-count limit exceeded'})
                    stop_walk = True
                    return
                visited += 1
                target.mkdir(parents=True, exist_ok=True)
                partial = local_path.with_name(local_path.name + '.partial')
                try:
                    remaining()
                    sftp.get(remote_path, str(partial))
                    actual_size = partial.stat().st_size
                    if actual_size != size:
                        report['errors'].append({'path': remote_path, 'error':
                            f'byte count mismatch: expected {size}, got {actual_size}'})
                        continue
                    partial.replace(local_path)
                    report['downloaded_files'].append({'path': str(local_path.relative_to(local_dir)),
                                                       'size': actual_size,
                                                       'sha256': _sha256(local_path)})
                    report['downloaded_bytes'] += actual_size
                except Exception as exc:
                    report['errors'].append({'path': remote_path,
                                             'error': type(exc).__name__ + ': ' + str(exc)})
                    if time.monotonic() >= deadline:
                        raise
        finally:
            close = getattr(iterator, 'close', None)
            if callable(close):
                close()

    try:
        walk(remote_dir, local_dir, 0)
        report['complete'] = not report['errors'] and not report['skipped']
    except Exception as exc:
        report['errors'].append({'path': remote_dir, 'error': type(exc).__name__ + ': ' + str(exc)})
    return report


def load_identity_contract(path: Path = IDENTITY_CONTRACT) -> dict:
    """Load the pinned board identity captured from local real-board evidence."""
    contract = json.loads(path.read_text(encoding='utf-8'))
    if contract.get('schema_version') != 1 or contract.get('target') != 'PYNQ-Z2':
        raise RuntimeError('unsupported target identity contract')
    if contract.get('ssh_host_key_sha256') != HOST_KEY:
        raise RuntimeError('target identity contract SSH pin does not match compiled pin')
    machine = contract.get('machine_identity')
    if not isinstance(machine, dict):
        raise RuntimeError('target identity contract is missing machine identity')
    for key in ('hostname', 'machine', 'device_tree_model', 'device_tree_compatible'):
        if key not in machine:
            raise RuntimeError(f'target identity contract is missing {key}')
    if not isinstance(machine['hostname'], str) or not machine['hostname']:
        raise RuntimeError('invalid pinned board hostname')
    if not isinstance(machine['machine'], str) or not machine['machine']:
        raise RuntimeError('invalid pinned board machine architecture')
    compatible = machine['device_tree_compatible']
    if compatible is not None and (not isinstance(compatible, list) or
                                   not all(isinstance(item, str) and item for item in compatible)):
        raise RuntimeError('invalid pinned device-tree compatible list')
    return contract


def parse_remote_identity(payload: str) -> dict:
    """Parse the single JSON identity object emitted by the ARM-side probe."""
    try:
        identity = json.loads(payload.strip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError('remote board identity is not valid JSON') from exc
    if not isinstance(identity, dict):
        raise RuntimeError('remote board identity must be a JSON object')
    required = {'hostname', 'machine', 'boot_id', 'device_tree_model', 'device_tree_compatible'}
    if not required.issubset(identity):
        raise RuntimeError('remote board identity JSON is incomplete')
    if not isinstance(identity['hostname'], str) or not isinstance(identity['machine'], str):
        raise RuntimeError('remote hostname/machine fields are malformed')
    if not isinstance(identity['boot_id'], str) or not identity['boot_id']:
        raise RuntimeError('remote boot_id is missing; retain it as run evidence')
    model = identity['device_tree_model']
    compatible = identity['device_tree_compatible']
    if model is not None and not isinstance(model, str):
        raise RuntimeError('remote device-tree model has an invalid type')
    if compatible is not None and (not isinstance(compatible, list) or
                                   not all(isinstance(item, str) for item in compatible)):
        raise RuntimeError('remote device-tree compatible has an invalid type')
    return identity


def validate_remote_identity(identity: dict, contract: dict) -> None:
    if contract.get('user_confirmed_board') != 'PYNQ-Z2':
        raise RuntimeError('identity contract lacks user-confirmed PYNQ-Z2 target')
    expected = contract['machine_identity']
    for field in ('hostname', 'machine', 'device_tree_model', 'device_tree_compatible'):
        if identity.get(field) != expected[field]:
            raise RuntimeError(
                f'target identity mismatch for {field}: expected {expected[field]!r}, '
                f'got {identity.get(field)!r}')


def validate_ssh_host_key_sha256(actual: str, contract: dict) -> None:
    """Check the server key on every connection, including known_hosts hits."""
    expected = contract.get('ssh_host_key_sha256')
    if not isinstance(actual, str) or actual != expected or actual != HOST_KEY:
        raise RuntimeError('Target SSH host-key identity mismatch')


def remote_identity_command() -> str:
    """Read board identity as JSON; boot_id is captured but intentionally not pinned."""
    code = (
        "import json,platform,socket,pathlib; "
        "read=lambda p: (pathlib.Path(p).read_bytes() if pathlib.Path(p).is_file() else None); "
        "model=read('/proc/device-tree/model'); comp=read('/proc/device-tree/compatible'); "
        "model=model.rstrip(b'\\0').decode('utf-8') if model is not None else None; "
        "comp=[x.decode('utf-8') for x in comp.split(b'\\0') if x] if comp is not None else None; "
        "boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(); "
        "print(json.dumps({'hostname':socket.gethostname(),'machine':platform.machine(),"
        "'boot_id':boot,'device_tree_model':model,'device_tree_compatible':comp},"
        "sort_keys=True,separators=(',',':')))"
    )
    return 'python3 -c '+shlex.quote(code)


def _strict_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f'Gate-A {name} must be an integer')
    return value


def validate_prior_gate_a(confirmed_config: Path, overlays_root: Path) -> tuple[dict, Path, dict]:
    """Verify the original Gate-A file, its measured values and local source release."""
    import yaml

    previous = yaml.safe_load(confirmed_config.read_text(encoding='utf-8'))
    if not isinstance(previous, dict) or previous.get('hardware_confirmed') is not True:
        raise RuntimeError('confirmed config is not a Gate-A hardware confirmation')
    evidence = (confirmed_config.parent/'signals.json').resolve(strict=True)
    declared = Path(previous.get('gate_a_evidence', ''))
    if not declared.is_absolute():
        declared = confirmed_config.parent/declared
    if declared.resolve(strict=True) != evidence:
        raise RuntimeError('confirmed config Gate-A evidence path does not name sibling signals.json')
    evidence_bytes = evidence.read_bytes()
    evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
    if previous.get('gate_a_sha256') != evidence_sha:
        raise RuntimeError('prior signal evidence SHA mismatch')
    try:
        gate = json.loads(evidence_bytes)
    except json.JSONDecodeError as exc:
        raise RuntimeError('prior signal evidence is not valid JSON') from exc
    if (not isinstance(gate, dict) or gate.get('status') != 'camera_signal_verified' or
            gate.get('camera_signal_verified') is not True):
        raise RuntimeError('prior Gate-A signal evidence is not a verified monitor result')

    values = {
        'width': _strict_int(gate.get('width'), 'width'),
        'height': _strict_int(gate.get('height'), 'height'),
        'fv_active': _strict_int(gate.get('fv_active'), 'fv_active'),
        'lv_active': _strict_int(gate.get('lv_active'), 'lv_active'),
        'pclk_hz': _strict_int(gate.get('pclk_hz'), 'pclk_hz'),
    }
    if values['width'] not in (640, 752) or values['height'] != 480:
        raise RuntimeError('prior Gate-A geometry is unsupported')
    if values['fv_active'] not in (0, 1) or values['lv_active'] not in (0, 1):
        raise RuntimeError('prior Gate-A signal polarities are invalid')
    if values['pclk_hz'] <= 0:
        raise RuntimeError('prior Gate-A PCLK is not measured')
    period = gate.get('frame_period_s')
    if isinstance(period, bool) or not isinstance(period, (int, float)) or not math.isfinite(period) or period <= 0:
        raise RuntimeError('prior Gate-A frame period is invalid')
    values['frame_period_s'] = float(period)
    for key, observed in values.items():
        configured = previous.get(key)
        if key == 'frame_period_s':
            if isinstance(configured, bool) or not isinstance(configured, (int, float)) or not math.isfinite(configured) or not math.isclose(float(configured), observed, rel_tol=1e-12, abs_tol=1e-12):
                raise RuntimeError('confirmed config frame_period_s differs from hashed Gate-A evidence')
        elif _strict_int(configured, key) != observed:
            raise RuntimeError(f'confirmed config {key} differs from hashed Gate-A evidence')

    mode = gate.get('overlay_mode')
    if mode not in GATE_A_MODES:
        raise RuntimeError('Gate-A evidence must come from a monitor-capable local release')
    release = overlays_root/mode
    manifest_path = release/'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('preboard_passed') is not True or manifest.get('overlay_mode') != mode:
        raise RuntimeError('Gate-A source is not a locally preboard-approved monitor/HDMI release')
    if manifest.get('part', manifest.get('device')) != 'xc7z020clg400-1':
        raise RuntimeError('Gate-A source release targets a different FPGA part')
    if (manifest.get('frame_width'), manifest.get('frame_height')) != (values['width'], values['height']):
        raise RuntimeError('Gate-A geometry does not match its local source release')
    artifact_sha = gate.get('artifact_sha256')
    if not isinstance(artifact_sha, dict):
        raise RuntimeError('Gate-A evidence is missing source artifact fingerprints')
    actual_hashes = {}
    for key, suffix in (('bit', 'bit'), ('hwh', 'hwh')):
        artifact_path = release/f'{mode}.{suffix}'
        actual = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        expected = manifest.get('files', {}).get(key, {}).get('sha256')
        if actual != expected or artifact_sha.get(key) != actual:
            raise RuntimeError(f'Gate-A source {key} does not match a signed local release')
        actual_hashes[key] = actual
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if artifact_sha.get('manifest') != manifest_sha:
        raise RuntimeError('Gate-A source manifest fingerprint is not a local signed release')
    actual_hashes['manifest'] = manifest_sha
    # Propagate confirmation only after geometry, evidence and release hashes
    # have all passed. The deployment template intentionally starts unconfirmed.
    return values | {'hardware_confirmed': True, 'gate_a_sha256': evidence_sha,
                     'gate_a_source_overlay_mode': mode,
                     'gate_a_source_artifact_sha256': actual_hashes,
                     'gate_a_source_preboard_passed': True}, evidence, gate


def execute(client, command, *, password, timeout_s=90):
    if timeout_s <= 0:
        raise ValueError('remote command timeout must be positive')
    stdin, stdout, stderr = client.exec_command(command, timeout=10)
    stdin.write(password+'\n'); stdin.flush()
    ch = stdout.channel
    deadline = time.monotonic()+timeout_s
    out, err = bytearray(), bytearray()
    while not ch.exit_status_ready():
        while ch.recv_ready(): out.extend(ch.recv(65536))
        while ch.recv_stderr_ready(): err.extend(ch.recv_stderr(65536))
        if time.monotonic() > deadline:
            # Drain bytes already buffered before closing; preserve both streams
            # on the exception so the caller can save them beside the run log.
            while ch.recv_ready(): out.extend(ch.recv(65536))
            while ch.recv_stderr_ready(): err.extend(ch.recv_stderr(65536))
            try:
                ch.close()
            finally:
                raise RemoteCommandTimeout('remote command deadline; command was not retried',
                                          timeout_s=timeout_s,
                                          stdout=bytes(out), stderr=bytes(err))
        time.sleep(.05)
    while ch.recv_ready(): out.extend(ch.recv(65536))
    while ch.recv_stderr_ready(): err.extend(ch.recv_stderr(65536))
    return ch.recv_exit_status(), out.decode(errors='replace'), err.decode(errors='replace')


def main():
    import paramiko
    import yaml
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--overlay', choices=['camera_raw_monitor', 'camera_raw_hdmi', 'structured_light_profile','structured_light_profile_hdmi'], required=True)
    parser.add_argument('--stage', choices=['signals', 'first-frame', 'hdmi', 'scene-a', 'scene-b', 'scene-aprime', 'profile', 'laser-profile','profile-hdmi'], required=True)
    parser.add_argument('--confirmed-config', type=Path)
    parser.add_argument('--campaign', choices=['initial', 'fixed'], default='initial')
    parser.add_argument('--count', type=int, default=10, help='Paired Profile requests; raw/scene counts are fixed by their runner')
    parser.add_argument('--threshold', type=int, default=30)
    parser.add_argument('--roi', nargs=4, type=int)
    args = parser.parse_args()
    paired = args.stage in ('profile', 'laser-profile','profile-hdmi')
    interactive_hdmi = args.stage == 'profile-hdmi'
    if paired != (args.overlay in ('structured_light_profile','structured_light_profile_hdmi')):
        parser.error('Profile stages require the paired Profile Overlay, and that Overlay only accepts Profile stages')
    if interactive_hdmi != (args.overlay == 'structured_light_profile_hdmi'):
        parser.error('Combined Profile/HDMI Overlay requires the interactive profile-hdmi stage')
    if interactive_hdmi and args.count != 10:
        parser.error('Interactive coexistence acceptance requires exactly ten captures')
    if not 1 <= args.count <= 100 or not 0 <= args.threshold <= 255:
        parser.error('count must be 1..100 and threshold 0..255')
    if args.stage == 'hdmi' and args.overlay != 'camera_raw_hdmi':
        parser.error('HDMI stage requires camera_raw_hdmi; no upload or download performed')
    identity_contract = load_identity_contract()
    release = ROOT/'board/overlays'/args.overlay
    manifest_path = release/'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (manifest.get('preboard_passed') is not True or
            manifest.get('overlay_mode') != args.overlay or
            manifest.get('part', manifest.get('device')) != 'xc7z020clg400-1'):
        raise RuntimeError('candidate has not passed real preboard audit')
    for kind in ('bit', 'hwh'):
        p = release/f'{args.overlay}.{kind}'
        if hashlib.sha256(p.read_bytes()).hexdigest() != manifest['files'][kind]['sha256']:
            raise RuntimeError('candidate artifact SHA mismatch')
    gate_values, prior_evidence = {}, None
    if args.stage != 'signals':
        if not args.confirmed_config:
            raise RuntimeError('first-frame/HDMI deployment requires prior measured signal config')
        gate_values, prior_evidence, _ = validate_prior_gate_a(
            args.confirmed_config, ROOT/'board/overlays')
    password = os.environ.get('PYNQ_PASSWORD') or getpass.getpass('PYNQ password: ')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
    result_group = 'profile_hdmi' if paired else 'gray_hdmi'
    run_name = f'{args.campaign}_{args.stage}_{run_id}' if args.campaign != 'initial' else f'{args.stage}_{run_id}'
    output = ROOT/'board/results'/result_group/run_name
    output.mkdir(parents=True, exist_ok=False)
    remote = '/home/xilinx/gray_hdmi_'+run_id
    config = {
        'board': 'pynq_z2', 'part': 'xc7z020clg400-1', 'camera': 'atk_mt9v034',
        'physical_revision': 'v1.5', 'pin_mapping_confirmed': True,
        'pin_confirmation_source': 'user confirmed 17 wires and PCLK/RST/STB in prior camera-control stage',
        'hardware_confirmed': False, 'overlay_mode': args.overlay,
        'bitfile': remote+f'/board/overlays/{args.overlay}/{args.overlay}.bit',
        'hwh': remote+f'/board/overlays/{args.overlay}/{args.overlay}.hwh',
        'manifest': remote+f'/board/overlays/{args.overlay}/manifest.json',
        'identity_contract': remote+'/board/configs/p2_target_identity.json',
        'identity_contract_sha256': _sha256(IDENTITY_CONTRACT),
        'ssh_host_key_sha256': identity_contract['ssh_host_key_sha256'],
        'user_confirmed_board': identity_contract['user_confirmed_board'],
    }
    record = {'host_started_utc': datetime.now(timezone.utc).isoformat(), 'remote_root': remote,
              'target': 'PYNQ-Z2', 'ip': '192.168.2.99', 'stage': args.stage,
              'overlay': args.overlay, 'campaign': args.campaign, 'hardware_result': 'not_tested'}
    client, sock, sftp = paramiko.SSHClient(), socket.socket(), None
    class Pinned(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, _client, hostname, key):
            actual = hashlib.sha256(key.asbytes()).hexdigest()
            if actual != HOST_KEY: raise RuntimeError('Target SSH identity changed')
            record['ssh_host_key_sha256'] = actual
    phase = 'connect'
    board_execution_started = False
    results_pull_attempted = False
    client.set_missing_host_key_policy(Pinned())
    try:
        sock.settimeout(5); sock.bind(('192.168.2.100',0)); sock.connect(('192.168.2.99',22))
        client.connect('192.168.2.99',username='xilinx',password=password,sock=sock,
                       look_for_keys=False,allow_agent=False,timeout=5,auth_timeout=5,banner_timeout=5)
        server_key = client.get_transport().get_remote_server_key()
        actual_host_key = hashlib.sha256(server_key.asbytes()).hexdigest()
        validate_ssh_host_key_sha256(actual_host_key, identity_contract)
        record['ssh_host_key_sha256'] = actual_host_key
        phase = 'read_target_identity'
        identity_code, identity_out, identity_err = execute(
            client, remote_identity_command(), password=password)
        if identity_code:
            raise RuntimeError('could not read target board identity: '+identity_err)
        board_identity = parse_remote_identity(identity_out)
        validate_remote_identity(board_identity, identity_contract)
        record['board_identity_readback'] = board_identity
        config['board_identity'] = {
            key: board_identity[key] for key in
            ('hostname', 'machine', 'device_tree_model', 'device_tree_compatible')}
        config['board_identity_capture'] = {
            'boot_id': board_identity['boot_id'],
            'captured_utc': datetime.now(timezone.utc).isoformat(),
        }
        if args.stage != 'signals':
            config.update(gate_values)
            config['gate_a_evidence'] = remote+'/prior_signals.json'
        if paired:
            config['threshold'] = args.threshold
            config['roi'] = args.roi or [0,0,config['width'],config['height']]
            x0,y0,x1,y1 = config['roi']
            if not (0 <= x0 < x1 <= config['width'] and 0 <= y0 < y1 <= config['height']):
                raise ValueError('ROI must be half-open and within measured geometry')
        config_path = output/'run_config.yaml'
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
        package = output/'deployment.tar.gz'
        file_hashes = {}
        with tarfile.open(package, 'w:gz') as archive:
            files = list((ROOT/'src/python').rglob('*.py')) + list((ROOT/'src/contracts').glob('*.json'))
            files += [ROOT/'board/configs/p2_target_identity.json',
                      ROOT/'board/scripts/verify_p2_board.py', ROOT/'board/scripts/gray_capture_board.py']
            if paired:
                files.append(ROOT/'board/scripts/profile_capture_board.py')
            if interactive_hdmi:
                files.extend([ROOT/'board/scripts/profile_dashboard_board.py',ROOT/'board/scripts/profile_hdmi_acceptance.py'])
                files.extend((ROOT/'board/notebooks').glob('*profile*.ipynb'))
            files += [manifest_path, release/f'{args.overlay}.bit', release/f'{args.overlay}.hwh']
            for p in files:
                rel = p.relative_to(ROOT).as_posix()
                archive.add(p, arcname=rel)
                file_hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            archive.add(config_path, arcname='run_config.yaml')
            file_hashes['run_config.yaml'] = _sha256(config_path)
            if prior_evidence is not None:
                archive.add(prior_evidence, arcname='prior_signals.json')
                file_hashes['prior_signals.json'] = _sha256(prior_evidence)
        record['deployment_sha256'] = _sha256(package)
        record['source_files'] = file_hashes
        phase = 'upload_and_hash_readback'
        sftp=client.open_sftp(); _set_sftp_timeout(sftp)
        sftp.mkdir(remote); sftp.put(str(package), remote+'/deployment.tar.gz')
        hashes=json.dumps(file_hashes)
        # Safe known-root extraction; the archive is generated solely from an
        # allowlist above, and every uploaded file is independently rehashed.
        unpack = "import tarfile,pathlib,hashlib,json;root=pathlib.Path("+repr(remote)+");p=root/'deployment.tar.gz';assert hashlib.sha256(p.read_bytes()).hexdigest()=="+repr(record['deployment_sha256'])+";t=tarfile.open(p);assert all(not m.issym() and not m.islnk() and (root/m.name).resolve().is_relative_to(root) for m in t.getmembers());t.extractall(root);expected=json.loads("+repr(hashes)+");assert all(hashlib.sha256((root/k).read_bytes()).hexdigest()==v for k,v in expected.items());print('UPLOAD_HASH_VERIFIED')"
        rc, data, err = execute(client, '/usr/local/share/pynq-venv/bin/python3 -c '+shlex.quote(unpack), password=password)
        if rc: raise RuntimeError('independent upload readback failed: '+err)
        # Create the result directory as the pinned SSH user before sudo starts
        # the board runner. The root runner can write its evidence, while the
        # same SSH user can atomically create nonce-bound operator replies.
        # Do not recursively change ownership of captured evidence.
        sftp.mkdir(remote+'/results')
        budget = 1080 if interactive_hdmi else max(180, args.count*8+30) if paired else 80
        runner = 'profile_hdmi_acceptance.py' if interactive_hdmi else 'profile_capture_board.py' if paired else 'verify_p2_board.py'
        command = f"timeout --signal=TERM --kill-after=5s {budget}s sudo -S -p '' env XILINX_XRT=/usr /usr/local/share/pynq-venv/bin/python3 "+shlex.quote(remote+'/board/scripts/'+runner)+' --config '+shlex.quote(remote+'/run_config.yaml')+' --output '+shlex.quote(remote+'/results')
        if paired and not interactive_hdmi:
            command += ' --count '+str(args.count)
            if args.stage == 'laser-profile': command += ' --laser-mode'
        elif not interactive_hdmi:
            command += ' --stage '+shlex.quote(args.stage)
        phase = 'board_acceptance'
        board_execution_started = True
        rc, data, err = execute(client,command,password=password,timeout_s=budget+10)
        record['exit_code']=rc
        (output/'stdout.txt').write_text(data,encoding='utf-8');(output/'stderr.txt').write_text(err,encoding='utf-8')
        phase = 'retrieve_results'
        results_pull_attempted = True
        transfer = bounded_download_tree(sftp, remote+'/results', output/'results',
                                         timeout_s=60 if paired else RESULTS_PULL_TIMEOUT_S,
                                         max_files=4096 if paired else RESULTS_MAX_FILES)
        (output/'remote_results_download.json').write_text(
            json.dumps(transfer, indent=2)+'\n', encoding='utf-8')
        record['results_transfer'] = transfer
        summaries=list((output/'results').glob('summary.json' if paired else '*/summary.json'))
        record['summary_files']=[str(p.relative_to(output)) for p in summaries]
        record['hardware_result']='stage_passed' if rc==0 else 'failed_or_pending'
        if not transfer['complete'] or not summaries:
            raise RuntimeError('bounded result retrieval was incomplete; inspect remote_results_download.json')
    except Exception as exc:
        record['failure_phase'] = phase
        record['error']=type(exc).__name__+': '+str(exc)
        if isinstance(exc, RemoteCommandTimeout):
            record['remote_command_timeout_s'] = exc.timeout_s
            record['remote_command_not_retried'] = True
            (output/'stdout.partial.txt').write_bytes(exc.stdout)
            (output/'stderr.partial.txt').write_bytes(exc.stderr)
        # If the remote board runner had started, preserve whatever evidence it
        # had already produced. This is a read-only, bounded retrieval attempt;
        # it never invokes or retries the board capture command.
        if sftp is not None and board_execution_started and not results_pull_attempted:
            results_pull_attempted = True
            try:
                transfer = bounded_download_tree(
                    sftp, remote+'/results', output/'results', timeout_s=30.0 if paired else 8.0,
                    max_depth=RESULTS_MAX_DEPTH, max_files=4096 if paired else RESULTS_MAX_FILES,
                    max_entries=RESULTS_MAX_ENTRIES, max_file_bytes=RESULTS_MAX_FILE_BYTES,
                    max_total_bytes=RESULTS_MAX_TOTAL_BYTES)
                record['failure_results_transfer'] = transfer
                (output/'remote_results_download.json').write_text(
                    json.dumps(transfer, indent=2)+'\n', encoding='utf-8')
                summaries=list((output/'results').glob('summary.json' if paired else '*/summary.json'))
                record['summary_files']=[str(p.relative_to(output)) for p in summaries]
            except Exception as pull_exc:
                record['failure_results_transfer_error'] = (
                    type(pull_exc).__name__+': '+str(pull_exc))
    finally:
        cleanup_errors = []
        if sftp:
            try: sftp.close()
            except Exception as cleanup_exc:
                cleanup_errors.append(type(cleanup_exc).__name__+': '+str(cleanup_exc))
        try: client.close()
        except Exception as cleanup_exc:
            cleanup_errors.append(type(cleanup_exc).__name__+': '+str(cleanup_exc))
        try: sock.close()
        except Exception as cleanup_exc:
            cleanup_errors.append(type(cleanup_exc).__name__+': '+str(cleanup_exc))
        if cleanup_errors:
            record['cleanup_errors'] = cleanup_errors
            if not record.get('error'):
                record['error'] = 'cleanup failure: ' + '; '.join(cleanup_errors)
        (output/'host_summary.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'evidence':str(output),'exit_code':record.get('exit_code'),'error':record.get('error')}))
    return 1 if record.get('error') or record.get('exit_code',1) else 0


if __name__=='__main__': raise SystemExit(main())
