"""Package one physically admitted R43 result without board I/O or baseline writes."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = Path('D:/Workspace/CodexWork/scan3d_r43_pl_balance_20261029_01')

def sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('build')
    parser.add_argument('--constraint-receipt', type=Path, required=True)
    args = parser.parse_args()
    build = ROOT / 'build' / args.build
    admission = json.loads((build / 'ADMISSION_R43.json').read_text())
    constraint = json.loads(args.constraint_receipt.read_text())
    if not admission['passed'] or not constraint['passed']:
        raise SystemExit('Full physical and constraint gates required')
    output = ROOT / 'firmware' / args.build
    output.mkdir(parents=True, exist_ok=False)
    sources = {
        'r43.bit': build / 'artifacts/r43.bit',
        'r43.bin': build / 'artifacts/r43.bin',
        'r43.hwh': ORIGINAL / 'firmware/b03_8static_banked/r43.hwh',
    }
    files = []
    for name, source in sources.items():
        shutil.copyfile(source, output / name)
        if sha(source) != sha(output / name):
            raise RuntimeError('Copy hash mismatch')
        files.append({'file': name, 'source': str(source), 'bytes': source.stat().st_size,
                      'sha256': sha(output / name)})
    manifest = {
        'kind': 'R43_EIGHT_LANE_POST_ROUTE_PHYSICAL_OPTIMIZATION_NOT_BOARD_PROOF',
        'build': args.build, 'row_lanes': 8, 'rtl_changed': False, 'bd_changed': False,
        'clock_periods_changed': False, 'protocol_changed': False,
        'hwh_provenance': 'Inherited byte-identical HWH from unchanged original BD and synthesized netlist',
        'route_dcp_sha256': sha(build / 'artifacts/route.dcp'),
        'admission_path': str(build / 'ADMISSION_R43.json'), 'admission': admission,
        'constraint_receipt': str(args.constraint_receipt),
        'constraint_receipt_sha256': sha(args.constraint_receipt), 'files': files,
        'board_validation': 'NOT_RUN_BY_TIMING_WORKER',
    }
    (output / 'MANIFEST_R43_TIMING.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest, indent=2))

if __name__ == '__main__':
    main()

