"""Board side (sudo): load the seed-retry bit once and compare the two decode modes on the
static object, camera only (no motor command).

1. identity, load authority, bit/HWH hashes; no demo or live run is active, motor idle;
2. runtime_seedretry = runtime_final + runtime_patch (hash-guarded), release_seedretry;
3. load the bit, read back PFO3 ABI and live control (idle);
4. the seed-retry contract test with the board's native assembler;
5. 45 s live runs: A full width + seed retry (manifest.json), B 2x binning, single seed
   (manifest_bin.json: same bit, policy 2); per-frame centres / points / measurement points.
The demo is not restarted here; /boot is installed separately.
"""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import numpy as np

from live_trial import PY, MACHINE, R, S, latest_status, regs, say
from live_trial2 import pfo3_live_ctrl

HERE = pathlib.Path(__file__).resolve().parent
D = R / 'deploy_seedretry'
CALIBRATION = {
    'camera_intrinsics_r41.json': 'e73e37f535f9029fb1b6f3a10c9a0bc97ccd8d58fdd456097137d358f927972a',
    'laser_plane_r41.json': '12c9c9935e82aa31e4c6748ee0b98938af36f17071c66427bb9559e95953ef23',
    'turntable_axis.json': 'f13b4f32af9372d0fd994a96a72cf69f2d6b08b097e272820fdfc075cf70f725',
}
SINGLE_SEED_FINGERPRINT = 'e7373198d746ed1992f3eaa4295837f442c2ed70a2e599206f8866ff25dcb04f'  # r33.protocol.FINGERPRINT


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def trial(name, rel, manifest, full_width, data):
    sys.path.insert(0, str(HERE))
    import demo_service
    out = data / (time.strftime('trial_%Y%m%d_%H%M%S_') + name)
    cmd = demo_service.live_command(PY, rel, manifest, out, 45, None, True, full_width)
    assert '--allow-motor' not in cmd and '--scan-start-request' not in cmd
    env = dict(os.environ, XILINX_XRT='/usr', PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(rel / 'runtime'))
    c0 = pfo3_live_ctrl()
    with (data / (out.name + '.log')).open('w') as console:
        code = subprocess.run(cmd, cwd=rel, env=env, stdout=console, stderr=subprocess.STDOUT, timeout=300).returncode
    c1 = pfo3_live_ctrl()
    s = json.loads((out / 'summary.json').read_text()) if (out / 'summary.json').exists() else {}
    ls = s.get('live_stream') or {}
    elapsed = ls.get('elapsed_s') or 0
    frames = json.loads((out / 'frames.json').read_text()) if (out / 'frames.json').exists() else []
    col = {k: np.array([f.get(k) or 0 for f in frames], float) for k in ('centres', 'xyz', 'xyz_measurement')}
    result = dict(name=name, run=out.name, exit=code, error=s.get('error'), frames=len(frames), fps=s.get('fps'),
                  pl_fps=round(ls.get('packets', 0) / elapsed, 1) if elapsed else None,
                  pl_errors=ls.get('errors'), bin2=full_width is False,
                  median={k: float(np.median(v)) if len(v) else None for k, v in col.items()},
                  p10={k: float(np.percentile(v, 10)) if len(v) else None for k, v in col.items()},
                  timing_median_ms=s.get('timing_median_ms'),
                  ctrl_before=[hex(c0[1])] + c0[2:], ctrl_after=[hex(c1[1])] + c1[2:])
    say('TRIAL', json.dumps(result))
    if code or s.get('error'):
        say('LOG_TAIL', (data / (out.name + '.log')).read_text()[-3000:])
    return result


def main():
    auth = json.loads((HERE / 'LOAD_AUTHORITY_SEEDRETRY.json').read_text())
    machine = pathlib.Path('/etc/machine-id').read_text().strip()
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    manifest = D / 'firmware/manifest.json'
    m = json.loads(manifest.read_text())
    bit = D / 'firmware/final.bit'
    assert machine == MACHINE == auth['machine_id'] and boot == auth['boot_id'], 'identity/boot'
    assert auth.get('load_authorized') is True and auth.get('user_statement', '').strip(), 'authority'
    assert sha(bit) == m['files']['bit']['sha256'] == auth['bit_sha256'], 'bit'
    assert sha(D / 'firmware/final.hwh') == m['files']['hwh']['sha256'], 'hwh'
    assert (m.get('live_stream') or {}).get('present') and m['pfo3'].get('same_run_seed_retry'), 'manifest'

    # 2. runtime_seedretry
    expected = json.loads((D / 'EXPECTED.json').read_text())
    base, rt, rel = S / 'runtime_final', S / 'runtime_seedretry', S / 'release_seedretry'
    for n, h in expected['base'].items():
        assert sha(base / n) == h, ('runtime_final differs', n)
    for n in expected['absent']:
        assert not (base / n).exists(), ('unexpected base file', n)
    for n, h in expected['new'].items():
        assert sha(D / 'runtime_patch' / n) == h, ('upload differs', n)
    if not rt.exists():
        shutil.copytree(base, rt, symlinks=True)
        for n in expected['new']:
            assert not (rt / n).is_symlink(), ('patch target is a link', n)
            shutil.copyfile(D / 'runtime_patch' / n, rt / n)
        rel.mkdir()
        (rel / 'runtime').symlink_to(rt, target_is_directory=True)
    for n, h in expected['new'].items():
        assert sha(rt / n) == h, ('runtime_seedretry differs', n)
    # the solidified T02-rotation calibration (plane d -80.755, axis, 3547 steps/turn) carries over
    cal = {n: sha(rt / 'calibration_r41' / n) for n in CALIBRATION}
    assert cal == CALIBRATION, ('calibration differs', cal)
    say('calibration', cal)

    # binning manifest: same bit, single seed (policy 2)
    mb = json.loads(manifest.read_text())
    mb['pfo3'].update(same_run_seed_retry=False, policy_revision=1, parameter_fingerprint=SINGLE_SEED_FINGERPRINT)
    manifest_bin = D / 'firmware/manifest_bin.json'
    manifest_bin.write_text(json.dumps(mb, indent=2))

    # 3. nothing may be running; then load
    for name in ('scan3d-demo', 'scan3d-live-demo', 'scan3d-calib'):
        assert subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode, (name, 'active')
    ps = subprocess.run(['pgrep', '-f', 'r33[.]live'], capture_output=True, text=True).stdout.split()
    assert not ps, ('live process running', ps)
    r0 = regs()
    assert not r0['motor']['enabled'] and not r0['guard']['enabled'], r0
    say('idle', r0)
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

    # 4. contract test with the ARM native assembler
    t = subprocess.run([PY, '-B', '-m', 'unittest', 'r33.tests_seed_retry_contract'], cwd=rt,
                       capture_output=True, text=True, timeout=300, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    say('CONTRACT_TEST', t.returncode, t.stderr.strip().splitlines()[-1:] if t.stderr else '')

    # 5. A/B
    data = S / 'live_demo_data'
    a = trial('seedretry_full', rel, manifest, True, data)
    b = trial('bin2_single', rel, manifest_bin, False, data)
    say('COMPARE', json.dumps(dict(seed_retry=a, bin2=b)))
    say('SEEDRETRY_TRIAL_DONE')


if __name__ == '__main__':
    main()
