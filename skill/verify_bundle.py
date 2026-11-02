"""Portable read-only integrity check. No board, network or third-party package."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re


def verify(root):
    root = Path(root).resolve()
    manifest = json.loads((root / 'manifest_sha256.json').read_text(encoding='utf-8'))
    errors = []
    registered = set()
    for item in manifest['files']:
        name = item['path']
        rel = PurePosixPath(name)
        if (not rel.parts or rel.parts[0] == '.git' or rel.is_absolute() or '..' in rel.parts or '\\' in name
                or name in registered or any(re.fullmatch(r'[a-z0-9_.-]+', part) is None for part in rel.parts)):
            errors.append({'path': name, 'error': 'invalid_or_duplicate_path'})
            continue
        registered.add(name)
        p = root.joinpath(*rel.parts)
        if any(x.is_symlink() for x in [p, *p.parents] if x != root.parent):
            errors.append({'path': name, 'error': 'symlink_not_allowed'})
        elif not p.is_file():
            errors.append({'path': name, 'error': 'missing'})
        elif p.stat().st_size != item['size']:
            errors.append({'path': name, 'error': 'size_mismatch'})
        elif hashlib.sha256(p.read_bytes()).hexdigest() != item['sha256']:
            errors.append({'path': name, 'error': 'sha256_mismatch'})
        if any(re.fullmatch(r'[a-z0-9_.-]+', part) is None for part in rel.parts):
            errors.append({'path': name, 'error': 'filename_not_lowercase_ascii'})
    # A Git checkout has local repository metadata that is not part of a
    # distributable source tree. Exclude only the root .git entry, not payloads,
    # logs or arbitrary hidden files. Do not walk metadata merely to ignore it.
    actual = set()
    for folder, directories, files in os.walk(root, followlinks=False):
        current = Path(folder)
        if current == root:
            directories[:] = [name for name in directories if name != '.git']
            files = [name for name in files if name != '.git']
        for name in files:
            actual.add((current / name).relative_to(root).as_posix())
    for name in sorted(actual - registered - {'manifest_sha256.json'}):
        errors.append({'path': name, 'error': 'unregistered_file'})
    return {'schema': 'SUBMISSION_BUNDLE_CHECK_V1', 'passed': not errors,
            'registered_files': len(registered), 'errors': errors,
            'evidence_class': 'FILE_INTEGRITY_NOT_HARDWARE_OR_ACCURACY'}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('root', nargs='?', default=str(Path(__file__).resolve().parents[1]))
    args = ap.parse_args()
    result = verify(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
