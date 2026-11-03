"""Verify portable workflow export and its failure/ownership boundaries."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from build_skill import build, SKILL_FILES

EXPECTED_ARCHIVE_NAMES = {'pynq-engineering-workflow/SKILL.md',
                          'pynq-engineering-workflow/readme.md'}

class BuildSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pynq_workflow_export_')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'
        source = Path(__file__).resolve().parents[2]
        for name in SKILL_FILES:
            dest = self.root / 'skill' / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / 'skill' / name, dest)
        self.output = self.base / 'skill.zip'

    def test_deterministic_exact_contents_and_source_unchanged(self):
        before = {p.relative_to(self.root): p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file()}
        result = build(self.root, self.output)
        other = self.base / 'second.zip'
        build(self.root, other)
        self.assertEqual(self.output.read_bytes(), other.read_bytes())
        self.assertEqual(result['files'], 2)
        with zipfile.ZipFile(self.output) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()), EXPECTED_ARCHIVE_NAMES)
            self.assertEqual(len(archive.namelist()), 2)
            for name in EXPECTED_ARCHIVE_NAMES:
                basename = name.split('/')[-1]
                relative = Path('skill') / ('skill.md' if basename == 'SKILL.md' else basename)
                self.assertEqual(archive.read(name), before[relative], name)
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes()
                                 for p in self.root.rglob('*') if p.is_file()})

    def test_existing_output_preserved(self):
        self.output.write_bytes(b'keep this')
        with self.assertRaises(FileExistsError):
            build(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), b'keep this')

    def test_missing_entry_does_not_leave_archive(self):
        (self.root / 'skill/skill.md').unlink()
        with self.assertRaises(FileNotFoundError):
            build(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_readme_does_not_leave_archive(self):
        (self.root / 'skill/readme.md').unlink()
        with self.assertRaises(FileNotFoundError):
            build(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_output_cannot_modify_source_tree(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            build(self.root, self.root / 'skill.zip')
        self.assertFalse((self.root / 'skill.zip').exists())

    def test_invalid_entry_rejected_before_writing(self):
        for content in ('no frontmatter',
                        '---\nname: wrong-name\ndescription: example\n---\nbody',
                        '---\nname: pynq-engineering-workflow\n---\nbody'):
            with self.subTest(content=content):
                (self.root / 'skill/skill.md').write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError):
                    build(self.root, self.output)
                self.assertFalse(self.output.exists())

    def test_invalid_text_rejected_before_writing(self):
        (self.root / 'skill/readme.md').write_bytes(b'\xff\xff')
        with self.assertRaises(UnicodeError):
            build(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_extracted_documents_in_unrelated_chinese_directory(self):
        build(self.root, self.output)
        consumer = self.base / '\u72ec\u7acb\u5de5\u7a0b with spaces'
        with zipfile.ZipFile(self.output) as archive:
            archive.extractall(consumer)
        skill = consumer / 'pynq-engineering-workflow'
        self.assertEqual({p.name for p in skill.iterdir()}, {'SKILL.md', 'readme.md'})
        for p in skill.iterdir():
            text = p.read_text(encoding='utf-8-sig')
            for target in re.findall(r'!?\[[^\]\n]*\]\(([^)\n]+)\)', text):
                if not target.startswith(('https://', 'http://', '#')):
                    self.assertTrue((p.parent / target.split('#', 1)[0]).is_file(), target)
        self.assertTrue((skill / 'SKILL.md').read_text(encoding='utf-8-sig').startswith('---'))

    def test_copied_cli_exports_without_source_checkout(self):
        script = self.base / 'export_workflow.py'
        shutil.copyfile(Path(__file__).with_name('build_skill.py'), script)
        result = subprocess.run([sys.executable, '-B', str(script), '--root', str(self.root),
                                 '--output', str(self.output)], cwd=self.base, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        data = json.loads(result.stdout)
        self.assertTrue(data['passed'])
        self.assertEqual(data['files'], 2)
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(set(archive.namelist()), EXPECTED_ARCHIVE_NAMES)

if __name__ == '__main__':
    unittest.main(verbosity=2)
