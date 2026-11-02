"""Board side (sudo): load the timing bit once, measure a camera-only live run, then
restart the BTN0 demo on it. Needs the user's load authority for this boot and bit.

1. identity, authority, hashes; the demo is not scanning and the motor is idle;
2. runtime_timing = runtime_fast + timing patch (hash-guarded), release_timing;
3. stop the idle demo, load the bit, read back PFO3 ABI and live control (idle);
4. 60 s camera-only run with --live-stream (no motor), report rates and the PL view;
5. restart scan3d-live-demo (BTN0 standing permission), live stream only if step 4 passed.
"""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

from live_trial import PY, MACHINE, R, S, latest_status, regs, say
from live_trial2 import pfo3_live_ctrl, restart

HERE = pathlib.Path(__file__).resolve().parent
D = R / 'deploy_timing'


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def stop_unit(unit):
    subprocess.run(['systemctl', 'stop', unit], timeout=150)
    for _ in range(60):
        if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode:
            return
        time.sleep(2)
    raise AssertionError(unit + ' still active')


def main():
    auth = json.loads((HERE / 'LOAD_AUTHORITY_TIMING.json').read_text())
    machine = pathlib.Path('/etc/machine-id').read_text().strip()
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    manifest = D / 'firmware/manifest.json'
    m = json.loads(manifest.read_text())
    bit = D / 'firmware/final.bit'
    assert machine == MACHINE == auth['machine_id'] and boot == auth['boot_id'], 'identity/boot'
    assert auth.get('load_authorized') is True and auth.get('user_statement', '').strip(), 'authority'
    assert sha(bit) == m['files']['bit']['sha256'] == auth['bit_sha256'], 'bit'
    assert sha(D / 'firmware/final.hwh') == m['files']['hwh']['sha256'], 'hwh'
    ls_decl = m.get('live_stream') or {}
    assert ls_decl.get('present') and ls_decl.get('early_booking') is False, 'manifest live_stream'

    # 2. runtime_timing
    # patch keys are paths under runtime/; base None marks a file runtime_fast does not have
    patch = json.loads((D / 'PATCH.json').read_text())
    base = S / 'runtime_fast'
    for n, h in patch.items():
        if h['base'] is None:
            assert not (base / n).exists(), ('unexpected base file', n)
        else:
            assert sha(base / n) == h['base'], ('base differs', n)
        assert sha(D / 'live_patch' / n) == h['new'], ('patch differs', n)
    rt, rel = S / 'runtime_timing', S / 'release_timing'
    if not rt.exists():
        shutil.copytree(base, rt, symlinks=True)
        for n in patch:
            assert not (rt / n).is_symlink(), ('patch target is a link', n)
            shutil.copyfile(D / 'live_patch' / n, rt / n)
        rel.mkdir()
        (rel / 'runtime').symlink_to(rt, target_is_directory=True)
    for n, h in patch.items():
        assert sha(rt / n) == h['new'], ('runtime_timing differs', n)

    # 3. stop the demo only while idle, then load
    unit, data = 'scan3d-live-demo', S / 'live_demo_data'
    # after a reboot /boot starts scan3d-demo; otherwise scan3d-live-demo may be running
    for name, folder in (('scan3d-demo', R / 'demo_data'), (unit, data)):
        if subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode:
            continue
        run, st = latest_status(folder)
        scan = (st.get('scan') or {}).get('state')
        assert scan not in ('SCANNING', 'HOMING', 'MOVING'), ('demo is scanning', name, scan)
        r0 = regs()
        assert not r0['motor']['enabled'] and not r0['guard']['enabled'], r0
        say('stop', name, 'run', run and run.name, 'scan', scan, 'motor', r0)
        stop_unit(name)
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
    sys.path.insert(0, str(HERE))
    import demo_service
    trial = data / time.strftime('trial_%Y%m%d_%H%M%S')
    cmd = demo_service.live_command(PY, rel, manifest, trial, 60, None, True)
    assert '--allow-motor' not in cmd and '--scan-start-request' not in cmd
    env = dict(os.environ, XILINX_XRT='/usr', PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=str(rel / 'runtime'))
    c0 = pfo3_live_ctrl()
    with (data / (trial.name + '.log')).open('w') as console:
        code = subprocess.run(cmd, cwd=rel, env=env, stdout=console, stderr=subprocess.STDOUT, timeout=300).returncode
    c1 = pfo3_live_ctrl()
    s = {}
    for name in ('summary.json', 'status.json'):
        if (trial / name).exists():
            s = json.loads((trial / name).read_text())
            break
    ls = s.get('live_stream') or {}
    elapsed = ls.get('elapsed_s') or 0
    passed = code == 0 and not s.get('error') and ls.get('packets', 0) > 0 and not ls.get('errors')
    say('trial', trial.name, 'exit', code, 'error', s.get('error'), 'stop', s.get('stop_reason'))
    say('arm', {k: s.get(k) for k in ('frames', 'fps', 'display_median_ms', 'phase')})
    say('timing_median_ms', s.get('timing_median_ms'))
    say('pl', ls, 'pl_fps', round(ls.get('packets', 0) / elapsed, 1) if elapsed else None)
    say('pfo3 ctrl/packets/errors before', [hex(c0[1])] + c0[2:], 'after', [hex(c1[1])] + c1[2:])
    say('pl_view', s.get('pl_view'))
    if not passed:
        say('LOG_TAIL', (data / (trial.name + '.log')).read_text()[-4000:])

    # 5. BTN0 demo on the timing bit
    restart(unit, rel, manifest, data, passed)
    time.sleep(60)
    run, st = latest_status(data)
    say(unit, subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True).stdout.strip(),
        'live_stream', passed, 'run', run and run.name, {k: st.get(k) for k in ('phase', 'error', 'frames', 'fps')},
        'scan', (st.get('scan') or {}).get('state'), 'pl', st.get('live_stream'))
    say('TIMING_TRIAL_PASS' if passed else 'TIMING_TRIAL_FAIL')


if __name__ == '__main__':
    main()
