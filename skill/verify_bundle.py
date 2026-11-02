"""Portable read-only integrity check. No board, network or third-party package."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


MANIFEST_NAME = 'manifest_sha256.json'


def _result(errors, registered=()):
    return {'schema': 'SUBMISSION_BUNDLE_CHECK_V1', 'passed': not errors,
            'registered_files': len(registered), 'errors': errors,
            'evidence_class': 'FILE_INTEGRITY_NOT_HARDWARE_OR_ACCURACY'}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _is_link(path):
    info = path.lstat()
    # Windows junctions are directory reparse points, including on Python
    # versions without Path.is_junction(). Never traverse them as payload.
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, 'st_file_attributes', 0)
                    & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)))


def _payload_has_link(root, rel):
    current = root
    for part in rel.parts:
        current = current / part
        try:
            if _is_link(current):
                return True
        except FileNotFoundError:
            break
    return False


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify(root):
    errors = []
    registered = set()
    try:
        root = Path(root).resolve()
        if not root.is_dir():
            return _result([{'path': '.', 'error': 'root_not_directory'}])
        manifest_path = root / MANIFEST_NAME
        if _is_link(manifest_path):
            return _result([{'path': MANIFEST_NAME, 'error': 'symlink_not_allowed'}])
        manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'),
                              object_pairs_hook=_unique_object)
    except FileNotFoundError:
        return _result([{'path': MANIFEST_NAME, 'error': 'manifest_missing'}])
    except (ValueError, UnicodeError, RecursionError):
        return _result([{'path': MANIFEST_NAME, 'error': 'invalid_manifest_json'}])
    except (OSError, RuntimeError):
        return _result([{'path': MANIFEST_NAME, 'error': 'manifest_read_error'}])
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), list):
        return _result([{'path': MANIFEST_NAME, 'error': 'invalid_manifest_structure'}])
    for index, item in enumerate(manifest['files']):
        if not isinstance(item, dict):
            errors.append({'path': MANIFEST_NAME, 'error': 'invalid_manifest_entry', 'index': index})
            continue
        invalid_fields = [field for field, valid in (
            ('path', isinstance(item.get('path'), str)),
            ('size', type(item.get('size')) is int and item['size'] >= 0),
            ('sha256', isinstance(item.get('sha256'), str)
             and re.fullmatch(r'[0-9a-f]{64}', item['sha256']) is not None)) if not valid]
        if invalid_fields:
            errors.append({'path': MANIFEST_NAME, 'error': 'invalid_manifest_entry',
                           'index': index, 'fields': invalid_fields})
            continue
        name = item['path']
        rel = PurePosixPath(name)
        if (not rel.parts or rel.parts[0] == '.git' or rel.is_absolute() or '..' in rel.parts or '\\' in name
                or name in registered or any(re.fullmatch(r'[a-z0-9_.-]+', part) is None for part in rel.parts)):
            errors.append({'path': name, 'error': 'invalid_or_duplicate_path'})
            continue
        registered.add(name)
        p = root.joinpath(*rel.parts)
        try:
            if _payload_has_link(root, rel):
                errors.append({'path': name, 'error': 'symlink_not_allowed'})
            elif not stat.S_ISREG(p.stat().st_mode):
                errors.append({'path': name, 'error': 'missing'})
            elif p.stat().st_size != item['size']:
                errors.append({'path': name, 'error': 'size_mismatch'})
            elif _sha256(p) != item['sha256']:
                errors.append({'path': name, 'error': 'sha256_mismatch'})
        except FileNotFoundError:
            errors.append({'path': name, 'error': 'missing'})
        except OSError:
            errors.append({'path': name, 'error': 'file_read_error'})
    # A Git checkout has local repository metadata that is not part of a
    # distributable source tree. Exclude only the root .git entry, not payloads,
    # logs or arbitrary hidden files. Do not walk metadata merely to ignore it.
    actual = set()
    def scan_error(error):
        errors.append({'path': os.path.relpath(error.filename or root, root).replace('\\', '/'),
                       'error': 'directory_read_error'})

    for folder, directories, files in os.walk(root, followlinks=False, onerror=scan_error):
        current = Path(folder)
        if current == root:
            directories[:] = [name for name in directories if name != '.git']
            files = [name for name in files if name != '.git']
        for name in directories[:]:
            p = current / name
            try:
                if _is_link(p):
                    directories.remove(name)
                    errors.append({'path': p.relative_to(root).as_posix(), 'error': 'symlink_not_allowed'})
            except OSError:
                directories.remove(name)
                errors.append({'path': p.relative_to(root).as_posix(), 'error': 'directory_read_error'})
        for name in files:
            actual.add((current / name).relative_to(root).as_posix())
    for name in sorted(actual - registered - {MANIFEST_NAME}):
        errors.append({'path': name, 'error': 'unregistered_file'})
    return _result(errors, registered)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('root', nargs='?', default=str(Path(__file__).resolve().parents[1]))
    args = ap.parse_args()
    result = verify(args.root)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
