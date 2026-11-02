"""Read-only board inspection (no load, no capture, no motion, no write).

Pinned SSH host key, explicit source bind on 192.168.2.100, one attempt.
Writes only a local receipt; the board is never modified.
"""
from __future__ import annotations
import argparse, hashlib, json, os, socket, sys, time
from pathlib import Path

PINNED_SHA256 = '4c1d057c68df33381952ffeb789813a5eabf00cebcb0c0a9182e719ba3e2b7ba'
READONLY = [
    ('hostname', 'hostname'),
    ('machine_id', 'cat /etc/machine-id'),
    ('boot_id', 'cat /proc/sys/kernel/random/boot_id'),
    ('uname', 'uname -a'),
    ('os_release', 'head -3 /etc/os-release'),
    ('uptime_s', 'cut -d. -f1 /proc/uptime'),
    ('mem_available_kb', "grep MemAvailable /proc/meminfo"),
    ('cma', "grep -E 'CmaTotal|CmaFree' /proc/meminfo"),
    ('disk_home', 'df -B1 /home'),
    ('disk_root', 'df -B1 /'),
    ('scan3d_processes', "ps -eo pid,etime,cmd | grep -Ei 'scan3d|hdmi_owner|continuous_scan|pl_replay' | grep -v grep"),
    ('hardware_locks', "ls -la /tmp/*.lock /run/*.lock 2>/dev/null | head -20"),
    ('data_dirs', 'ls -la /home/xilinx 2>/dev/null | head -20'),
    ('loaded_bit_search', "ls -la /home/xilinx/*.bit /home/xilinx/*/*.bit 2>/dev/null | head -10"),
]


class Pin:
    def __init__(self, expected):
        self.expected = expected
    def missing_host_key(self, client, hostname, key):  # paramiko policy interface
        digest = hashlib.sha256(key.asbytes()).hexdigest()
        if digest != self.expected:
            raise RuntimeError('PYNQ SSH host key mismatch')


def connect(host, user, password, source, timeout=15):
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(Pin(PINNED_SHA256))
    sock = socket.socket()
    sock.settimeout(6)
    # Per-socket NIC selection; no route, VPN or firewall configuration changes.
    interface=os.environ.get('PYNQ_SOURCE_IFINDEX')
    if os.name=='nt' and interface:
        index=int(interface)
        if not 1<=index<2**31:raise ValueError('invalid source interface index')
        sock.setsockopt(socket.IPPROTO_IP,31,socket.htonl(index))
    if source:
        sock.bind((source, 0))
    sock.connect((host, 22))
    client.connect(host, username=user, password=password, sock=sock, look_for_keys=False,
                   allow_agent=False, timeout=timeout, banner_timeout=timeout, auth_timeout=timeout)
    digest = hashlib.sha256(client.get_transport().get_remote_server_key().asbytes()).hexdigest()
    if digest != PINNED_SHA256:
        client.close()
        raise RuntimeError('PYNQ SSH key changed: ' + digest)
    return client, digest


def run(client, command, timeout=20):
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode('utf-8', 'replace')
    err = stderr.read().decode('utf-8', 'replace')
    return dict(command=command, out=out.strip()[:4000], err=err.strip()[:800],
                exit=stdout.channel.recv_exit_status())


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--host', default='192.168.2.99')
    p.add_argument('--user', default='xilinx')
    p.add_argument('--source', default='192.168.2.100')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    password = os.environ.get('PYNQ_PASSWORD')
    if not password:
        raise SystemExit('PYNQ_PASSWORD is not set; refusing to guess a board password')
    started = time.time()
    client, digest = connect(a.host, a.user, password, a.source)
    try:
        results = {name: run(client, cmd) for name, cmd in READONLY}
    finally:
        client.close()
    receipt = dict(schema='R26_BOARD_READONLY_V1', host=a.host, user=a.user, source_bind=a.source,
                   ssh_host_key_sha256=digest, pinned_key_match=True,
                   hardware_modified=False, fpga_loaded=False, camera_commands=0, motor_commands=0,
                   wall_s=time.time() - started, results=results)
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x', encoding='utf-8') as f:
        json.dump(receipt, f, indent=2)
        f.flush(); os.fsync(f.fileno())
    for name, row in results.items():
        first = (row['out'] or row['err']).splitlines()
        print('%-20s %s' % (name, first[0] if first else '(empty)'))
    print('WROTE', out)


if __name__ == '__main__':
    main()
