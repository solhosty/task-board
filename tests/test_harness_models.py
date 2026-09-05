"""Model choices per harness at the global, project, and task scopes.

The rotation order stays global; only the model a harness runs is narrowed by a
project and then by a task, so these tests assert the narrowing direction and
that clearing a choice restores inheritance instead of freezing a value.
"""
import copy
import functools
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class ScopedModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rotation-models-')
        self.root = Path(self.temp.name).resolve()
        self.original = (app.DATA_ROOT, app.DB_PATH, app.WORKTREE_ROOT, app.ADAPTERS)
        app.DATA_ROOT = self.root / 'state'
        app.DB_PATH = app.DATA_ROOT / 'state.sqlite3'
        app.WORKTREE_ROOT = self.root / 'worktrees'
        app.ADAPTERS = copy.deepcopy(app.ADAPTERS)
        app.init_db()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.check_output(['git', 'init'], cwd=self.repo, stderr=subprocess.STDOUT)
        for key in ('codex', 'claude'):
            app.execute('UPDATE harnesses SET installed=1,enabled=1,billing_confirmed=1 WHERE key=?', (key,))
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(app.API, directory=str(app.STATIC_ROOT)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.project = self.api('/api/projects', {'name': 'Models', 'repo_path': str(self.repo)})
        self.task_id = self.api(f"/api/projects/{self.project['id']}/tasks", {'text': 'Work'})['id']

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        app.DATA_ROOT, app.DB_PATH, app.WORKTREE_ROOT, app.ADAPTERS = self.original
        self.temp.cleanup()

    def api(self, path, payload=None):
        request = urllib.request.Request(self.url + path, data=json.dumps(payload).encode() if payload is not None else None,
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    def task(self):
        return app.one('SELECT * FROM tasks WHERE id=?', (self.task_id,))

    def test_each_scope_narrows_the_next_and_clearing_restores_inheritance(self):
        self.api('/api/harnesses/claude', {'model': 'sonnet'})
        self.assertEqual(app.model_snapshot(self.task())['claude'], {'model': 'sonnet', 'source': 'global'})

        self.api(f"/api/projects/{self.project['id']}/harness-models", {'harness': 'claude', 'model': 'opus'})
        self.assertEqual(app.model_snapshot(self.task())['claude'], {'model': 'opus', 'source': 'project'})
        self.assertEqual(app.one('SELECT model FROM harnesses WHERE key=?', ('claude',))['model'], 'sonnet')

        self.api(f'/api/tasks/{self.task_id}/harness-models', {'harness': 'claude', 'model': 'haiku'})
        self.assertEqual(app.model_snapshot(self.task())['claude'], {'model': 'haiku', 'source': 'task'})
        self.assertEqual(app.model_snapshot(project_id=self.project['id'])['claude'],
                         {'model': 'opus', 'source': 'project'})

        self.api(f'/api/tasks/{self.task_id}/harness-models', {'harness': 'claude', 'model': None})
        self.assertEqual(app.model_snapshot(self.task())['claude'], {'model': 'opus', 'source': 'project'})
        self.api(f"/api/projects/{self.project['id']}/harness-models", {'harness': 'claude', 'model': 'inherit'})
        self.assertEqual(app.model_snapshot(self.task())['claude'], {'model': 'sonnet', 'source': 'global'})

    def test_a_scoped_choice_reaches_the_chosen_harness(self):
        self.api(f'/api/tasks/{self.task_id}', {'preferred_harness': 'claude'})
        self.api(f"/api/projects/{self.project['id']}/harness-models", {'harness': 'claude', 'model': 'opus'})
        chosen, selection = app.choose_harness(self.task())
        self.assertEqual((chosen['key'], chosen['model'], selection), ('claude', 'opus', 'preferred'))

        self.api(f'/api/tasks/{self.task_id}/harness-models', {'harness': 'claude', 'model': 'haiku'})
        chosen, _ = app.choose_harness(self.task())
        self.assertEqual(chosen['model'], 'haiku')
        # The legacy per-task pin and the scoped choice stay one value.
        self.assertEqual(self.task()['preferred_model'], 'haiku')

    def test_legacy_preferred_model_still_decides_and_is_mirrored(self):
        self.api(f'/api/tasks/{self.task_id}', {'preferred_harness': 'codex', 'preferred_model': 'gpt-5-codex'})
        self.assertEqual(app.model_snapshot(self.task())['codex'], {'model': 'gpt-5-codex', 'source': 'task'})
        self.assertEqual(app.scoped_models('task', self.task_id), {'codex': 'gpt-5-codex'})
        state = self.api(f'/api/tasks/{self.task_id}')
        self.assertEqual(state['harness_models']['codex']['model'], 'gpt-5-codex')

    def test_unknown_harness_and_oversized_model_are_refused(self):
        for payload in ({'harness': 'nope', 'model': 'opus'}, {'harness': 'claude', 'model': 'x' * 200}):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.api(f"/api/projects/{self.project['id']}/harness-models", payload)
            self.assertEqual(caught.exception.code, 400)
        self.assertEqual(app.scoped_models('project', self.project['id']), {})

    def test_bootstrap_carries_the_project_snapshot_and_task_overrides(self):
        self.api(f"/api/projects/{self.project['id']}/harness-models", {'harness': 'codex', 'model': 'gpt-5-codex'})
        self.api(f'/api/tasks/{self.task_id}/harness-models', {'harness': 'claude', 'model': 'opus'})
        bootstrap = self.api('/api/bootstrap')
        project = next(item for item in bootstrap['projects'] if item['id'] == self.project['id'])
        self.assertEqual(project['harness_models']['codex'], {'model': 'gpt-5-codex', 'source': 'project'})
        self.assertEqual(project['tasks'][0]['harness_models'], {'claude': 'opus'})
        self.assertIn('models', bootstrap['adapters']['claude'])

    def test_deleting_a_task_removes_its_model_choices(self):
        self.api(f'/api/tasks/{self.task_id}/harness-models', {'harness': 'claude', 'model': 'opus'})
        request = urllib.request.Request(self.url + f'/api/tasks/{self.task_id}', method='DELETE')
        urllib.request.urlopen(request, timeout=15).close()
        self.assertEqual(app.rows('SELECT * FROM task_harness_models'), [])


if __name__ == '__main__':
    unittest.main()
