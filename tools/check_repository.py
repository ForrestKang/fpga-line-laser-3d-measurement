"""Offline repository structure, authored-document links and Python syntax checks."""
import argparse
import ast
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


def local_link_error(root, source, target):
    target = target.strip()
    if not target or target.startswith('#'):
        return None
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    path = unquote(parsed.path)
    if not path:
        return None
    resolved = (source.parent / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        return 'outside_repository'
    if not resolved.exists():
        return 'missing_target'
    return None


def check(root):
    root = Path(root).resolve()
    required = ['readme.md', 'license.md', 'source_map.json',
                'manifest_sha256.json', 'build/restore_sources.py',
                'build/reproduce.py', 'src/fpga/scripts/build_final.tcl',
                'src/ps/r33/live.py', 'board/firmware/final.bit',
                'board/firmware/final.hwh', 'report/design_report.pdf',
                'report/ai_collaboration_records.md',
                'skill/pynq_release_check.py', 'docs/known_issues.md']
    errors = []
    for path in required:
        if not (root / path).is_file():
            errors.append({'path': path, 'error': 'required_file_missing'})
    documents = [root / 'readme.md', root / 'contributing.md', root / 'security.md']
    documents += sorted((root / 'docs').glob('*.md'))
    link_count = 0
    # Deliberately exclude frozen historical reports: they preserve original
    # workstation paths and are not newly authored repository navigation.
    for source in documents:
        if not source.is_file():
            errors.append({'path': str(source.relative_to(root)), 'error': 'document_missing'})
            continue
        for match in re.finditer(r'!?\[[^\]\n]*\]\(([^)\n]+)\)', source.read_text(encoding='utf-8')):
            target = match.group(1)
            link_count += 1
            error = local_link_error(root, source, target)
            if error:
                errors.append({'path': source.relative_to(root).as_posix(),
                               'target': target, 'error': error})
    py_count = 0
    for source in root.rglob('*.py'):
        relative = source.relative_to(root)
        if any(part in {'.git', '__pycache__', '.venv'} for part in relative.parts):
            continue
        py_count += 1
        try:
            ast.parse(source.read_bytes(), filename=relative.as_posix())
        except (SyntaxError, UnicodeError) as exc:
            errors.append({'path': relative.as_posix(), 'error': 'python_syntax',
                           'detail': str(exc)})
    return {'schema': 'REPOSITORY_CHECK_V1', 'passed': not errors,
            'python_files': py_count, 'documents': len(documents),
            'links_examined': link_count, 'errors': errors,
            'scope': 'structure_links_syntax_only_not_business_or_hardware_acceptance'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = check(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
