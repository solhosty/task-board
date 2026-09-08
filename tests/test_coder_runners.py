import tempfile
import unittest
import base64
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import shlex
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import app
from infra.runner import remote_delivery_runner as delivery
from infra.runner import remote_agent_runner as remote_agent
from harness_rotation import remote_transport


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
        again = app.save_coder_runner(self.server, self.owner, self.workspace)
        self.assertEqual(again['id'], self.runner['id'])
        self.assertIsNone(again['detected_max_tasks'])
        self.assertEqual(again['workspace_url'], 'http://localhost:3000/@owner/private-runner')
        migrated = app.save_coder_runner(self.server, self.owner, dict(self.workspace, id='different', name='fresh-runner'))
        self.assertEqual(migrated['workspace_id'], 'different')
        self.assertEqual(migrated['workspace_name'], 'fresh-runner')

    def test_rejects_other_owner_organization_and_shared_workspace(self):
        for fields in [{'owner_id':'other'}, {'organization_name':'other','organization_id':'other'}, {'shared_with':[{'name':'someone'}]}]:
            with self.assertRaises(ValueError):
                app.save_coder_runner(self.server, self.owner, dict(self.workspace, **fields))

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
             patch.object(app, 'coder_json', return_value=self.workspace), \
             patch.object(app, 'refresh_runner_capacity', return_value=self.runner), \
             patch.object(app.subprocess, 'run') as run:
            runner, environment = app.ensure_coder_runner(self.server, {})
        self.assertEqual(runner['id'], self.runner['id'])
        self.assertEqual(environment['CODER_SESSION_TOKEN'], 'secret')
        run.assert_not_called()

    def test_legacy_agent_template_fallback_runner_remains_usable(self):
        legacy_workspace = dict(self.workspace, template_name='harness-hunter-local-rese-3f023b')
        profile = {'template_name': 'agent-template'}
        with patch.object(app, 'coder_runner_context', return_value=('secret', self.owner, self.runner)), \
             patch.object(app, 'coder_json', return_value=legacy_workspace), \
             patch.object(app, 'refresh_runner_capacity', return_value=self.runner), \
             patch.object(app.subprocess, 'run') as run:
            runner, _ = app.ensure_coder_runner(self.server, profile)
        self.assertEqual(runner['id'], self.runner['id'])
        run.assert_not_called()

    def test_unrelated_runner_template_still_requires_migration(self):
        incompatible_workspace = dict(self.workspace, template_name='unreviewed-template')
        with patch.object(app, 'coder_runner_context', return_value=('secret', self.owner, self.runner)), \
             patch.object(app, 'coder_json', return_value=incompatible_workspace):
            with self.assertRaisesRegex(ValueError, 'different template'):
                app.ensure_coder_runner(self.server, {'template_name': 'agent-template'})

    def test_template_compatibility_allows_only_the_recorded_legacy_fallback(self):
        self.assertEqual(app.compatible_runner_template({'template_name': 'agent-template'}, 'agent-template'), True)
        self.assertEqual(app.compatible_runner_template({'template_name': 'agent-template'}, 'harness-hunter-local-rese-3f023b'), True)
        self.assertEqual(app.compatible_runner_template({'template_name': 'agent-template'}, 'unreviewed-template'), False)

    def test_runner_migration_uses_the_project_template_not_legacy_fallback(self):
        created = SimpleNamespace(returncode=0, stdout='', stderr='')
        new_workspace = dict(self.workspace, id='new-workspace', name='new-runner', template_name='agent-template')
        new_runner = dict(self.runner, id=99, workspace_id='new-workspace', workspace_name='new-runner', template_name='agent-template')
        with patch.object(app, 'coder_runner_context', return_value=('secret', self.owner, self.runner)), \
             patch.object(app.subprocess, 'run', return_value=created) as run, \
             patch.object(app, 'coder_json', return_value=new_workspace), \
             patch.object(app, 'save_coder_runner', return_value=new_runner), \
             patch.object(app, 'refresh_runner_capacity', return_value=new_runner):
            result = app.migrate_coder_runner(self.server, {'template_name': 'agent-template'})
        self.assertEqual(result['runner'], new_runner)
        self.assertEqual(run.call_args.args[0][run.call_args.args[0].index('--template') + 1], 'agent-template')

    def test_runner_capacity_is_measured_inside_the_workspace(self):
        measured = {'cpu_count': 4, 'memory_bytes': 8 * 1024 ** 3,
                    'memory_per_task_bytes': 2 * 1024 ** 3, 'max_tasks': 4}
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(measured), stderr='')
        with patch.object(app.subprocess, 'run', return_value=completed) as run:
            refreshed = app.refresh_runner_capacity(self.runner, {'CODER_URL':'http://localhost:3000'}, force=True)
        self.assertEqual((refreshed['detected_cpu_count'], refreshed['detected_memory_bytes'], refreshed['detected_max_tasks']),
                         (4, 8 * 1024 ** 3, 4))
        self.assertIn('MEMORY_PER_TASK', run.call_args.kwargs['input'])

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

    def test_account_binding_is_runner_scoped_and_never_stores_a_token(self):
        binding = app.execute('''INSERT INTO runner_account_bindings(
            runner_id,provider,label,account_email,plan_type,priority,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)''', (self.runner['id'], 'codex', 'codex-one',
            'one@example.test', 'pro', 0, app.now(), app.now()))
        saved = app.runner_account_bindings(1)
        self.assertEqual(saved[0]['id'], binding)
        self.assertEqual(saved[0]['workspace_name'], self.runner['workspace_name'])
        self.assertNotIn('token', saved[0])

    def test_runner_cannot_host_two_provider_accounts(self):
        values = (self.runner['id'], 'codex', 'first', 0, app.now(), app.now())
        app.execute('''INSERT INTO runner_account_bindings(runner_id,provider,label,priority,created_at,updated_at)
                       VALUES(?,?,?,?,?,?)''', values)
        with self.assertRaises(Exception):
            app.execute('''INSERT INTO runner_account_bindings(runner_id,provider,label,priority,created_at,updated_at)
                           VALUES(?,?,?,?,?,?)''', (self.runner['id'], 'claude', 'second', 1, app.now(), app.now()))

    def test_legacy_task_workspace_is_not_silently_abandoned(self):
        task, run_id = self.task_run()
        app.execute("UPDATE execution_leases SET workspace_name='old-task-container' WHERE run_id=?", (run_id,))
        profile = {'coder_server_id':1, 'repo_url':'https://example.com/repo'}
        with patch.object(app, 'project_coder_profile', return_value=profile), patch.object(app, 'ensure_coder_runner') as ensure:
            with self.assertRaisesRegex(ValueError, 'legacy'):
                app.provision_coder_execution('new-run', {'id':self.project}, task)
        ensure.assert_not_called()

    def test_missing_remote_codex_never_starts_an_auth_bridge(self):
        result = subprocess_result(returncode=1)
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
            command = app.remote_agent_request({'key':'codex', 'model':'default'}, worktree, task, {'verify_command':'true'}, 'standard', screenshot_path='/home/coder/.harness-runner/results/task-a/1/after.png')
        encoded = command.split()[-1].strip("'")
        request = json.loads(base64.urlsafe_b64decode(encoded.encode()).decode())
        self.assertEqual(request['prompt'], 'quote; $(not-a-command)')
        self.assertEqual(request['screenshot_path'], '/home/coder/.harness-runner/results/task-a/1/after.png')
        self.assertNotIn('quote;', command)

    def test_remote_attachments_are_staged_then_referenced_by_codex_and_claude(self):
        source = Path(self.temp.name) / 'screen.png'
        source.write_bytes(b'png-bytes')
        record = {'id': 9, 'filename': 'screen.png', 'media_type': 'image/png', 'kind': 'image',
                  'byte_size': source.stat().st_size, 'sha256': __import__('hashlib').sha256(source.read_bytes()).hexdigest(),
                  'stored_path': str(source)}
        remote = '/home/coder/.harness-runner/attachments/task-a/' + record['sha256'][:12] + '-screen.png'
        completed = SimpleNamespace(returncode=0, stdout=json.dumps([dict(record, stored_path=remote)]), stderr='')
        with patch.object(remote_transport.subprocess, 'run', return_value=completed) as transfer:
            staged = remote_transport.stage_attachments(self.runner, {}, 'task-a', [record], app.APP_ROOT)
        self.assertEqual(staged[0]['stored_path'], remote)
        sent = json.loads(transfer.call_args.kwargs['input'])
        self.assertEqual(sent[0]['sha256'], record['sha256'])
        remote_command = transfer.call_args.args[0][-1]
        self.assertEqual(shlex.split(remote_command)[:2], ['python3', '-c'])
        codex = remote_agent.command_for({'harness': 'codex', 'prompt': 'read it', 'attachments': staged}, Path('/tmp/work'))
        self.assertEqual(codex[codex.index('--image') + 1], remote)
        claude = remote_agent.command_for({'harness': 'claude', 'prompt': 'read it', 'attachments': staged}, Path('/tmp/work'))
        self.assertEqual(claude[claude.index('--add-dir') + 1], str(Path(remote).parent))

    def test_remote_attachment_staging_refuses_a_changed_local_file(self):
        source = Path(self.temp.name) / 'notes.md'
        source.write_text('old')
        record = {'id': 1, 'filename': 'notes.md', 'media_type': 'text/markdown', 'kind': 'file',
                  'byte_size': 3, 'sha256': '0' * 64, 'stored_path': str(source)}
        with self.assertRaisesRegex(RuntimeError, 'changed or is missing'):
            remote_transport.stage_attachments(self.runner, {}, 'task-a', [record], app.APP_ROOT)

    def test_missing_attachment_record_fails_before_remote_execution(self):
        task, _ = self.task_run()
        session_id = app.execute("INSERT INTO task_sessions(task_id,session_number,status,opened_at) VALUES(?,1,'active',?)",
                                 (task['id'], app.now()))
        app.execute("""INSERT INTO task_attachments(task_id,session_id,filename,media_type,kind,byte_size,sha256,stored_path,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?)""",
                    (task['id'], session_id, 'gone.png', 'image/png', 'image', 1, '0' * 64,
                     str(Path(self.temp.name) / 'gone.png'), app.now()))
        with self.assertRaisesRegex(RuntimeError, 'no longer stored locally'):
            app.stage_remote_attachments(self.runner, {}, {'task_key': 'task-a'}, task['id'])

    def test_remote_harness_falls_back_to_other_authenticated_cli(self):
        app.execute("UPDATE harnesses SET installed=1,enabled=1 WHERE key IN ('codex','claude')")
        task = {'preferred_harness':'codex', 'preferred_model':None}
        with patch.object(app, 'remote_claude_account', return_value={'installed':True, 'authenticated':True}) as claude:
            selected, selection = app.choose_remote_harness(task, self.runner, {}, ('codex',))
        self.assertEqual(selected['key'], 'claude')
        self.assertEqual(selection, 'remote fallback')
        claude.assert_called_once_with(self.runner, {})

    def test_remote_harness_skips_quota_cooldowns(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec='seconds')
        app.execute("UPDATE harnesses SET installed=1,enabled=1,cooldown_until=? WHERE key IN ('codex','claude')", (future,))
        selected, selection = app.choose_remote_harness({'preferred_harness':'codex', 'preferred_model':None}, self.runner, {})
        self.assertIsNone(selected)
        self.assertEqual(selection, 'remote cooldown')

    def test_bound_second_codex_account_ignores_first_accounts_global_cooldown(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec='seconds')
        app.execute("UPDATE harnesses SET installed=1,enabled=1,cooldown_until=? WHERE key='codex'", (future,))
        app.execute('''INSERT INTO runner_account_bindings(runner_id,provider,label,priority,created_at,updated_at)
                       VALUES(?,'codex','codex-two',1,?,?)''', (self.runner['id'], app.now(), app.now()))
        with patch.object(app, 'remote_codex_account', return_value={'installed':True, 'authenticated':True}):
            selected, selection = app.choose_remote_harness({'preferred_harness':'codex', 'preferred_model':None}, self.runner, {})
        self.assertEqual(selected['key'], 'codex')
        self.assertEqual(selection, 'remote preferred')

    def test_pool_failover_selects_second_account_of_same_provider(self):
        second = app.save_coder_runner(self.server, self.owner, dict(self.workspace, id='workspace-two', name='second-runner'))
        app.execute('''INSERT INTO runner_account_bindings(runner_id,provider,label,priority,created_at,updated_at)
                       VALUES(?,'codex','codex-one',0,?,?)''', (self.runner['id'], app.now(), app.now()))
        app.execute('''INSERT INTO runner_account_bindings(runner_id,provider,label,priority,created_at,updated_at)
                       VALUES(?,'codex','codex-two',1,?,?)''', (second['id'], app.now(), app.now()))
        selected = app.pool_runner_for_failover({'project_id':self.project}, self.server, self.runner['id'])
        self.assertEqual(selected['runner_id'], second['id'])

    def test_pool_routes_a_new_or_legacy_unbound_task_to_an_account(self):
        second = app.save_coder_runner(self.server, self.owner, dict(self.workspace, id='workspace-two', name='second-runner'))
        app.execute('''INSERT INTO runner_account_bindings(runner_id,provider,label,priority,created_at,updated_at)
                       VALUES(?,'codex','codex-two',0,?,?)''', (second['id'], app.now(), app.now()))
        selected = app.pool_runner_for_task({'project_id':self.project}, self.server)
        self.assertEqual(selected['runner_id'], second['id'])

    def test_remote_all_quota_cooldowns_pause_without_starting_another_attempt(self):
        task, run_id = self.task_run()
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec='seconds')
        app.execute("UPDATE harnesses SET installed=1,enabled=1,cooldown_until=? WHERE key IN ('codex','claude')", (future,))
        worktree = {'worktree_path':'/home/coder/.harness-runner/tasks/task-preserved',
                    'branch_name':'harness/task-preserved', 'base_sha':'a' * 40}
        with patch.object(app, 'effective_execution_backend', return_value=('coder', {})), \
             patch.object(app, 'provision_coder_execution', return_value={'runner':self.runner, 'environment':{}, 'worktree':worktree}):
            app._run_attempt(run_id, self.project, task['id'])
        run = app.one('SELECT * FROM runs WHERE id=?', (run_id,))
        self.assertEqual(run['status'], 'paused_cooldown')
        self.assertIn('No eligible account in the Coder pool', run['message'])
        self.assertEqual(app.rows('SELECT * FROM attempts WHERE run_id=?', (run_id,)), [])

    def test_remote_quota_preserves_worktree_and_queues_handoff(self):
        task, run_id = self.task_run()
        app.execute("UPDATE projects SET default_mode='unattended',auto_failover=1 WHERE id=?", (self.project,))
        worktree = {'worktree_path':'/home/coder/.harness-runner/tasks/task-preserved',
                    'branch_name':'harness/task-preserved', 'base_sha':'a' * 40}
        codex = app.one("SELECT * FROM harnesses WHERE key='codex'")
        result = {'code':1, 'output':'Usage limit reached; resets in 1 hour.',
                  'timed_out':False, 'verify_code':None, 'verification':'', 'diff':'checkpoint'}
        with patch.object(app, 'effective_execution_backend', return_value=('coder', {})), \
             patch.object(app, 'provision_coder_execution', return_value={'runner':self.runner, 'environment':{}, 'worktree':worktree}), \
             patch.object(app, 'choose_remote_harness', return_value=(codex, 'remote preferred')), \
             patch.object(app, 'remote_agent_request', return_value='remote-command'), \
             patch.object(app, 'run_remote_agent', return_value=(result, result['output'])), \
             patch.object(app.threading, 'Thread') as thread:
            app._run_attempt(run_id, self.project, task['id'])
        attempt = app.one('SELECT * FROM attempts WHERE run_id=?', (run_id,))
        run = app.one('SELECT * FROM runs WHERE id=?', (run_id,))
        self.assertEqual(attempt['status'], 'quota')
        self.assertEqual(attempt['worktree_path'], worktree['worktree_path'])
        self.assertEqual(run['status'], 'rotating')
        self.assertIn('next eligible account', run['message'])
        self.assertIsNotNone(app.one("SELECT cooldown_until FROM harnesses WHERE key='codex'")['cooldown_until'])
        thread.return_value.start.assert_called_once_with()

    def test_detected_runner_capacity_allows_parallel_remote_dispatch(self):
        app.execute("UPDATE coder_runners SET detected_max_tasks=2 WHERE id=?", (self.runner['id'],))
        app.execute("UPDATE projects SET default_mode='unattended' WHERE id=?", (self.project,))
        app.execute("""INSERT INTO project_coder_profiles(project_id,coder_server_id,repo_url,base_ref,template_name,enabled,default_target,created_at,updated_at)
            VALUES(?,1,'https://github.com/example/repository.git','main','base',1,'coder',?,?)""",
                    (self.project, app.now(), app.now()))
        first = app.execute("INSERT INTO tasks(project_id,text,task_order,created_at) VALUES(?,'first',1,?)", (self.project, app.now()))
        second = app.execute("INSERT INTO tasks(project_id,text,task_order,created_at) VALUES(?,'second',2,?)", (self.project, app.now()))
        third = app.execute("INSERT INTO tasks(project_id,text,task_order,created_at) VALUES(?,'third',3,?)", (self.project, app.now()))
        harness = app.one("SELECT * FROM harnesses WHERE key='codex'")
        with patch.object(app, 'execution_blockers', return_value=[]), \
             patch.object(app, 'choose_harness', return_value=(harness, 'rotation')), \
             patch.object(app.threading, 'Thread') as thread:
            first_run = app.request_run(self.project, first)
            second_run = app.request_run(self.project, second)
            third_run = app.request_run(self.project, third)
            self.assertEqual((first_run['status'], second_run['status'], third_run['status']), ('queued', 'queued', 'awaiting_capacity'))
            self.assertIn('Waiting for the next slot', third_run['message'])
            self.assertEqual(thread.return_value.start.call_count, 2)
            app.update_run(first_run['run_id'], 'complete', 'done')
            self.assertEqual(app.one('SELECT status FROM runs WHERE id=?', (third_run['run_id'],))['status'], 'queued')
            self.assertEqual(thread.return_value.start.call_count, 3)

    def test_reviewed_remote_attempt_commits_pushes_and_links_pr(self):
        task, run_id = self.task_run()
        app.execute("""INSERT INTO project_coder_profiles(project_id,coder_server_id,repo_url,base_ref,template_name,created_at,updated_at)
            VALUES(?,1,'https://github.com/example/repository.git','main','base',?,?)""", (self.project, app.now(), app.now()))
        worktree = {'worktree_path':'/home/coder/.harness-runner/tasks/task-delivery',
                    'branch_name':'harness/task-delivery', 'base_sha':'a' * 40}
        attempt_id = app.execute("""INSERT INTO attempts(task_id,status,started_at,worktree_path,branch_name,base_sha,run_id)
            VALUES(?,'verified',?,?,?,?,?)""", (task['id'], app.now(), worktree['worktree_path'], worktree['branch_name'], worktree['base_sha'], run_id))
        app.execute("UPDATE attempts SET error='previous push error' WHERE id=?", (attempt_id,))
        app.execute('UPDATE runs SET attempt_id=?,status=\'committing\' WHERE id=?', (attempt_id, run_id))
        result = {'commit_sha':'b' * 40, 'head_sha':'b' * 40, 'branch_name':worktree['branch_name'],
                  'diff':'diff --git a/file b/file', 'diff_stat':' file | 1 +',
                  'pr_url':'https://github.com/example/repository/pull/7', 'pr_number':'7'}
        with patch.object(app, 'provision_coder_execution', return_value={'runner':self.runner, 'environment':{}, 'worktree':worktree}), \
             patch.object(app, 'run_remote_delivery', return_value=result) as deliver:
            app.finish_commit(run_id, self.project, task['id'], attempt_id)
        self.assertEqual(app.one('SELECT * FROM runs WHERE id=?', (run_id,))['status'], 'complete')
        self.assertEqual(app.one('SELECT * FROM attempts WHERE id=?', (attempt_id,))['commit_sha'], result['commit_sha'])
        self.assertIsNone(app.one('SELECT * FROM attempts WHERE id=?', (attempt_id,))['error'])
        linked = app.one('SELECT * FROM task_pull_requests WHERE task_id=?', (task['id'],))
        self.assertEqual((linked['url'], linked['number'], linked['branch_name']), (result['pr_url'], '7', worktree['branch_name']))
        deliver.assert_called_once()

    def test_remote_pull_request_sync_uses_runner_external_auth(self):
        task, _ = self.task_run()
        app.execute("""INSERT INTO project_coder_profiles(project_id,coder_server_id,repo_url,base_ref,template_name,created_at,updated_at)
            VALUES(?,1,'https://github.com/example/repository.git','main','base',?,?)""", (self.project, app.now(), app.now()))
        app.execute("""INSERT INTO coder_task_worktrees(task_id,runner_id,task_key,repo_url,created_at,updated_at)
            VALUES(?,?,?,'https://github.com/example/repository.git',?,?)""",
                    (task['id'], self.runner['id'], 'task-pr-status', app.now(), app.now()))
        pull_request = {'task_id':task['id'], 'provider':'github',
                        'url':'https://github.com/example/repository/pull/7'}
        remote_status = {'url':pull_request['url'], 'number':'7', 'branch_name':'harness/task-pr-status',
                         'head_sha':'b' * 40, 'state':'closed', 'merged_at':None}
        with patch.object(app, 'ensure_coder_runner', return_value=(self.runner, {})) as ensure, \
             patch.object(app, 'run_remote_pr_status', return_value=remote_status) as status:
            result = app.pull_request_status({'id':self.project}, pull_request)
        self.assertEqual((result['state'], result['review_state'], result['head_sha']), ('closed', 'unknown', remote_status['head_sha']))
        ensure.assert_called_once()
        status.assert_called_once()

    def test_remote_delivery_uses_coder_git_auth_for_push(self):
        with tempfile.TemporaryDirectory(prefix='harness-delivery-') as directory:
            root, worktree = Path(directory).resolve(), Path(directory).resolve() / 'task-delivery'
            worktree.mkdir()
            request = {'worktree_path':str(worktree), 'branch_name':'harness/task-a', 'base_sha':'a' * 40,
                       'repo_url':'https://github.com/example/repository.git', 'base_ref':'main',
                       'auth_provider_id':'github', 'title':'deliver a safe change'}
            encoded = base64.urlsafe_b64encode(json.dumps(request).encode()).decode()
            events = []

            def fake_run(command, cwd, check=True, env=None):
                if command[:3] == ['git', 'diff', '--cached']:
                    return SimpleNamespace(returncode=1, stdout='')
                if command[:3] == ['git', 'rev-parse', 'HEAD']:
                    return SimpleNamespace(returncode=0, stdout='b' * 40 + '\n')
                if 'push' in command:
                    events.append(command)
                return SimpleNamespace(returncode=0, stdout='')

            output = io.StringIO()
            with patch.object(delivery, 'ROOT', root), patch.object(delivery, 'run', side_effect=fake_run), \
                 patch.object(delivery, 'github_token', return_value='credential-not-printed'), \
                 patch.object(delivery, 'github_json', side_effect=[{'id':123, 'login':'hunter'}, [], {'html_url':'https://github.com/example/repository/pull/7', 'number':7}]), \
                 redirect_stdout(output):
                delivery.main(encoded)
            self.assertEqual(events, [['git', 'push', '--force-with-lease', '--set-upstream', 'origin', 'harness/task-a']])
            self.assertIn(delivery.MARKER, output.getvalue())
            self.assertNotIn('credential-not-printed', output.getvalue())

    def test_remote_delivery_recovers_an_existing_unpushed_commit(self):
        with tempfile.TemporaryDirectory(prefix='harness-delivery-') as directory:
            root, worktree = Path(directory).resolve(), Path(directory).resolve() / 'task-delivery'
            worktree.mkdir()
            request = {'worktree_path':str(worktree), 'branch_name':'harness/task-a', 'base_sha':'a' * 40,
                       'repo_url':'https://github.com/example/repository.git', 'base_ref':'main',
                       'auth_provider_id':'github', 'title':'deliver a safe change'}
            encoded = base64.urlsafe_b64encode(json.dumps(request).encode()).decode()
            commands = []

            def fake_run(command, cwd, check=True, env=None):
                commands.append(command)
                if command[:3] == ['git', 'diff', '--cached']:
                    return SimpleNamespace(returncode=0, stdout='')
                if command[:3] == ['git', 'rev-parse', 'HEAD']:
                    return SimpleNamespace(returncode=0, stdout='b' * 40 + '\n')
                if command[:3] == ['git', 'show', '-s']:
                    return SimpleNamespace(returncode=0, stdout='private@example.com\nprivate@example.com\n')
                return SimpleNamespace(returncode=0, stdout='')

            output = io.StringIO()
            with patch.object(delivery, 'ROOT', root), patch.object(delivery, 'run', side_effect=fake_run), \
                 patch.object(delivery, 'github_token', return_value='credential-not-printed'), \
                 patch.object(delivery, 'github_json', side_effect=[{'id':123, 'login':'hunter'}, [], {'html_url':'https://github.com/example/repository/pull/7', 'number':7}]), \
                 redirect_stdout(output):
                delivery.main(encoded)
            self.assertTrue(any(command[:3] == ['git', 'commit', '--amend'] for command in commands))
            self.assertIn(['git', 'push', '--force-with-lease', '--set-upstream', 'origin', 'harness/task-a'], commands)

    def test_retry_remote_delivery_reuses_only_a_failed_delivery_attempt(self):
        task, run_id = self.task_run(status='stopped')
        attempt_id = app.execute("""INSERT INTO attempts(task_id,status,started_at,worktree_path,run_id)
            VALUES(?,'merge_failed',?,?,?)""", (task['id'], app.now(), '/home/coder/.harness-runner/tasks/task-retry', run_id))
        app.execute('UPDATE runs SET attempt_id=? WHERE id=?', (attempt_id, run_id))
        with patch.object(app.threading, 'Thread') as thread:
            app.retry_remote_delivery(run_id)
        self.assertEqual(app.one('SELECT * FROM runs WHERE id=?', (run_id,))['status'], 'committing')
        thread.assert_called_once_with(target=app.finish_commit, args=(run_id, self.project, task['id'], attempt_id), daemon=True)
        thread.return_value.start.assert_called_once_with()

    def test_claude_login_input_is_forwarded_only_to_active_memory_bridge(self):
        bridge = MagicMock()
        bridge.snapshot.return_value = 'waiting for code'
        key = (self.server['id'], self.runner['workspace_id'], 'claude')
        app.MODEL_AUTH_FLOWS[key] = {'status':'pending', 'provider':'claude', 'message':'waiting', 'bridge':bridge, 'started_at':0}
        result = app.send_remote_claude_login_input(self.server['id'], self.runner['workspace_id'], 'one-time-code')
        bridge.send.assert_called_once_with('one-time-code')
        self.assertEqual(result['screen'], 'waiting for code')
        app.MODEL_AUTH_FLOWS.pop(key, None)

    def test_claude_login_input_replays_characters_then_terminal_enter(self):
        bridge = object.__new__(app.RemoteClaudeLogin)
        bridge.process = MagicMock()
        bridge.process.poll.return_value = None
        bridge.process.stdin = MagicMock()
        bridge.send('one-time-code')
        expected = [call(character.encode()) for character in 'one-time-code'] + [call(b'\r')]
        self.assertEqual(bridge.process.stdin.write.call_args_list, expected)
        self.assertEqual(bridge.process.stdin.flush.call_count, len(expected))

    def test_claude_login_screen_removes_terminal_control_sequences(self):
        bridge = object.__new__(app.RemoteClaudeLogin)
        bridge.lock = threading.RLock()
        bridge.screen = '\x1b[?25hWelcome\x1b[0m\r\nhttps://example.test/login\x1b]0;title\x07'
        self.assertEqual(bridge.snapshot(), 'Welcome\nhttps://example.test/login')

    def test_claude_theme_matcher_handles_terminal_redraw_without_spaces(self):
        self.assertIsNotNone(app.re.search(r'choose\s*the\s*text\s*style|choosethetextstyle', 'Choosethetextstylethatlooksbest', app.re.I))

    def test_claude_login_watcher_marks_successful_terminal_session_complete(self):
        bridge = MagicMock()
        bridge.snapshot.return_value = 'Logged in as person@example.com\nLogin successful.'
        flow = {'status': 'pending', 'provider': 'claude', 'message': 'waiting', 'bridge': bridge, 'started_at': app.time.time()}
        app.watch_remote_claude_login((self.server['id'], self.runner['workspace_id'], 'claude'), flow)
        self.assertEqual(flow['status'], 'complete')
        bridge.close.assert_called_once_with()

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
