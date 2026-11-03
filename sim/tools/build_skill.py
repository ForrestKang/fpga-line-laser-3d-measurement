"""Export the reusable PYNQ engineering workflow using Python's standard library."""
import argparse
import json
from pathlib import Path
import re
import stat
import zipfile

SKILL_NAME = 'pynq-engineering-workflow'
SKILL_FILES = ('skill.md', 'readme.md')

def _read_regular(root, relative):
    path = root
    for part in Path(relative).parts:
        path = path / part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'linked input: {relative}')
    if not path.is_file():
        raise ValueError(f'not a regular input file: {relative}')
    data = path.read_bytes()
    if not data.decode('utf-8-sig').strip():
        raise ValueError(f'empty input: {relative}')
    return data

def _validate_entry(data):
    text = data.decode('utf-8-sig').replace('\r\n', '\n')
    match = re.match(r'\A---\n(.*?)\n---\n', text, re.S)
    if match is None:
        raise ValueError('skill entry requires YAML frontmatter')
    header = match.group(1)
    name = re.search(r'^name:\s*(\S+)\s*$', header, re.M)
    description = re.search(r'^description:\s*(\S.*)$', header, re.M)
    if name is None or name.group(1) != SKILL_NAME or description is None:
        raise ValueError('skill entry name or description is invalid')

def build(root, output):
    root = Path(root).resolve(strict=True)
    output = Path(output).absolute()
    if output.resolve().is_relative_to(root):
        raise ValueError('output must be outside the source repository')
    entries = {}
    for name in SKILL_FILES:
        data = _read_regular(root, f'skill/{name}')
        if name == 'skill.md':
            _validate_entry(data)
        archive_name = 'SKILL.md' if name == 'skill.md' else name
        entries[f'{SKILL_NAME}/{archive_name}'] = data
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
            'output': str(output), 'size': output.stat().st_size}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
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
