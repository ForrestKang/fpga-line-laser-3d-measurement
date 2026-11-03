"""Verify that the exported skill remains usable outside its source checkout."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from build_skill import build, SKILL_FILES, VALIDATION_FILES


EXPECTED_ARCHIVE_NAMES = {
    'pynq-overlay-check/SKILL.md',
    'pynq-overlay-check/readme.md',
    'pynq-overlay-check/pynq_release_check.md',
    'pynq-overlay-check/pynq_release_check.py',
    'pynq-overlay-check/verify_bundle.py',
    'pynq-overlay-check/test_pynq_release_check.py',
    'pynq-overlay-check/test_verify_bundle.py',
    'pynq-overlay-check/test_portability.py',
    'validation/skill_portability.json',
    'validation/skill_regression.json',
    'validation/skill_cross_model.json',
    'validation/skill_package.json',
}


class BuildSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill_export_')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'
        source = Path(__file__).resolve().parents[2]
        for name in SKILL_FILES:
            dest = self.root / 'skill' / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / 'skill' / name, dest)
        for name in VALIDATION_FILES:
            dest = self.root / 'sim/results' / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / 'sim/results' / name, dest)
        self.output = self.base / 'skill.zip'

    def test_deterministic_exact_contents_and_source_unchanged(self):
        before = {p.relative_to(self.root): p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file()}
        result = build(self.root, self.output)
        other = self.base / 'second.zip'
        build(self.root, other)
        self.assertEqual(self.output.read_bytes(), other.read_bytes())
        self.assertEqual(result['files'], len(SKILL_FILES) + len(VALIDATION_FILES))
        with zipfile.ZipFile(self.output) as archive:
            self.assertIsNone(archive.testzip())
            # This expectation is independent of the builder's allowlist.
            self.assertEqual(set(archive.namelist()), EXPECTED_ARCHIVE_NAMES)
            self.assertEqual(len(archive.namelist()), len(EXPECTED_ARCHIVE_NAMES))
            for name in EXPECTED_ARCHIVE_NAMES:
                basename = name.split('/')[-1]
                relative = (Path('sim/results') / basename if name.startswith('validation/')
                            else Path('skill') / ('skill.md' if basename == 'SKILL.md' else basename))
                self.assertEqual(archive.read(name), before[relative], name)
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes()
                                 for p in self.root.rglob('*') if p.is_file()})

    def test_existing_output_preserved(self):
        self.output.write_bytes(b'keep this')
        with self.assertRaises(FileExistsError):
            build(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), b'keep this')

    def test_missing_input_does_not_leave_archive(self):
        (self.root / 'skill/skill.md').unlink()
        with self.assertRaises(FileNotFoundError):
            build(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_output_cannot_modify_source_tree(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            build(self.root, self.root / 'skill.zip')
        self.assertFalse((self.root / 'skill.zip').exists())

    def test_invalid_validation_json_rejected_before_writing(self):
        (self.root / 'sim/results/skill_regression.json').write_text('{bad', encoding='utf-8')
        with self.assertRaises(ValueError):
            build(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_extracted_tools_in_unrelated_chinese_directory(self):
        build(self.root, self.output)
        consumer = self.base / '\u72ec\u7acb\u5de5\u7a0b with spaces'
        with zipfile.ZipFile(self.output) as archive:
            archive.extractall(consumer)
        skill = consumer / 'pynq-overlay-check'
        # These subprocesses use only extracted files, not the source checkout.
        for script in ('test_pynq_release_check.py', 'test_verify_bundle.py', 'test_portability.py'):
            with self.subTest(installed_script=script):
                result = subprocess.run([sys.executable, '-B', str(skill / script)],
                                        cwd=consumer, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        project = consumer / 'project'
        project.mkdir()
        data = b'independent payload'
        (project / 'data.bin').write_bytes(data)
        record = {'path': 'data.bin', 'size': len(data),
                  'sha256': hashlib.sha256(data).hexdigest()}
        (project / 'manifest_sha256.json').write_text(json.dumps({'files': [record]}))
        result = subprocess.run([sys.executable, '-B', str(skill / 'verify_bundle.py'), str(project)],
                                cwd=consumer, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout.decode(errors='replace'))
        self.assertTrue(json.loads(result.stdout)['passed'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
