"""Read a live HDMI acceptance session; record only an explicit operator reply.

No Overlay, MMIO, camera, or DMA operation is performed by this host helper.
The run configuration binds the remote directory; SSH identity is always pinned.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import socket

from deploy_gray_candidate import (HOST_KEY, execute, load_identity_contract,
    parse_remote_identity, remote_identity_command, validate_remote_identity,
    validate_ssh_host_key_sha256)


def remote_root_from_config(config):
    bitfile = PurePosixPath(config['bitfile'])
    parts = bitfile.parts
    if len(parts) != 8 or parts[:3] != ('/', 'home', 'xilinx'):
        raise ValueError('unexpected deployment directory')
    if not parts[3].startswith('gray_hdmi_') or parts[4:7] != ('board', 'overlays', 'structured_light_profile_hdmi'):
        raise ValueError('configuration is not a deployed combined Overlay')
    if parts[7] != 'structured_light_profile_hdmi.bit':
        raise ValueError('unexpected bitfile name')
    return str(PurePosixPath(*parts[:4]))


def confirmation(status, phase, statement):
    if phase not in {'test_pattern', 'scene_change', 'final_visual'}:
        raise ValueError('unknown operator phase')
    if status.get('phase') != phase:
        raise ValueError('live session is not waiting for this phase')
    nonce = status.get('nonce')
    if not isinstance(nonce, str) or len(nonce) != 32 or any(c not in '0123456789abcdef' for c in nonce):
        raise ValueError('invalid live session nonce')
    if not statement or not statement.strip():
        raise ValueError('explicit operator statement is required')
    return {'nonce': nonce, 'phase': phase, 'approved': True,
            'user_statement': statement, 'recorded_host_utc': datetime.now(timezone.utc).isoformat()}


def main():
    import paramiko
    import yaml
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--confirm', choices=['test_pattern', 'scene_change', 'final_visual'])
    parser.add_argument('--user-statement')
    args = parser.parse_args()
    if bool(args.confirm) != bool(args.user_statement):
        parser.error('--confirm requires the actual --user-statement')
    config = yaml.safe_load(args.config.read_text(encoding='utf-8'))
    remote = remote_root_from_config(config)
    password = os.environ.get('PYNQ_PASSWORD')
    if not password:
        raise RuntimeError('PYNQ_PASSWORD environment variable is required')
    contract = load_identity_contract()
    client, sock = paramiko.SSHClient(), socket.socket()
    class Pinned(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, _client, _hostname, key):
            if hashlib.sha256(key.asbytes()).hexdigest() != HOST_KEY:
                raise RuntimeError('target SSH identity changed')
    client.set_missing_host_key_policy(Pinned())
    sftp = None
    try:
        sock.settimeout(5); sock.bind(('192.168.2.100', 0)); sock.connect(('192.168.2.99', 22))
        client.connect('192.168.2.99', username='xilinx', password=password, sock=sock,
                       look_for_keys=False, allow_agent=False, timeout=5, auth_timeout=5, banner_timeout=5)
        key = client.get_transport().get_remote_server_key()
        validate_ssh_host_key_sha256(hashlib.sha256(key.asbytes()).hexdigest(), contract)
        rc, out, err = execute(client, remote_identity_command(), password=password, timeout_s=10)
        if rc: raise RuntimeError('identity read failed: '+err)
        validate_remote_identity(parse_remote_identity(out), contract)
        sftp = client.open_sftp(); sftp.get_channel().settimeout(3)
        with sftp.open(remote+'/results/live_status.json', 'rb') as stream:
            status = json.loads(stream.read(2*1024*1024))
        if args.confirm:
            payload = confirmation(status, args.confirm, args.user_statement)
            target = remote+'/results/'+args.confirm+'_confirmation.json'
            # Exclusive creation rejects duplicate confirmations. The receiver
            # binds the nonce again before advancing its capture sequence.
            temporary = target+'.'+payload['nonce']+'.tmp'
            with sftp.open(temporary, 'wx') as stream:
                stream.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
            sftp.rename(temporary, target)  # standard rename refuses existing target
            local = args.config.parent/(args.confirm+'_operator_confirmation.json')
            local.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(status, ensure_ascii=False, indent=2))
    finally:
        if sftp: sftp.close()
        client.close(); sock.close()


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    main()
