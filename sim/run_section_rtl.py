"""Run the unchanged section RTL oracle with bundled full-size board inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

RUNS = ['r37_r41b_live01', 'r37_r41b_live05', 'r37_r41b_live06', 'r37_r41b_live07',
        'r37_r41c_live01', 'r37_r41f_live02', 'r37_r41f_live03', 'r37_r41g_live01', 'r37_r41g_live02']


def input_identity(work, board):
    root, board = Path(work).resolve() / 'fpga', Path(board).resolve()
    inputs, raw_count = [], 0
    for name in RUNS:
        folder = board / name
        raws = sorted(folder.glob('raw_*.gray8'))
        if not raws:
            raise ValueError('No Raw frames: ' + str(folder))
        for file in [folder / 'calibration_live.json', folder / 'status.json', *raws]:
            data = file.read_bytes()
            if file.suffix == '.gray8':
                if len(data) != 752 * 480:
                    raise ValueError('Wrong Raw dimensions: ' + str(file))
                raw_count += 1
            else:
                json.loads(data)
            inputs.append(dict(path=file.relative_to(board).as_posix(), size=len(data),
                               sha256=hashlib.sha256(data).hexdigest()))
    if raw_count != 182:
        raise ValueError('Expected all 182 historical frames, got ' + str(raw_count))
    sources = []
    for name in ['rtl/section_measure.sv', 'tests/tb_section_measure.sv', 'model/section_measure_model.py',
                 'scripts/section_rtl_verify.py', 'scripts/section_model_compare.py',
                 'scripts/render_ui_mockups.py', 'runtime/r33/profile_live.py']:
        data = (root / name).read_bytes()
        sources.append(dict(path=name, size=len(data), sha256=hashlib.sha256(data).hexdigest()))
    content = dict(inputs=inputs, sources=sources)
    return dict(schema='SECTION_RTL_INPUT_IDENTITY_V1', raw_frames=raw_count, synthetic_packets=13,
                expected_packets=195, content_sha256=hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest(),
                **content)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--work', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--board', type=Path, help='Optional explicit read-only input directory')
    ap.add_argument('--iverilog', default='iverilog')
    ap.add_argument('--vvp', default='vvp')
    ap.add_argument('--workers', type=int, choices=(1, 2), default=2)
    ap.add_argument('--validate-inputs-only', action='store_true')
    a = ap.parse_args()
    root = a.work.resolve() / 'fpga'
    board = (a.board or root / 'verification/section/board').resolve()
    identity = input_identity(a.work, board)
    a.output.mkdir(parents=True, exist_ok=False)
    (a.output / 'input_identity.json').write_text(json.dumps(identity, indent=2), encoding='utf-8')
    if a.validate_inputs_only:
        print(json.dumps(dict(passed=True, content_sha256=identity['content_sha256'], expected_packets=195)))
        return 0
    iverilog, vvp = shutil.which(a.iverilog), shutil.which(a.vvp)
    if not iverilog or not vvp:
        raise ValueError('Install Icarus Verilog and vvp, or provide explicit paths')
    source = (root / 'scripts/section_rtl_verify.py').read_text(encoding='utf-8')
    for name, path in [('iverilog', iverilog), ('vvp', vvp)]:
        old = "IVER / '" + name + ".exe'"
        if old not in source:
            raise ValueError('Unexpected frozen simulator invocation: ' + old)
        source = source.replace(old, 'Path(' + repr(str(path)) + ')')
    marker = "if __name__ == '__main__':"
    if source.count(marker) != 1:
        raise ValueError('Unexpected main guard')
    source = source.replace(marker, 'cmp.m.BOARD = Path(' + repr(str(board)) + ')\n\n' + marker)
    token = hashlib.sha256(str(a.output.resolve()).encode()).hexdigest()[:12]
    adapted = root / ('scripts/section_submission_' + token + '.py')
    with adapted.open('x', encoding='utf-8') as stream:
        stream.write(source)
    temp = a.output.resolve() / 'temp'
    temp.mkdir()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', TEMP=str(temp), TMP=str(temp))
    with (a.output / 'console.log').open('x', encoding='utf-8') as log:
        result = subprocess.run([sys.executable, '-B', str(adapted), '--work-dir',
                                 str(a.output.resolve() / 'runs'), '--report', str(a.output.resolve() / 'result.json'),
                                 '--workers', str(a.workers)], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        return result.returncode
    report = json.loads((a.output / 'result.json').read_text(encoding='utf-8'))
    if not report['passed'] or report['packets'] != 195:
        return 1
    print('PASS 195 packets; input identity:', identity['content_sha256'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
