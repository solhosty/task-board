import tempfile
import unittest
import base64
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import app


class CoderRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='harness-runner-db-')
        self.original = app.DATA_ROOT, app.DB_PATH
        app.DATA_ROOT = Path(self.temp.name)
        app.DB_PATH = app.DATA_ROOT / 'state.sqlite3'
        app.init_db()
        app.execute("INSERT INTO coder_servers(id,name,base_url,organization,created_at,updated_at) VALUES(1,'test','http://localhost:3000','default',?,?)", (app.now(), app.now()))
        self.server = app.coder_server_or_404(1)
        self.owner = {'id': 'owner-id', 'username': 'owner'}
        self.workspace = {'id':'workspace-id', 'name':'private-runner', 'owner_id':'owner-id',
                          'organization_name':'default', 'organization_id':'org-id', 'template_name':'base'}
        self.runner = app.save_coder_runner(self.server, self.owner, self.workspace)
        self.project = app.execute("INSERT INTO projects(name,repo_path,verify_command,default_mode,created_at) VALUES('test','/tmp','true','supervised',?)", (app.now(),))

    def tearDown(self):
        app.DATA_ROOT, app.DB_PATH = self.original
        self.temp.cleanup()

    def task_run(self, index=1, status='queued'):
        task_id = app.execute("INSERT INTO tasks(project_id,text,task_order,created_at) VALUES(?,'work',?,?)", (self.project, index, app.now()))
        run_id = 'run-' + str(index)
        app.execute("INSERT INTO runs(id,project_id,task_id,mode,status,created_at,updated_at) VALUES(?,?,?,'supervised',?,?,?)", (run_id, self.project, task_id, status, app.now(), app.now()))
        app.create_execution_lease(run_id, task_id, 'coder')
        return {'id':task_id}, run_id

    def test_binding_is_stable_and_survives_database_reinit(self):
        app.init_db()
        again = app.save_coder_runner(self.server, self.owner, self.workspace, 2)
        self.assertEqual(again['id'], self.runner['id'])
        self.assertEqual(again['max_tasks'], 2)
        self.assertEqual(again['workspace_url'], 'http://localhost:3000/@owner/private-runner')
        with self.assertRaisesRegex(ValueError, 'migration'):
            app.save_coder_runner(self.server, self.owner, dict(self.workspace, id='different'))
        migrated = app.save_coder_runner(self.server, self.owner, dict(self.workspace, id='different', name='fresh-runner'), allow_migration=True)
        self.assertEqual(migrated['workspace_id'], 'different')
        self.assertEqual(migrated['workspace_name'], 'fresh-runner')

    def test_rejects_other_owner_organization_and_shared_workspace(self):
        for fields in [{'owner_id':'other'}, {'organization_name':'other','organization_id':'other'}, {'shared_with':[{'name':'someone'}]}]:
            with self.assertRaises(ValueError):
                app.save_coder_runner(self.server, self.owner, dict(self.workspace, **fields))
        for limit in [0, 9, True, 1.5, '2']:
            with self.assertRaises(ValueError):
                app.save_coder_runner(self.server, self.owner, self.workspace, limit)

    def test_token_owner_or_deployment_change_does_not_inherit_runner(self):
        orgs = {'value':[{'id':'org-id', 'name':'coder', 'is_default':True}]}
        with patch.object(app, 'read_coder_token', return_value='secret'), patch.object(app, 'coder_json', side_effect=[{'id':'other-owner'}, orgs]):
            self.assertIsNone(app.coder_runner_context(self.server)[2])
        with patch.object(app, 'read_coder_token', return_value='secret'), patch.object(app, 'coder_json', side_effect=[self.owner, orgs]):
            self.assertIsNone(app.coder_runner_context(dict(self.server, base_url='https://other.example'))[2])

    def test_default_organization_alias_is_resolved_to_id(self):
        orgs = {'value':[{'id':'org-id', 'name':'coder', 'is_default':True}]}
        with patch.object(app, 'read_coder_token', return_value='secret'), patch.object(app, 'coder_json', side_effect=[dict(self.owner), orgs]):
            _, owner, _ = app.coder_runner_context(self.server)
        app.validate_runner_workspace(self.server, owner, dict(self.workspace, organization_name='coder'))

    def test_existing_runner_is_reused_without_create(self):
        with patch.object(app, 'coder_runner_context', return_value=('secret', self.owner, self.runner)), \
             patch.object(app, 'coder_json', return_value=self.workspace), patch.object(app.subprocess, 'run') as run:
            runner, environment = app.ensure_coder_runner(self.server, {})
        self.assertEqual(runner['id'], self.runner['id'])
        self.assertEqual(environment['CODER_SESSION_TOKEN'], 'secret')
        run.assert_not_called()

    def test_capacity_released_by_terminal_run_but_worktree_retained(self):
        task, run_id = self.task_run()
        saved = app.reserve_runner_worktree(run_id, task, self.runner, 'https://example.com/repo')
        self.assertEqual(app.reserve_runner_worktree(run_id, task, self.runner, 'https://example.com/repo'), saved)
        second, second_run = self.task_run(2)
        with self.assertRaisesRegex(ValueError, 'capacity'):
            app.reserve_runner_worktree(second_run, second, self.runner, 'https://example.com/repo')
        app.update_run(run_id, 'stopped', 'done')
        other = app.reserve_runner_worktree(second_run, second, self.runner, 'https://example.com/repo')
        self.assertNotEqual(saved['task_key'], other['task_key'])
        self.assertEqual(app.rows('SELECT * FROM coder_task_worktrees')[0]['task_key'], saved['task_key'])

    def test_worktree_cannot_switch_repository(self):
        task, run_id = self.task_run()
        app.reserve_runner_worktree(run_id, task, self.runner, 'https://example.com/repo')
        with self.assertRaisesRegex(ValueError, 'preserved'):
            app.reserve_runner_worktree(run_id, task, self.runner, 'https://example.com/other')

    def test_legacy_task_workspace_is_not_silently_abandoned(self):
        task, run_id = self.task_run()
        app.execute("UPDATE execution_leases SET workspace_name='old-task-container' WHERE run_id=?", (run_id,))
        profile = {'coder_server_id':1, 'repo_url':'https://example.com/repo'}
        with patch.object(app, 'project_coder_profile', return_value=profile), patch.object(app, 'ensure_coder_runner') as ensure:
            with self.assertRaisesRegex(ValueError, 'legacy'):
                app.provision_coder_execution('new-run', {'id':self.project}, task)
        ensure.assert_not_called()

    def test_missing_remote_codex_never_starts_an_auth_bridge(self):
        result = subprocess_result(stdout='HARNESS_MISSING')
        with patch.object(app.subprocess, 'run', return_value=result), \
             patch.object(app, 'RemoteCodexAppServer') as bridge:
            status = app.remote_codex_account(self.runner, {'CODER_URL':'http://localhost:3000'})
        self.assertFalse(status['installed'])
        self.assertFalse(status['authenticated'])
        bridge.assert_not_called()

    def test_remote_agent_request_keeps_prompt_out_of_the_shell_command(self):
        task = {'text': 'work', 'id': 1, 'preferred_harness': None, 'preferred_model': None}
        worktree = {'worktree_path': '/home/coder/.harness-runner/tasks/task-a', 'base_sha': 'a' * 40, 'branch_name': 'harness/task-a'}
        with patch.object(app, 'task_prompt', return_value='quote; $(not-a-command)'):
            command = app.remote_agent_request({'key':'codex', 'model':'default'}, worktree, task, {'verify_command':'true'}, 'standard')
        encoded = command.split()[-1].strip("'")
        request = json.loads(base64.urlsafe_b64decode(encoded.encode()).decode())
        self.assertEqual(request['prompt'], 'quote; $(not-a-command)')
        self.assertNotIn('quote;', command)

    def test_claude_login_input_is_forwarded_only_to_active_memory_bridge(self):
        bridge = MagicMock()
        bridge.snapshot.return_value = 'waiting for code'
        key = (self.server['id'], self.runner['workspace_id'], 'claude')
        app.MODEL_AUTH_FLOWS[key] = {'status':'pending', 'provider':'claude', 'message':'waiting', 'bridge':bridge, 'started_at':0}
        result = app.send_remote_claude_login_input(self.server['id'], self.runner['workspace_id'], 'one-time-code')
        bridge.send.assert_called_once_with('one-time-code')
        self.assertEqual(result['screen'], 'waiting for code')
        app.MODEL_AUTH_FLOWS.pop(key, None)

    def test_claude_login_screen_removes_terminal_control_sequences(self):
        bridge = object.__new__(app.RemoteClaudeLogin)
        bridge.lock = threading.RLock()
        bridge.screen = '\x1b[?25hWelcome\x1b[0m\r\nhttps://example.test/login\x1b]0;title\x07'
        self.assertEqual(bridge.snapshot(), 'Welcome\nhttps://example.test/login')

    def test_claude_theme_matcher_handles_terminal_redraw_without_spaces(self):
        self.assertIsNotNone(app.re.search(r'choose\s*the\s*text\s*style|choosethetextstyle', 'Choosethetextstylethatlooksbest', app.re.I))

    def test_claude_onboarding_bridge_selects_default_theme(self):
        bridge = MagicMock()
        bridge.process.poll.return_value = None
        bridge.snapshot.return_value = 'Choosethetextstylethatlooksbest'
        flow = {'status':'pending', 'bridge':bridge}
        app.advance_remote_claude_onboarding(flow)
        bridge.accept_default.assert_called_once_with()
        self.assertEqual(flow['message'], 'Preparing Claude Code sign-in.')

    def test_claude_onboarding_selects_default_subscription_login(self):
        bridge = MagicMock()
        bridge.process.poll.return_value = None
        bridge.snapshot.return_value = 'ClaudeCodecanbeusedwithyourClaudesubscription.Selectloginmethod:'
        flow = {'status':'pending', 'bridge':bridge}
        app.advance_remote_claude_login_method(flow)
        bridge.accept_default.assert_called_once_with()
        self.assertEqual(flow['message'], 'Opening Claude browser sign-in.')

    def test_claude_login_extracts_a_wrapped_claude_url(self):
        bridge = object.__new__(app.RemoteClaudeLogin)
        bridge.lock = threading.RLock()
        bridge.screen = 'Browserdidntopen\n\nhttps://claude.ai/oauth/authorize?state=abc&code_\nchallenge=def\n\nPastecodehere'
        self.assertEqual(bridge.verification_url(), 'https://claude.ai/oauth/authorize?state=abc&code_challenge=def')

    def test_claude_login_stops_wrapped_url_before_return_code_prompt(self):
        bridge = object.__new__(app.RemoteClaudeLogin)
        bridge.lock = threading.RLock()
        bridge.screen = 'https://platform.claude.com/oauth/authorize?state=abc\n&code_challenge=def\nPastecodehereifprompted'
        self.assertEqual(bridge.verification_url(), 'https://platform.claude.com/oauth/authorize?state=abc&code_challenge=def')


def subprocess_result(stdout='', returncode=0, stderr=''):
    class Result:
        pass
    result = Result()
    result.stdout, result.returncode, result.stderr = stdout, returncode, stderr
    return result
