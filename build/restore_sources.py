"""Restore buildable sources with functional module names into a NEW directory. Never touches hardware."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import re


def restore(output):
    root = Path(__file__).resolve().parents[1]
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('Choose a non-existing output directory: ' + str(output))
    entries = json.loads((root / 'source_map.json').read_text(encoding='utf-8'))
    selected = [e for e in entries if e.get('restore')]
    names = set()
    for e in selected:
        name = PurePosixPath(e['restore'])
        if (not name.parts or name.is_absolute() or '..' in name.parts or '\\' in str(name)
                or str(name).casefold() in names
                or any(re.fullmatch(r'[A-Za-z0-9_.-]+', part) is None for part in name.parts)):
            raise ValueError('Unsafe or duplicate restore path: ' + str(name))
        names.add(str(name).casefold())
        p = root / e['path']
        if not p.resolve().is_relative_to(root):
            raise ValueError('Source path outside bundle: ' + e['path'])
        if p.is_symlink() or not p.is_file() or p.stat().st_size != e['size'] or hashlib.sha256(p.read_bytes()).hexdigest() != e['sha256']:
            raise ValueError('Source integrity mismatch: ' + e['path'])
    output.mkdir(parents=True, exist_ok=False)
    for e in selected:
        dest = output / e['restore']
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / e['path'], dest)
    # Original integration scripts expect runtime beside rtl; recreate that layout
    # from the one canonical runtime copy in the submission, avoiding ZIP duplicates.
    if not (output / 'fpga/runtime').exists():
        shutil.copytree(output / 'runtime', output / 'fpga/runtime')
    if (output / 'fpga/model').is_dir():
        shutil.copytree(output / 'fpga/model', output / 'model')
    return output


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    print(restore(args.output))
