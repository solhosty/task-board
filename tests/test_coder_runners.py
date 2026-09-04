import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


def subprocess_result(stdout='', returncode=0, stderr=''):
    class Result:
        pass
    result = Result()
    result.stdout, result.returncode, result.stderr = stdout, returncode, stderr
    return result
