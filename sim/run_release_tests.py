"""Build host-native helpers and run every packaged r33 test module offline."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--work', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--cc', default=os.environ.get('CC', 'gcc'))
    a = ap.parse_args()
    work = a.work.resolve()
    runtime = work / 'runtime'
    output = a.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    temp = output / 'temp'
    temp.mkdir()
    native = output / 'native'
    native.mkdir()
    cc = shutil.which(a.cc)
    if cc is None:
        raise SystemExit('A host C compiler is required; pass --cc or install gcc')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
               TEMP=str(temp), TMP=str(temp))
    paths = [runtime, runtime / 'structured_light_system_algorithm_v1/src/python',
             runtime / 'structured_light_system_algorithm_v1/board/scripts',
             runtime / 'structured_light_system_geometry_v1/src/python',
             work / 'fpga/model', work / 'fpga/scripts']
    env['PYTHONPATH'] = os.pathsep.join(map(str, paths)) + os.pathsep + env.get('PYTHONPATH', '')
    env.update(R33_TEST_FIXTURES=str(work / 'pl/direction_tests/evidence04'),
               R33_TEST_PIXELS=str(work / 'fpga/vendor/verification/sim/T05_000.hex'),
               R33_TEST_CALIBRATION=str(work / 'fpga/vendor/verification/calibrations/T05/calibration.json'),
               R34_STORAGE_BASELINE=str(work / 'test_fixtures/source_baseline'),
               R33_TEST_TEMP=str(temp))
    suffix = '.dll' if os.name == 'nt' else '.so'
    assembler = native / ('assembler' + suffix)
    with (output / 'native_build.log').open('x', encoding='utf-8') as log:
        subprocess.run([sys.executable, '-B', str(runtime / 'r33/build_native.py'),
                        '--output', str(assembler), '--cc', cc], cwd=runtime,
                       env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        markers = native / ('markers' + suffix)
        source = runtime / 'r33/native/markers_r34.c'
        flags = ['-O3', '-std=c99', '-Wall', '-Wextra', '-Werror', '-shared']
        if os.name != 'nt':
            flags.append('-fPIC')
        subprocess.run([cc, *flags, str(source), '-o', str(markers)], env=env,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    receipt = dict(schema='R34_NATIVE_MARKER_BUILD_V1',
                   source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                   output_sha256=hashlib.sha256(markers.read_bytes()).hexdigest(),
                   output_bytes=markers.stat().st_size, machine=platform.machine(),
                   compiler=cc, flags=flags, display_only=True)
    Path(str(markers) + '.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    env['R33_NATIVE_ASSEMBLER'] = str(assembler)
    env['SCAN3D_R34_MARKERS_LIBRARY'] = str(markers)
    modules = ['r33.' + f.stem for f in sorted((runtime / 'r33').glob('tests*.py'))]
    if not modules:
        raise RuntimeError('No runtime tests found')
    config = output / 'test_configuration.json'
    config.write_text(json.dumps(dict(modules=modules, runtime=str(runtime),
        native_helpers=[str(assembler), str(markers)], compiler=cc,
        fixture_paths={k: v for k, v in env.items() if k.startswith(('R33_TEST_', 'R34_STORAGE_'))}),
        indent=2), encoding='utf-8')
    worker = output / 'run_suite.py'
    worker.write_text('''import json,sys,time,unittest
from pathlib import Path
cfg=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
suite=unittest.defaultTestLoader.loadTestsFromNames(cfg["modules"])
start=time.perf_counter()
result=unittest.TextTestRunner(verbosity=2).run(suite)
report=dict(schema="OFFLINE_RUNTIME_RELEASE_SUITE_V1", modules=cfg["modules"],
    tests_run=result.testsRun, failures=[(str(t),d) for t,d in result.failures],
    errors=[(str(t),d) for t,d in result.errors], skipped=[(str(t),d) for t,d in result.skipped],
    passed=result.wasSuccessful(), duration_s=time.perf_counter()-start,
    evidence_class="PC_MOCK_NATIVE_AND_FROZEN_FIXTURES_NOT_BOARD", hardware_commands=0)
Path(sys.argv[2]).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
raise SystemExit(0 if result.wasSuccessful() else 1)
''', encoding='utf-8')
    with (output / 'console.log').open('x', encoding='utf-8') as log:
        result = subprocess.run([sys.executable, '-B', str(worker), str(config),
            str(output / 'result.json')], cwd=runtime, env=env,
            stdout=log, stderr=subprocess.STDOUT)
    print('Result:', output / 'result.json', 'exit:', result.returncode)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
