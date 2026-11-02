"""Git checkout metadata must not conceal unexpected or damaged payload files."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from verify_bundle import verify


class BundleCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'payload.bin').write_bytes(b'1234')
        self.items = [{'path': 'payload.bin', 'size': 4,
                       'sha256': hashlib.sha256(b'1234').hexdigest()}]
        self.write_manifest()

    def write_manifest(self):
        (self.root / 'manifest_sha256.json').write_text(json.dumps({'files': self.items}), encoding='utf-8')

    def test_root_git_metadata_directory_is_excluded(self):
        (self.root / '.git').mkdir()
        (self.root / '.git/config').write_text('local checkout metadata', encoding='utf-8')
        self.assertTrue(verify(self.root)['passed'])

    def test_worktree_git_file_is_excluded(self):
        (self.root / '.git').write_text('gitdir: ../worktrees/example', encoding='utf-8')
        self.assertTrue(verify(self.root)['passed'])

    def test_unregistered_payload_still_fails(self):
        (self.root / 'unexpected.bin').write_bytes(b'bad')
        self.assertIn({'path': 'unexpected.bin', 'error': 'unregistered_file'}, verify(self.root)['errors'])

    def test_changed_payload_still_fails(self):
        (self.root / 'payload.bin').write_bytes(b'4321')
        self.assertIn({'path': 'payload.bin', 'error': 'sha256_mismatch'}, verify(self.root)['errors'])

    def test_manifest_cannot_declare_git_metadata(self):
        self.items.append({'path': '.git/config', 'size': 0, 'sha256': '0'*64})
        self.write_manifest()
        self.assertIn({'path': '.git/config', 'error': 'invalid_or_duplicate_path'}, verify(self.root)['errors'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
