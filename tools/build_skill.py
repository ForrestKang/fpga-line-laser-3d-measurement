"""Build the independently installable PYNQ skill using only Python's stdlib."""

import argparse
import hashlib
import json
from pathlib import Path
import stat
import zipfile


SKILL_NAME = 'pynq-overlay-check'
SKILL_FILES = (
    'skill.md', 'readme.md', 'pynq_release_check.md',
    'pynq_release_check.py', 'verify_bundle.py',
    'test_pynq_release_check.py', 'test_verify_bundle.py', 'test_portability.py',
)
VALIDATION_FILES = (
    'skill_portability.json', 'skill_regression.json', 'skill_cross_model.json',
    'skill_package.json',
)


def _read_regular(root, relative):
    path = root
    for part in Path(relative).parts:
        path = path / part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'linked input: {relative}')
    if not path.is_file():
        raise ValueError(f'not a regular input file: {relative}')
    return path.read_bytes()


def build(root, output):
    root = Path(root).resolve(strict=True)
    output = Path(output).absolute()
    if output.resolve().is_relative_to(root):
        raise ValueError('output must be outside the source repository')
    entries = {}
    for name in SKILL_FILES:
        archive_name = 'SKILL.md' if name == 'skill.md' else name
        entries[f'{SKILL_NAME}/{archive_name}'] = _read_regular(root, f'skill/{name}')
    for name in VALIDATION_FILES:
        data = _read_regular(root, f'sim/results/{name}')
        json.loads(data.decode('utf-8-sig'))
        entries[f'validation/{name}'] = data
    # Fixed metadata and ordering make repeated builds byte-identical.
    with output.open('xb') as stream:
        try:
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=9) as archive:
                for name, data in sorted(entries.items()):
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, data, compresslevel=9)
        except BaseException:
            stream.close()
            output.unlink()
            raise
    return {'passed': True, 'skill': SKILL_NAME, 'files': len(entries),
            'output': str(output), 'size': output.stat().st_size,
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True,
                        help='New ZIP outside the repository; parent must exist.')
    args = parser.parse_args()
    try:
        result = build(args.root, args.output)
    except (OSError, ValueError) as exc:
        result = {'passed': False, 'error': str(exc)}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
