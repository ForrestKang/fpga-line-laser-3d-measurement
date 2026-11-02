"""Failure-focused tests for repository-local Markdown link validation."""
from pathlib import Path
import tempfile
import unittest
from check_repository import local_link_error


class RepositoryLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'docs').mkdir()
        self.source = self.root / 'docs/guide.md'
        self.source.write_text('# Guide', encoding='utf-8')
        (self.root / 'read me.md').write_text('# Home', encoding='utf-8')

    def test_encoded_existing_path_and_anchor(self):
        self.assertIsNone(local_link_error(self.root, self.source, '../read%20me.md#home'))

    def test_missing_relative_file(self):
        self.assertEqual('missing_target', local_link_error(self.root, self.source, 'absent.md'))

    def test_external_links_do_not_become_local_paths(self):
        for target in ['https://github.com/example/project', 'mailto:contact@example.org', '#section']:
            self.assertIsNone(local_link_error(self.root, self.source, target))

    def test_escape_is_rejected_before_existence(self):
        self.assertEqual('outside_repository', local_link_error(self.root, self.source, '../../outside.md'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
