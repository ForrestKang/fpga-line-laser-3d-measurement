"""Git checkout metadata must not conceal unexpected or damaged payload files."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
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

    def assert_error(self, code, path=None):
        result = verify(self.root)
        self.assertFalse(result['passed'])
        self.assertEqual(result['schema'], 'SUBMISSION_BUNDLE_CHECK_V1')
        self.assertTrue(any(error['error'] == code and (path is None or error['path'] == path)
                            for error in result['errors']), result)
        return result

    def test_valid_bundle_and_empty_bundle(self):
        self.assertTrue(verify(self.root)['passed'])
        (self.root / 'payload.bin').unlink()
        self.items = []
        self.write_manifest()
        self.assertTrue(verify(self.root)['passed'])

    def test_missing_manifest(self):
        (self.root / 'manifest_sha256.json').unlink()
        self.assert_error('manifest_missing')

    def test_unreadable_manifest_directory(self):
        (self.root / 'manifest_sha256.json').unlink()
        (self.root / 'manifest_sha256.json').mkdir()
        self.assert_error('manifest_read_error')

    def test_bad_json_and_encoding(self):
        for data in (b'{', b'\xff', b'{"files": []} trailing'):
            with self.subTest(data=data):
                (self.root / 'manifest_sha256.json').write_bytes(data)
                self.assert_error('invalid_manifest_json')

    def test_utf8_bom_manifest_is_accepted_without_rewriting(self):
        path = self.root / 'manifest_sha256.json'
        content = b'\xef\xbb\xbf' + path.read_bytes()
        path.write_bytes(content)
        self.assertTrue(verify(self.root)['passed'])
        self.assertEqual(path.read_bytes(), content)

    def test_duplicate_json_keys_at_both_levels(self):
        for content in ('{"files": [], "files": ' + json.dumps(self.items) + '}',
                        '{"files": [{"path": "ignored.bin", "path": "payload.bin", '
                        '"size": 4, "sha256": "' + self.items[0]['sha256'] + '"}]}'):
            with self.subTest(content=content):
                (self.root / 'manifest_sha256.json').write_text(content, encoding='utf-8')
                self.assert_error('invalid_manifest_json')

    def test_invalid_manifest_structure(self):
        for manifest in (None, [], 1, 'text', {}, {'files': None}, {'files': {}}, {'files': 'text'}):
            with self.subTest(manifest=manifest):
                (self.root / 'manifest_sha256.json').write_text(json.dumps(manifest), encoding='utf-8')
                self.assert_error('invalid_manifest_structure')

    def test_invalid_entry_shapes(self):
        for entry in (None, [], 1, 'text', {}):
            with self.subTest(entry=entry):
                self.items = [entry]
                self.write_manifest()
                self.assert_error('invalid_manifest_entry')

    def test_invalid_field_types_and_values(self):
        original = self.items[0].copy()
        values = {'path': (None, 1, [], {}),
                  'size': (None, True, False, '4', 4.0, -1, [], {}),
                  'sha256': (None, 1, [], {}, '', '0' * 63, 'g' * 64, 'A' * 64)}
        for field, candidates in values.items():
            for value in candidates:
                with self.subTest(field=field, value=value):
                    self.items = [dict(original, **{field: value})]
                    self.write_manifest()
                    result = self.assert_error('invalid_manifest_entry')
                    self.assertIn(field, result['errors'][0]['fields'])
        for field in original:
            with self.subTest(missing=field):
                self.items = [original.copy()]
                del self.items[0][field]
                self.write_manifest()
                self.assert_error('invalid_manifest_entry')

    def test_invalid_paths_remain_rejected(self):
        for name in ('', '.', '../payload.bin', '/payload.bin', 'C:/payload.bin',
                     'dir\\payload.bin', 'Payload.bin', '\u6587\u4ef6.bin', '.git/config', 'a/../payload.bin'):
            with self.subTest(name=name):
                self.items[0]['path'] = name
                self.write_manifest()
                self.assert_error('invalid_or_duplicate_path')

    def test_duplicate_path(self):
        self.items.append(self.items[0].copy())
        self.write_manifest()
        self.assert_error('invalid_or_duplicate_path', 'payload.bin')

    def test_nested_git_is_payload(self):
        (self.root / 'nested/.git').mkdir(parents=True)
        (self.root / 'nested/.git/config').write_bytes(b'local')
        self.assert_error('unregistered_file', 'nested/.git/config')
        self.items.append({'path': 'nested/.git/config', 'size': 5,
                           'sha256': hashlib.sha256(b'local').hexdigest()})
        self.write_manifest()
        self.assertTrue(verify(self.root)['passed'])

    def test_missing_and_resized_payload(self):
        (self.root / 'payload.bin').unlink()
        self.assert_error('missing', 'payload.bin')
        (self.root / 'payload.bin').write_bytes(b'longer')
        self.assert_error('size_mismatch', 'payload.bin')

    def test_missing_or_file_root(self):
        for root in (self.root / 'absent', self.root / 'payload.bin'):
            with self.subTest(root=root):
                result = verify(root)
                self.assertFalse(result['passed'])
                self.assertEqual(result['errors'][0]['error'], 'root_not_directory')

    def test_payload_read_failure_is_structured(self):
        original_open = Path.open

        def fail_payload(path, *args, **kwargs):
            if path == self.root / 'payload.bin':
                raise PermissionError('simulated read denial')
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', fail_payload):
            self.assert_error('file_read_error', 'payload.bin')

    def test_directory_scan_failure_is_structured(self):
        def failed_walk(root, **kwargs):
            kwargs['onerror'](PermissionError(13, 'simulated scan denial', str(root / 'denied')))
            return iter(())

        with patch('verify_bundle.os.walk', failed_walk):
            self.assert_error('directory_read_error', 'denied')

    def test_large_file_is_hashed_in_bounded_chunks(self):
        data = b'chunk-data\x00' * 250000
        target = self.root / 'payload.bin'
        target.write_bytes(data)
        self.items = [{'path': 'payload.bin', 'size': len(data),
                       'sha256': hashlib.sha256(data).hexdigest()}]
        self.write_manifest()
        original_open = Path.open
        reads = []

        class CheckedReader:
            def __enter__(self):
                self.stream = original_open(target, 'rb')
                return self

            def __exit__(self, *args):
                self.stream.close()

            def read(self, size=-1):
                if not 0 < size <= 1024 * 1024:
                    raise AssertionError('unbounded payload read')
                reads.append(size)
                return self.stream.read(size)

        def checked_open(path, *args, **kwargs):
            return CheckedReader() if path == target else original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', checked_open):
            self.assertTrue(verify(self.root)['passed'])
        self.assertGreater(len(reads), 3)

    def make_symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as error:
            self.skipTest('symbolic links unavailable: ' + str(error))

    def test_symbolic_payload_cannot_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'payload.bin'
            target.write_bytes(b'1234')
            (self.root / 'payload.bin').unlink()
            self.make_symlink(self.root / 'payload.bin', target)
            self.assert_error('symlink_not_allowed', 'payload.bin')
            self.assertEqual(target.read_bytes(), b'1234')

    def test_symbolic_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'manifest.json'
            target.write_bytes((self.root / 'manifest_sha256.json').read_bytes())
            (self.root / 'manifest_sha256.json').unlink()
            self.make_symlink(self.root / 'manifest_sha256.json', target)
            self.assert_error('symlink_not_allowed', 'manifest_sha256.json')

    def test_unregistered_symbolic_directory_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            self.make_symlink(self.root / 'linked', Path(outside), directory=True)
            self.assert_error('symlink_not_allowed', 'linked')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction test')
    def test_junction_cannot_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'secret.bin'
            target.write_bytes(b'keep')
            link = self.root / 'linked'
            created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), outside],
                                     capture_output=True, text=True)
            if created.returncode:
                self.skipTest('junction creation unavailable: ' + created.stderr.strip())
            try:
                result = self.assert_error('symlink_not_allowed', 'linked')
                self.assertFalse(any(error['path'] == 'linked/secret.bin' for error in result['errors']))
                self.items.append({'path': 'linked/secret.bin', 'size': 4,
                                   'sha256': hashlib.sha256(b'keep').hexdigest()})
                self.write_manifest()
                self.assert_error('symlink_not_allowed', 'linked/secret.bin')
                self.assertEqual(target.read_bytes(), b'keep')
            finally:
                link.rmdir()

    def test_cli_returns_json_and_never_changes_inputs(self):
        script = Path(__file__).with_name('verify_bundle.py')
        manifest_path = self.root / 'manifest_sha256.json'
        original = manifest_path.read_bytes()
        surrogate_entry = dict(self.items[0], path='\ud800.bin')
        for case, content, code in (('valid', original, 0), ('missing', None, 1),
                                    ('bad_json', b'{', 1), ('structure', b'[]', 1),
                                    ('duplicate', b'{"files": [], "files": []}', 1),
                                    ('surrogate_path', json.dumps({'files': [surrogate_entry]}).encode('utf-8'), 1)):
            with self.subTest(case=case):
                if manifest_path.exists():
                    manifest_path.unlink()
                if content is not None:
                    manifest_path.write_bytes(content)
                before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
                run = subprocess.run([sys.executable, '-B', str(script), str(self.root)],
                                     capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(run.returncode, code, run.stderr)
                self.assertEqual(run.stderr, '')
                self.assertEqual(json.loads(run.stdout)['passed'], code == 0)
                after = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
                self.assertEqual(before, after)

    def test_cli_json_under_cp950_with_unicode_root_and_error(self):
        root = self.root / '\u4e2d\u6587\u5de5\u7a0b'
        root.mkdir()
        path = root / 'manifest_sha256.json'
        name = '\u6d4b\u8bd5.bin'
        path.write_text(json.dumps({'files': [dict(self.items[0], path=name)]}), encoding='utf-8')
        before = path.read_bytes()
        env = dict(os.environ, PYTHONIOENCODING='cp950:strict')
        run = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('verify_bundle.py')), str(root)],
                             capture_output=True, env=env)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(run.stderr, b'')
        self.assertEqual(json.loads(run.stdout.decode('ascii'))['errors'][0]['path'], name)
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
