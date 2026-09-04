import concurrent.futures
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from remote_worktree import prepare, validate_source


class RemoteWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='harness-runner-test-')
        self.root = Path(self.temp.name)
        self.repo = self.root / 'source'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'runner@example.test')
        self.git('config', 'user.name', 'Runner Test')
        (self.repo / 'README').write_text('initial')
        self.git('add', '.')
        self.git('commit', '-m', 'initial')

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def checkout(self, key='task-a', ref='main'):
        return prepare(self.root / 'runner', str(self.repo), ref, key, allow_local=True)

    def test_reuses_worktree_and_preserves_uncommitted_files_and_base(self):
        first = self.checkout()
        worktree = Path(first['worktree_path'])
        (worktree / 'progress.txt').write_text('keep this')
        (worktree / 'README').write_text('in progress')
        (self.repo / 'README').write_text('new upstream')
        self.git('commit', '-am', 'upstream update')
        self.assertEqual(self.checkout(ref='a-ref-that-does-not-exist'), first)
        self.assertEqual((worktree / 'progress.txt').read_text(), 'keep this')
        self.assertEqual((worktree / 'README').read_text(), 'in progress')
        self.assertNotEqual(self.git('rev-parse', 'HEAD'), first['base_sha'])
        origin = subprocess.check_output(['git', '-C', str(worktree), 'remote', 'get-url', 'origin'], text=True).strip()
        self.assertEqual(origin, str(self.repo))

    def test_two_task_setups_and_duplicate_setup_are_safe_concurrently(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(self.checkout, ['task-a', 'task-b', 'task-a']))
        self.assertEqual(results[0], results[2])
        self.assertNotEqual(results[0]['worktree_path'], results[1]['worktree_path'])
        self.assertNotEqual(results[0]['branch_name'], results[1]['branch_name'])
        self.assertEqual(results[0]['base_sha'], results[1]['base_sha'])

    def test_recovers_after_checkout_before_metadata_was_saved(self):
        first = self.checkout()
        (self.root / 'runner/tasks/task-a.json').unlink()
        (Path(first['worktree_path']) / 'saved.txt').write_text('preserved')
        self.assertEqual(first, self.checkout())
        self.assertEqual((Path(first['worktree_path']) / 'saved.txt').read_text(), 'preserved')

    def test_missing_worktree_requires_recovery(self):
        first = self.checkout()
        worktree = Path(first['worktree_path'])
        worktree.rename(worktree.with_name('moved-task'))
        with self.assertRaisesRegex(ValueError, 'recovery'):
            self.checkout()

    def test_source_change_does_not_overwrite_task(self):
        first = self.checkout()
        with self.assertRaisesRegex(ValueError, 'different repository'):
            prepare(self.root / 'runner', str(self.root / 'other'), 'main', 'task-a', allow_local=True)
        self.assertTrue(Path(first['worktree_path']).is_dir())

    def test_source_validation_rejects_secrets_and_command_transports(self):
        for value in ['https://github.com/o/r.git', 'ssh://git@github.com/o/r.git', 'git@github.com:o/r.git']:
            validate_source(value)
        for value in ['https://token@github.com/o/r', 'https://u:p@github.com/o/r',
                      'https://github.com/o/r?token=secret',
                      'ext::sh -c evil', '-option', '/tmp/repo', 'file:///tmp/repo',
                      'https://github.com/o/r\nother']:
            with self.assertRaises(ValueError):
                validate_source(value)

    def test_task_path_cannot_escape_root(self):
        with self.assertRaises(ValueError):
            self.checkout('../outside')

    def test_refspec_cannot_update_another_task_branch(self):
        for ref in ['main:refs/heads/harness/task-b', '+main', '--upload-pack=evil']:
            with self.assertRaises(ValueError):
                self.checkout(ref=ref)
