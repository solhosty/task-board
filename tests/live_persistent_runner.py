"""Opt-in integration probe using a pre-existing disposable Coder workspace.

Uses temporary local DB state, leaves two diagnostic worktrees remotely, and never
starts a model login, reads model tokens, invokes inference, or creates a workspace.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server-id', type=int, required=True)
    parser.add_argument('--project-id', type=int, required=True)
    parser.add_argument('--workspace', required=True)
    args = parser.parse_args()
    server = app.coder_server_or_404(args.server_id)
    profile = app.project_coder_profile(args.project_id)
    token, owner, _ = app.coder_runner_context(server)
    workspace = app.coder_json(server['base_url'], '/api/v2/users/me/workspace/' + app.quote(args.workspace, safe=''), token)
    original = app.DATA_ROOT, app.DB_PATH
    with tempfile.TemporaryDirectory(prefix='harness-live-runner-') as scratch:
        try:
            app.DATA_ROOT = Path(scratch)
            app.DB_PATH = app.DATA_ROOT / 'state.sqlite3'
            app.init_db()
            app.execute('''INSERT INTO coder_servers(id,name,base_url,organization,status,token_configured,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)''', (server['id'], server['name'], server['base_url'], server['organization'],
                                           server['status'], server['token_configured'], app.now(), app.now()))
            runner = app.save_coder_runner(server, owner, workspace)
            project_id = app.execute("INSERT INTO projects(name,repo_path,verify_command,default_mode,created_at) VALUES('Runner probe','/tmp','true','supervised',?)", (app.now(),))
            app.execute('''INSERT INTO project_coder_profiles(project_id,coder_server_id,repo_url,base_ref,auth_provider_id,template_name,enabled,created_at,updated_at)
                VALUES(?,?,?,?,?,?,1,?,?)''', (project_id, server['id'], profile['repo_url'], profile['base_ref'],
                                            profile['auth_provider_id'], profile['template_name'], app.now(), app.now()))
            leases = []
            for index in (1, 2):
                task_id = app.execute("INSERT INTO tasks(project_id,text,task_order,created_at) VALUES(?,'Persistent runner probe',?,?)", (project_id, index, app.now()))
                run_id = 'probe-' + str(index)
                app.execute("INSERT INTO runs(id,project_id,task_id,mode,status,created_at,updated_at) VALUES(?,?,?,'supervised','queued',?,?)", (run_id, project_id, task_id, app.now(), app.now()))
                app.create_execution_lease(run_id, task_id, 'coder')
                task = app.one('SELECT * FROM tasks WHERE id=?', (task_id,))
                app.provision_coder_execution(run_id, app.project_or_404(project_id), task)
                first = app.execution_lease(run_id)
                _, environment = app.ensure_coder_runner(server, profile)
                artifact = str(Path(first['worktree_path']) / 'runner-probe-uncommitted.txt')
                code = 'from pathlib import Path; Path(' + repr(artifact) + ').write_text("preserved")'
                subprocess.run(['coder', 'ssh', args.workspace, '--', shlex.join(['python3', '-c', code])],
                               env=environment, check=True, capture_output=True, timeout=30)
                app.provision_coder_execution(run_id, app.project_or_404(project_id), task)
                second = app.execution_lease(run_id)
                assert first['worktree_path'] == second['worktree_path']
                assert first['base_sha'] == second['base_sha']
                code = 'from pathlib import Path; assert Path(' + repr(artifact) + ').read_text() == "preserved"'
                subprocess.run(['coder', 'ssh', args.workspace, '--', shlex.join(['python3', '-c', code])],
                               env=environment, check=True, capture_output=True, timeout=30)
                app.update_run(run_id, 'stopped', 'Probe finished')
                leases.append(second)
            assert leases[0]['workspace_id'] == leases[1]['workspace_id'] == workspace['id']
            assert leases[0]['worktree_path'] != leases[1]['worktree_path']
            print(json.dumps({'runner_reused': True, 'distinct_task_worktrees': True,
                              'retry_preserved_uncommitted_files': True, 'workspace_created': False,
                              'worktree_paths': [lease['worktree_path'] for lease in leases]}, indent=2))
        finally:
            app.DATA_ROOT, app.DB_PATH = original


if __name__ == '__main__':
    main()
