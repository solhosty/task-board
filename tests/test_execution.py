"""Real HTTP -> runner -> subprocess -> Git -> verify -> merge tests.

Only the harness command is replaced with a deterministic local worker.
No real subscription is used and no user repository is touched.
"""
import copy
import functools
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rotation-execution-')
        self.root = Path(self.temp.name).resolve()
        self.original = (app.DATA_ROOT, app.DB_PATH, app.WORKTREE_ROOT, app.ADAPTERS)
        app.DATA_ROOT = self.root / 'state'
        app.DB_PATH = app.DATA_ROOT / 'state.sqlite3'
        app.WORKTREE_ROOT = self.root / 'worktrees'
        app.ADAPTERS = copy.deepcopy(app.ADAPTERS)
        app.init_db()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init')
        self.git('config', 'user.name', 'Execution test')
        self.git('config', 'user.email', 'execution@example.test')
        (self.repo / 'README.md').write_text('Test repository\n')
        self.git('add', 'README.md')
        self.git('commit', '-m', 'Initial fixture')
        self.base = self.git('rev-parse', 'HEAD').strip()
        worker = self.root / 'worker.py'
        worker.write_text('''import sys, json
from pathlib import Path
mode = sys.argv[1]
if mode == 'quota':
    Path('checkpoint.txt').write_text('progress from first harness')
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Checkpoint saved; continue with the feature implementation.'}}), flush=True)
    print('Usage limit reached; quota exceeded', flush=True)
    sys.exit(1)
if mode == 'resume':
    assert Path('checkpoint.txt').read_text() == 'progress from first harness'
Path('feature.txt').write_text('implemented and verified\\n')
print('Implemented the requested feature.', flush=True)
''')
        self.worker = worker
        self.configure_worker('codex', 'success')
        self.configure_worker('claude', 'resume')
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(app.API, directory=str(app.STATIC_ROOT)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        app.DATA_ROOT, app.DB_PATH, app.WORKTREE_ROOT, app.ADAPTERS = self.original
        self.temp.cleanup()

    def test_task_permissions_are_saved_before_start_and_snapshotted(self):
        project = self.api('/api/projects', {'name':'Permissions', 'repo_path':str(self.repo), 'default_mode':'supervised'})
        created = self.api(f"/api/projects/{project['id']}/tasks", {'text':'Work', 'start':True, 'tool_permissions':'auto'})
        task_id = created['id']
        state = self.api(f'/api/tasks/{task_id}')
        self.assertEqual(state['task']['tool_permissions'], 'auto')
        self.assertEqual(set(json.loads(state['run']['permissions_json']).values()), {'auto'})
        self.api(f'/api/tasks/{task_id}', {'tool_permissions':'standard'})
        self.api('/api/harnesses/claude', {'tool_permissions':'standard'})
        self.api(f"/api/runs/{state['run']['id']}/approve-dispatch", {})
        state = self.wait(task_id, 'awaiting_review')
        self.assertEqual(state['attempts'][0]['tool_permissions'], 'auto')

    def test_coder_github_auth_status_never_exposes_the_token(self):
        server = {'id': 7, 'base_url': 'http://127.0.0.1:3000', 'token_configured': 1}
        with patch.object(app, 'read_coder_token', return_value='secret-token'), \
             patch.object(app, 'coder_json', return_value={'authenticated': False}) as request:
            status = app.coder_external_auth_status(server)
        self.assertEqual(status, {
            'provider': 'github',
            'authenticated': False,
            'login_url': 'http://127.0.0.1:3000/external-auth/github',
        })
        self.assertNotIn('token', status)
        request.assert_called_once_with('http://127.0.0.1:3000', '/api/v2/external-auth/github', 'secret-token')

    def test_coder_auth_requirement_becomes_a_resumable_run_state(self):
        project = self.api('/api/projects', {'name':'Coder auth', 'repo_path':str(self.repo), 'default_mode':'supervised'})
        task = self.api(f"/api/projects/{project['id']}/tasks", {'text':'Work remotely'})
        submission = self.api(f"/api/projects/{project['id']}/run", {'task_id':task['id']})
        login_url = 'http://127.0.0.1:3000/external-auth/github'
        with patch.object(app, '_run_attempt', side_effect=app.CoderExternalAuthRequired(login_url)):
            app.run_attempt(submission['run_id'], project['id'], task['id'])
        state = self.api(f"/api/tasks/{task['id']}")
        self.assertEqual(state['run']['status'], 'awaiting_external_auth')
        self.assertIn(login_url, state['run']['message'])

    def test_permission_override_survives_quota_handoff(self):
        self.configure_worker('codex', 'quota')
        project = self.api('/api/projects', {'name':'Permissions', 'repo_path':str(self.repo), 'default_mode':'unattended'})
        task = self.api(f"/api/projects/{project['id']}/tasks", {'text':'Work', 'start':True, 'tool_permissions':'auto'})
        state = self.wait(task['id'], 'complete')
        self.assertEqual([a['harness_key'] for a in state['attempts']], ['codex','claude'])
        self.assertEqual([a['tool_permissions'] for a in state['attempts']], ['auto','auto'])

    def test_unsupported_permissions_never_launch(self):
        project = self.api('/api/projects', {'name':'Permissions', 'repo_path':str(self.repo), 'default_mode':'unattended'})
        task = self.api(f"/api/projects/{project['id']}/tasks", {'text':'Work', 'start':True, 'tool_permissions':'ask'})
        state = self.api(f"/api/tasks/{task['id']}")
        self.assertEqual(state['run']['status'], 'blocked')
        self.assertIn('live approval', state['run']['message'])
        self.assertEqual(state['attempts'], [])
        self.assertFalse((self.repo/'feature.txt').exists())

    def test_defaults_for_all_harnesses_and_validation(self):
        for key in app.ADAPTERS:
            self.api('/api/harnesses/'+key, {'tool_permissions':'standard'})
        for key in ('codex','claude','droid'):
            self.api('/api/harnesses/'+key, {'tool_permissions':'auto'})
        with self.assertRaises(urllib.error.HTTPError):
            self.api('/api/harnesses/opencode', {'tool_permissions':'auto'})
        project = self.api('/api/projects', {'name':'Permissions', 'repo_path':str(self.repo)})
        with self.assertRaises(urllib.error.HTTPError):
            self.api(f"/api/projects/{project['id']}/tasks", {'text':'Work', 'tool_permissions':'bypass'})
        self.assertEqual(app.rows('SELECT * FROM tasks'), [])

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, stderr=subprocess.STDOUT, text=True)

    def configure_worker(self, key, mode):
        app.ADAPTERS[key]['runnable'] = True
        app.ADAPTERS[key]['build'] = lambda root, model, prompt: [sys.executable, '-u', str(self.worker), mode]
        app.execute('UPDATE harnesses SET installed=1,enabled=1,billing_confirmed=1 WHERE key=?', (key,))

    def api(self, path, payload=None):
        request = urllib.request.Request(self.url + path, data=json.dumps(payload).encode() if payload is not None else None,
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    def create(self, mode='supervised', verify='git diff --check', location='worktree', auto_failover=True):
        project = self.api('/api/projects', {'name': 'Disposable', 'repo_path': str(self.repo), 'default_mode': mode, 'verify_command': verify, 'execution_mode': location, 'auto_failover': auto_failover})
        task = self.api(f"/api/projects/{project['id']}/tasks", {'text': 'Implement the feature', 'start': True})
        return project['id'], task['id'], task['submission']['run_id']

    def wait(self, task_id, expected):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.api(f'/api/tasks/{task_id}')
            if state['run']['status'] == expected:
                return state
            if state['run']['status'] == 'stopped' and expected != 'stopped':
                self.fail(state['run']['message'])
            time.sleep(.05)
        self.fail(f"Run never reached {expected}: {state['run']}")

    def test_submission_executes_after_review_and_merges(self):
        _, task_id, run_id = self.create()
        self.wait(task_id, 'awaiting_dispatch')
        self.api(f'/api/runs/{run_id}/approve-dispatch', {})
        state = self.wait(task_id, 'awaiting_commit')
        self.assertFalse((self.repo / 'feature.txt').exists())
        attempt_id = state['run']['attempt_id']
        diff = self.api(f'/api/attempts/{attempt_id}/diff')['diff']
        self.assertIn('feature.txt', diff)  # Includes the worker's untracked file.
        self.assertIn('implemented and verified', diff)
        self.assertTrue(any(m['role'] == 'assistant' for m in state['messages']))
        self.api(f'/api/runs/{run_id}/approve-commit', {})
        state = self.wait(task_id, 'complete')
        self.assertEqual((self.repo / 'feature.txt').read_text(), 'implemented and verified\n')
        self.assertEqual(state['task']['status'], 'completed')
        self.assertFalse(Path(state['attempts'][0]['worktree_path']).exists())

    def test_execution_lease_is_created_then_bound_to_the_task_worktree(self):
        _, task_id, run_id = self.create()
        state = self.api(f'/api/tasks/{task_id}')
        self.assertEqual(state['lease']['run_id'], run_id)
        self.assertEqual(state['lease']['backend'], 'local')
        self.assertEqual(state['lease']['state'], 'planned')
        self.assertIsNone(state['lease']['worktree_path'])

        self.api(f'/api/runs/{run_id}/approve-dispatch', {})
        state = self.wait(task_id, 'awaiting_commit')
        self.assertEqual(state['lease']['state'], 'ready')
        self.assertEqual(Path(state['lease']['worktree_path']), Path(state['attempts'][0]['worktree_path']))
        self.assertEqual(state['lease']['base_sha'], state['attempts'][0]['base_sha'])

    def test_unattended_execution_does_not_deadlock(self):
        _, task_id, _ = self.create(mode='unattended')
        self.wait(task_id, 'complete')
        self.assertTrue((self.repo / 'feature.txt').exists())

    def test_verification_failure_is_visible_and_never_merges(self):
        _, task_id, run_id = self.create(verify='exit 1')
        self.api(f'/api/runs/{run_id}/approve-dispatch', {})
        state = self.wait(task_id, 'stopped')
        self.assertIn('Verification failed', state['run']['message'])
        self.assertEqual(state['attempts'][0]['status'], 'verify_failed')
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.base)
        self.assertFalse((self.repo / 'feature.txt').exists())

    def test_quota_resumes_same_task_on_second_worker(self):
        self.configure_worker('codex', 'quota')
        _, task_id, run_id = self.create()
        self.api(f'/api/runs/{run_id}/approve-dispatch', {})
        self.wait(task_id, 'awaiting_resume')
        self.api(f'/api/runs/{run_id}/resume', {})
        state = self.wait(task_id, 'awaiting_commit')
        self.assertEqual([a['harness_key'] for a in state['attempts']], ['codex', 'claude'])
        self.assertEqual(state['attempts'][0]['worktree_path'], state['attempts'][1]['worktree_path'])
        self.api(f'/api/runs/{run_id}/approve-commit', {})
        self.wait(task_id, 'complete')
        self.assertTrue((self.repo / 'checkpoint.txt').exists())
        self.assertTrue((self.repo / 'feature.txt').exists())

    def test_auto_failover_can_be_disabled_for_unattended_projects(self):
        self.configure_worker('codex', 'quota')
        _, task_id, _ = self.create(mode='unattended', auto_failover=False)
        state = self.wait(task_id, 'awaiting_resume')
        self.assertEqual([attempt['harness_key'] for attempt in state['attempts']], ['codex'])

    def test_no_enabled_worker_is_persistently_blocked_then_retryable(self):
        app.execute('UPDATE harnesses SET enabled=0')
        project_id, task_id, _ = self.create()
        state = self.wait(task_id, 'blocked')
        self.assertIn('not in your rotation', state['run']['message'])
        self.assertEqual(state['attempts'], [])
        self.configure_worker('codex', 'success')
        self.api(f'/api/projects/{project_id}/run', {'task_id': task_id})
        self.wait(task_id, 'awaiting_dispatch')

    def test_local_dirty_folder_preserves_existing_edits_and_never_commits(self):
        (self.repo / 'README.md').write_text('Uncommitted user work\n')
        (self.repo / 'personal.txt').write_text('Existing untracked work\n')
        _, task_id, run_id = self.create(location='local')
        self.api(f'/api/runs/{run_id}/approve-dispatch', {})
        state = self.wait(task_id, 'awaiting_review')
        self.assertEqual(Path(state['attempts'][0]['worktree_path']), self.repo)
        self.assertTrue((self.repo / 'feature.txt').exists())
        self.api(f'/api/runs/{run_id}/complete', {})
        self.wait(task_id, 'complete')
        self.assertEqual((self.repo / 'README.md').read_text(), 'Uncommitted user work\n')
        self.assertEqual((self.repo / 'personal.txt').read_text(), 'Existing untracked work\n')
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.base)

    def test_local_handoff_continues_in_same_folder(self):
        self.configure_worker('codex', 'quota')
        _, task_id, _ = self.create(mode='unattended', location='local')
        state = self.wait(task_id, 'complete')
        self.assertEqual([a['harness_key'] for a in state['attempts']], ['codex', 'claude'])
        self.assertTrue(all(Path(a['worktree_path']) == self.repo for a in state['attempts']))
        self.assertTrue((self.repo / 'checkpoint.txt').exists())
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.base)

    def test_local_close_never_deletes_project_files(self):
        _, task_id, run_id = self.create(location='local')
        self.api(f'/api/runs/{run_id}/approve-dispatch', {})
        self.wait(task_id, 'awaiting_review')
        self.api(f'/api/runs/{run_id}/discard', {})
        self.wait(task_id, 'discarded')
        self.assertTrue((self.repo / 'feature.txt').exists())
        self.assertTrue((self.repo / 'README.md').exists())

    def test_plain_folder_without_git_runs(self):
        self.repo = self.root / 'plain-folder'
        self.repo.mkdir()
        _, task_id, _ = self.create(mode='unattended', location='local')
        self.wait(task_id, 'complete')
        self.assertTrue((self.repo / 'feature.txt').exists())

    def test_structured_permission_failure_is_not_success(self):
        command = [sys.executable, '-c', 'print(\'{"type":"result","result":"Edit permission denied","permission_denials":[{"tool_name":"Edit"}]}\')']
        app.ADAPTERS['codex']['build'] = lambda *args: command
        _, task_id, _ = self.create(mode='unattended', location='local')
        state = self.wait(task_id, 'stopped')
        self.assertIn('Edit permission denied', state['run']['message'])
        self.assertEqual(state['task']['status'], 'pending')

    def test_billing_checkbox_no_longer_controls_selection(self):
        app.execute('UPDATE harnesses SET billing_confirmed=0')
        self.assertTrue(app.eligible_harnesses())
        h = self.api('/api/bootstrap')['harnesses'][0]
        self.assertEqual(h['availability']['code'], 'ready')

    def test_two_tasks_cannot_edit_same_project_concurrently(self):
        project_id, task_id, run_id = self.create(location='local')
        second = self.api(f'/api/projects/{project_id}/tasks', {'text': 'Second task', 'start': True})
        self.assertEqual(second['submission']['status'], 'blocked')
        self.assertIn('Another task', second['submission']['message'])

    def test_handoff_keeps_interrupted_harness_progress(self):
        self.configure_worker('codex', 'quota')
        project_id, task_id, run_id = self.create(location='local')
        self.api(f'/api/runs/{run_id}/approve-dispatch', {})
        state = self.wait(task_id, 'awaiting_resume')
        prompt = app.task_prompt(state['task'], app.project_or_404(project_id))
        self.assertIn('Checkpoint saved; continue with the feature implementation.', prompt)
        self.assertIn('codex attempt #', prompt)
        events = self.api(f"/api/attempts/{state['attempts'][0]['id']}/log")['events']
        self.assertTrue(any(e['kind']=='message' for e in events))

    def test_legacy_messages_are_attributed_without_rewriting_history(self):
        _, task_id, run_id = self.create(mode='unattended', location='local')
        self.wait(task_id, 'complete')
        app.execute('UPDATE task_messages SET attempt_id=NULL WHERE task_id=?', (task_id,))
        messages=self.api(f'/api/tasks/{task_id}')['messages']
        self.assertEqual(next(m for m in messages if m['role']=='assistant')['harness_key'],'codex')

    def test_permission_retry_uses_same_harness_and_folder_without_global_change(self):
        app.execute("UPDATE harnesses SET enabled=0 WHERE key='codex'")
        denied = [sys.executable,'-c','print(\'{"type":"result","result":"Build needs approval","permission_denials":[{"tool_name":"Bash","tool_input":{"command":"cargo build"}}]}\')']
        app.ADAPTERS['claude']['build']=lambda *args: denied
        _, task_id, run_id=self.create(mode='unattended',location='local')
        state=self.wait(task_id,'stopped')
        self.assertEqual(state['attempts'][0]['display_status'],'Needs permission')
        self.configure_worker('claude','success')
        self.api(f'/api/runs/{run_id}/retry-permissions',{'mode':'auto'})
        state=self.wait(task_id,'complete')
        self.assertEqual([a['harness_key'] for a in state['attempts']],['claude','claude'])
        self.assertEqual(state['attempts'][0]['worktree_path'],state['attempts'][1]['worktree_path'])
        self.assertEqual(app.one("SELECT tool_permissions FROM harnesses WHERE key='claude'")['tool_permissions'],'standard')
        self.assertEqual(state['run']['permission_override'],'auto')

    def test_permission_mode_persists_and_rejects_bypass(self):
        self.api('/api/harnesses/claude',{'tool_permissions':'auto'})
        self.assertEqual(app.one("SELECT tool_permissions FROM harnesses WHERE key='claude'")['tool_permissions'],'auto')
        with self.assertRaises(urllib.error.HTTPError):
            self.api('/api/harnesses/claude',{'tool_permissions':'bypassPermissions'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
