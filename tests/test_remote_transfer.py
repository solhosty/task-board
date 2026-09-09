"""Exercise the actual runner programs without Coder or model calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HELPERS = Path(__file__).resolve().parents[1] / 'infra' / 'runner'


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='transfer-test-')
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'source'
        self.target = Path(self.temp.name) / 'target'
        self.source.mkdir()
        self.git(self.source, 'init', '-q')
        self.git(self.source, 'config', 'user.name', 'Test')
        self.git(self.source, 'config', 'user.email', 'test@example.test')
        (self.source / 'tracked').write_text('original\n')
        self.git(self.source, 'add', '.')
        self.git(self.source, 'commit', '-qm', 'base')
        subprocess.run(['git', 'clone', '-q', str(self.source), str(self.target)], check=True)

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True).stdout

    def export(self):
        return json.loads(subprocess.check_output([sys.executable, str(HELPERS / 'remote_transfer_export.py'), str(self.source)]))

    def apply(self, data):
        result = subprocess.run([sys.executable, str(HELPERS / 'remote_transfer_apply.py'), str(self.target)],
                                input=json.dumps(data), text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_changed_deleted_and_binary_files(self):
        (self.source / 'tracked').unlink()
        (self.source / 'binary').write_bytes(b'\x00\xffdata')
        result = self.apply(self.export())
        self.assertTrue(result['available'])
        self.assertFalse((self.target / 'tracked').exists())
        self.assertEqual((self.target / 'binary').read_bytes(), b'\x00\xffdata')

    def test_existing_destination_changes_are_preserved(self):
        (self.target / 'tracked').write_text('user work')
        self.assertFalse(self.apply(self.export())['available'])
        self.assertEqual((self.target / 'tracked').read_text(), 'user work')

    def test_transfers_new_source_commit_and_uncommitted_work(self):
        (self.source / 'tracked').write_text('committed change\n')
        self.git(self.source, 'commit', '-am', 'source-only commit')
        (self.source / 'extra').write_text('uncommitted work')
        result = self.apply(self.export())
        self.assertTrue(result['available'])
        self.assertEqual(self.git(self.source, 'rev-parse', 'HEAD'), self.git(self.target, 'rev-parse', 'HEAD'))
        self.assertEqual((self.target / 'tracked').read_text(), 'committed change\n')
        self.assertEqual((self.target / 'extra').read_text(), 'uncommitted work')

    def test_invalid_path_rejected_before_any_file_changes(self):
        data = self.export()
        data['deleted'] = ['tracked', '../outside']
        self.assertFalse(self.apply(data)['available'])
        self.assertEqual((self.target / 'tracked').read_text(), 'original\n')
