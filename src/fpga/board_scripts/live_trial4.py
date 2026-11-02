"""Board side (sudo): recover the PFO3 RX DMA left armed by the failed grab, install the
fixed runtime (runtime_live2) and rerun the camera-only live trial. No FPGA load.
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

from live_trial import PY, D, S, latest_status, regs, say
from live_trial2 import pfo3_live_ctrl, restart

HERE = pathlib.Path(__file__).resolve().parent


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def reset_pfo3_rx():
    """S2MM soft reset; only when the PL owes the DMA nothing (no dump/copy pending)."""
    fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
    with mmap.mmap(fd, 4096, offset=0x40000000) as ctrl, mmap.mmap(fd, 4096, offset=0x40430000) as dma:
        rd = lambda mm, o: struct.unpack_from('<I', mm, o)[0]
        before = dict(live=rd(ctrl, 0x24), busy=rd(ctrl, 0x14), s2mm_cr=rd(dma, 0x30), s2mm_sr=rd(dma, 0x34))
        if before['s2mm_sr'] & 1:
            os.close(fd)
            return before, 'already halted'
        assert before['live'] & 7 == 0 and not before['busy'] & 1, ('PFO3 not idle', before)
        struct.pack_into('<I', dma, 0x30, 4)
        for _ in range(500):
            if not rd(dma, 0x30) & 4 and rd(dma, 0x34) & 1:
                break
            time.sleep(.001)
        after = dict(s2mm_cr=rd(dma, 0x30), s2mm_sr=rd(dma, 0x34), mm2s_sr=rd(dma, 0x04))
    os.close(fd)
    assert after['s2mm_sr'] & 1, ('reset not confirmed', after)
    return before, after


def main():
    unit = 'scan3d-live-demo'
    manifest, data = D / 'firmware/manifest.json', S / 'live_demo_data'
    run, st = latest_status(data)
    scan = (st.get('scan') or {}).get('state')
    assert scan not in ('SCANNING', 'HOMING', 'MOVING'), ('demo is scanning', scan)
    r0 = regs()
    assert not r0['motor']['enabled'] and not r0['guard']['enabled'], r0
    subprocess.run(['systemctl', 'stop', unit], timeout=150)
    for _ in range(60):
        if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode:
            break
        time.sleep(2)
    assert subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode, 'demo still active'
    say('rx reset', reset_pfo3_rx())

    patch = json.loads((HERE / 'PATCH.json').read_text())
    base = S / 'runtime_fast/r33'
    for n, h in patch.items():
        assert sha(base / n) == h['base'], ('base differs', n)
        assert sha(HERE / 'live_patch' / n) == h['new'], ('patch differs', n)
    rt, rel = S / 'runtime_live2', S / 'release_live2'
    if not rt.exists():
        shutil.copytree(S / 'runtime_fast', rt, symlinks=True)
        for n in patch:
            shutil.copyfile(HERE / 'live_patch' / n, rt / 'r33' / n)
        rel.mkdir()
        (rel / 'runtime').symlink_to(rt, target_is_directory=True)
    for n, h in patch.items():
        assert sha(rt / 'r33' / n) == h['new'], ('runtime_live2 differs', n)

    sys.path.insert(0, str(HERE))
    import demo_service
    trial = data / time.strftime('trial_%Y%m%d_%H%M%S')
    cmd = demo_service.live_command(PY, rel, manifest, trial, 60, None, True)
    assert '--allow-motor' not in cmd and '--scan-start-request' not in cmd
    env = dict(os.environ, XILINX_XRT='/usr', PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(rel / 'runtime'))
    c0 = pfo3_live_ctrl()
    t0 = time.monotonic()
    with (data / (trial.name + '.log')).open('w') as console:
        code = subprocess.run(cmd, cwd=rel, env=env, stdout=console, stderr=subprocess.STDOUT, timeout=300).returncode
    wall = time.monotonic() - t0
    c1 = pfo3_live_ctrl()
    s = {}
    for name in ('summary.json', 'status.json'):
        if (trial / name).exists():
            s = json.loads((trial / name).read_text())
            break
    ls = s.get('live_stream') or {}
    elapsed = ls.get('elapsed_s') or 0
    passed = code == 0 and not s.get('error') and ls.get('packets', 0) > 0 and not ls.get('errors')
    say('trial', trial.name, 'exit', code, 'wall_s', round(wall, 1), 'error', s.get('error'), 'stop', s.get('stop_reason'))
    say('cleanup', s.get('cleanup_errors'))
    say('arm', {k: s.get(k) for k in ('frames', 'fps', 'display_median_ms', 'phase')})
    say('timing_median_ms', s.get('timing_median_ms'))
    say('pl', ls, 'pl_fps', round(ls.get('packets', 0) / elapsed, 1) if elapsed else None)
    say('pfo3 live/packets/errors before', c0[1:], 'after', c1[1:])
    say('last', {k: (s.get('last') or {}).get(k) for k in ('capture_ms', 'pl_ms', 'xyz', 'xyz_measurement')})
    if not passed:
        say('LOG_TAIL', (data / (trial.name + '.log')).read_text()[-4000:])
        say('rx after', reset_pfo3_rx())

    restart(unit, rel, manifest, data, passed)
    time.sleep(60)
    run, st = latest_status(data)
    say(unit, subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True).stdout.strip(),
        'live_stream', passed, 'run', run and run.name, {k: st.get(k) for k in ('phase', 'error', 'frames', 'fps')},
        'scan', (st.get('scan') or {}).get('state'), 'pl', st.get('live_stream'))
    say('LIVE_TRIAL_PASS' if passed else 'LIVE_TRIAL_FAIL')


if __name__ == '__main__':
    main()
