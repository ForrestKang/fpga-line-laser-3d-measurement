"""Board side (sudo): load the live-stream bit once, measure a camera-only live run, then
start the BTN0 demo on it. Needs the user's load authority for this boot and bit.

1. identity, authority, hashes; the demo is not scanning and the motor is idle;
2. runtime_live = runtime_fast + live patch (hash-guarded), release_live;
3. stop scan3d-demo, load the bit, read back PFO3 ABI and live control (must be 0);
4. 60 s camera-only run with --live-stream (no motor), report rates;
5. start scan3d-live-demo (BTN0 standing permission), live stream only if step 4 passed.
"""
import hashlib
import json
import mmap
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import time

R = pathlib.Path('/home/xilinx/scan3d_final_timingfix_20261030_01')
S = R / 'btn0_full_scan_20261030_01'
D = R / 'deploy_live'
MACHINE = 'c82cf5bcd3a14b95935d65bfb529d72d'
PY = '/usr/local/share/pynq-venv/bin/python3'
FAST = {'scan_live.py': '0d6fedc91fef356c1784419ea9604491e7a4f99f5ba92ebdc46044fdcfc87cce',
        'page_state.py': '93324061eacaa31526401fc2f38eebe73b9bcfcca737a8dc0a09d985b79922bf'}


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def regs():
    out = {}
    fd = os.open('/dev/mem', os.O_RDONLY | os.O_SYNC)
    for name, base, dis, err in (('motor', 0x43c40000, 7, 0x38), ('guard', 0x43c50000, 5, 0x1c)):
        with mmap.mmap(fd, 4096, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ, offset=base) as mm:
            st = struct.unpack_from('<I', mm, 4)[0]
            out[name] = dict(enabled=bool(st & 1), disabled=bool(st & (1 << dis)),
                             error=struct.unpack_from('<I', mm, err)[0])
    os.close(fd)
    return out


def latest_status(data):
    files = sorted(data.glob('run_*/status.json'), key=lambda p: p.stat().st_mtime)
    return (files[-1].parent, json.loads(files[-1].read_text())) if files else (None, {})


def say(*a):
    print(*a, flush=True)


def main():
    auth = json.loads((R / 'live_trial/LOAD_AUTHORITY_LIVE.json').read_text())
    machine = pathlib.Path('/etc/machine-id').read_text().strip()
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    manifest = D / 'firmware/manifest.json'
    m = json.loads(manifest.read_text())
    bit = D / 'firmware/final.bit'
    assert machine == MACHINE == auth['machine_id'] and boot == auth['boot_id'], 'identity/boot'
    assert auth.get('load_authorized') is True and auth.get('user_statement', '').strip(), 'authority'
    assert sha(bit) == m['files']['bit']['sha256'] == auth['bit_sha256'], 'bit'
    assert sha(D / 'firmware/final.hwh') == m['files']['hwh']['sha256'], 'hwh'
    assert (m.get('live_stream') or {}).get('present'), 'manifest live_stream'

    # 2. runtime_live
    patch = json.loads((D / 'PATCH.json').read_text())
    base = S / 'runtime_fast/r33'
    for n, h in patch.items():
        assert sha(base / n) == h['base'], ('base differs', n)
        assert sha(D / 'live_patch' / n) == h['new'], ('patch differs', n)
    for n, h in FAST.items():
        assert sha(base / n) == h, ('fast differs', n)
    rt = S / 'runtime_live'
    rel = S / 'release_live'
    if not rt.exists():
        shutil.copytree(S / 'runtime_fast', rt, symlinks=True)
        for n, h in patch.items():
            shutil.copyfile(D / 'live_patch' / n, rt / 'r33' / n)
            assert sha(rt / 'r33' / n) == h['new']
        rel.mkdir()
        (rel / 'runtime').symlink_to(rt, target_is_directory=True)
    for n, h in patch.items():
        assert sha(rt / 'r33' / n) == h['new'], ('runtime_live differs', n)

    # 3. stop the demo only while idle, then load
    run, st = latest_status(R / 'demo_data')
    scan = (st.get('scan') or {}).get('state')
    assert scan not in ('SCANNING', 'HOMING', 'MOVING'), ('demo is scanning', scan)
    r0 = regs()
    assert not r0['motor']['enabled'] and not r0['guard']['enabled'], r0
    say('demo run', run and run.name, 'scan', scan, 'motor', r0)
    subprocess.run(['systemctl', 'stop', 'scan3d-demo'], timeout=150)
    for _ in range(60):
        if subprocess.run(['systemctl', 'is-active', '--quiet', 'scan3d-demo']).returncode:
            break
        time.sleep(2)
    assert subprocess.run(['systemctl', 'is-active', '--quiet', 'scan3d-demo']).returncode, 'demo still active'
    load = ("import json,sys\nfrom pynq import Overlay,MMIO,PL\n"
            "o=Overlay(sys.argv[1],download=True)\n"
            "f=MMIO(o.ip_dict['pfo3_frontend']['phys_addr'],4096)\n"
            "print(json.dumps(dict(bit=PL.bitfile_name,regs={hex(x):f.read(x) for x in (0,4,8,0x14,0x24,0x28,0x2c)})))\n")
    out = subprocess.run([PY, '-B', '-c', load, str(bit)], capture_output=True, text=True, timeout=180,
                         env=dict(os.environ, XILINX_XRT='/usr'))
    assert out.returncode == 0, out.stderr[-2000:]
    loaded = json.loads(out.stdout.strip().splitlines()[-1])
    say('loaded', json.dumps(loaded))
    assert loaded['regs']['0x0'] == 0x57334650 and loaded['regs']['0x24'] == 0, 'PFO3 readback'

    # 4. camera-only live run
    sys.path.insert(0, str(R / 'live_trial'))
    import demo_service
    data = S / 'live_demo_data'
    data.mkdir(exist_ok=True)
    trial = data / time.strftime('trial_%Y%m%d_%H%M%S')
    cmd = demo_service.live_command(PY, rel, manifest, trial, 60, None, True)
    env = dict(os.environ, XILINX_XRT='/usr', PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(rel / 'runtime'))
    with (data / (trial.name + '.log')).open('w') as console:
        code = subprocess.run(cmd, cwd=rel, env=env, stdout=console, stderr=subprocess.STDOUT, timeout=240).returncode
    s = {}
    for name in ('summary.json', 'status.json'):
        if (trial / name).exists():
            s = json.loads((trial / name).read_text())
            break
    ls = s.get('live_stream') or {}
    elapsed = ls.get('elapsed_s') or s.get('elapsed_s') or 0
    passed = code == 0 and not s.get('error') and ls.get('packets', 0) > 0 and not ls.get('errors')
    say('trial', trial.name, 'exit', code, 'error', s.get('error'), 'stop', s.get('stop_reason'))
    say('arm', {k: s.get(k) for k in ('frames', 'fps', 'display_median_ms')})
    say('pl', ls, 'pl_fps', round(ls.get('packets', 0) / elapsed, 1) if elapsed else None)
    say('last', {k: (s.get('last') or {}).get(k) for k in ('capture_ms', 'pl_ms', 'xyz', 'xyz_measurement')})
    if not passed:
        say('LOG_TAIL', (data / (trial.name + '.log')).read_text()[-3000:])

    # 5. BTN0 demo on the live bit
    unit = 'scan3d-live-demo'
    cmd = ['systemd-run', '--unit=' + unit, '--property=KillMode=mixed', '--property=TimeoutStopSec=90',
           '--setenv=XILINX_XRT=/usr', PY, '-u', '-B', str(R / 'live_trial/demo_service.py'),
           '--release', str(rel), '--manifest', str(manifest), '--data', str(data),
           '--motor-standing', str(R / 'boot_autostart_btn0/motor_standing.json')] + (['--live-stream'] if passed else [])
    subprocess.run(cmd, check=True, timeout=60)
    time.sleep(60)
    run, st = latest_status(data)
    say(unit, subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True).stdout.strip(),
        'live_stream', passed, 'run', run and run.name, {k: st.get(k) for k in ('phase', 'error', 'frames', 'fps')},
        'scan', (st.get('scan') or {}).get('state'))
    say('LIVE_TRIAL_PASS' if passed else 'LIVE_TRIAL_FAIL')


if __name__ == '__main__':
    main()
