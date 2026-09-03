"""Exercise the project/goal API with disposable folders and SQLite state."""
import functools
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def main():
    with tempfile.TemporaryDirectory(prefix="harness-smoke-") as directory:
        root = Path(directory)
        app.DATA_ROOT = root / "state"
        app.DB_PATH = app.DATA_ROOT / "state.sqlite3"
        app.init_db()
        project_folder = root / "Example app"
        project_folder.mkdir()
        existing_spec = '# Existing project notes\n- [ ] A legacy checklist item\n'
        (project_folder / 'TASKS.md').write_text(existing_spec, encoding='utf-8')
        other_folder = root / "Other app"
        other_folder.mkdir()
        handler = functools.partial(app.API, directory=str(app.STATIC_ROOT))
        server = app.ThreadingHTTPServer((app.HOST, 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://{app.HOST}:{server.server_port}"

        def request(route, data=None, method=None):
            headers = {"Content-Type": "application/json"}
            req = urllib.request.Request(base + route, data=json.dumps(data).encode() if data is not None else None, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)

        try:
            fallback = app.create_server(server.server_port)
            try:
                assert fallback.server_port != server.server_port
            finally:
                fallback.server_close()
            assert request('/api/bootstrap')['projects'] == []
            folders = request('/api/directories', {'path': str(root)})
            assert any(f['name'] == 'Example app' for f in folders['folders'])
            p = request('/api/projects', {'repo_path': str(project_folder), 'name': 'Example', 'verify_command': 'git diff --check'})
            q = request('/api/projects', {'repo_path': str(other_folder), 'name': 'Other'})
            assert p['tasks'] == []
            first = request(f"/api/projects/{p['id']}/tasks", {'text': 'Build account settings\nInclude profile and notification preferences.'})
            second = request(f"/api/projects/{p['id']}/tasks", {'text': 'Build the reporting feature'})
            request(f"/api/tasks/{first['id']}/messages", {'content': 'Start with the profile screen.'})
            session = request(f"/api/tasks/{first['id']}")
            assert len(session['messages']) == 2
            assert session['task']['project_id'] == p['id']
            linked = request(f"/api/tasks/{first['id']}/pull-requests", {'url': 'https://github.com/example/research-harness/pull/42'})['pull_request']
            assert linked['number'] == '42' and linked['state'] == 'untracked'
            assert request(f"/api/tasks/{first['id']}")['pull_requests'][0]['url'] == linked['url']
            try:
                request(f"/api/tasks/{first['id']}/pull-requests", {'url': 'https://example.com/not-a-pr'})
                raise AssertionError('invalid pull-request URL accepted')
            except urllib.error.HTTPError as error:
                assert error.code == 400
            assert request(f"/api/tasks/{second['id']}")['messages'][0]['content'] == 'Build the reporting feature'
            coder = request('/api/coder-servers', {'name': 'Test Coder', 'base_url': 'https://coder.example.test', 'organization': 'engineering'})['server']
            assert coder['status'] == 'unverified' and not coder['token_configured']
            profile = request(f"/api/projects/{p['id']}/coder-profile", {'coder_server_id': coder['id'], 'setup_profile': 'python', 'default_target': 'coder', 'base_ref': 'main'})['coder_profile']
            assert profile['template_name'] == 'harness-test-coder-example' and profile['repo_url'] is None
            board_project = next(item for item in request('/api/bootstrap')['projects'] if item['id'] == p['id'])
            assert board_project['coder_profile']['server_name'] == 'Test Coder'
            assert all(task['execution_backend'] == 'coder' and task['execution_target_label'] == 'Test Coder' for task in board_project['tasks'])
            request(f"/api/tasks/{first['id']}", {'execution_target': 'local'})
            board_project = next(item for item in request('/api/bootstrap')['projects'] if item['id'] == p['id'])
            assert next(task for task in board_project['tasks'] if task['id'] == first['id'])['execution_backend'] == 'local'
            bootstrap = request('/api/bootstrap')
            assert len(next(x for x in bootstrap['projects'] if x['id'] == p['id'])['tasks']) == 2
            assert next(x for x in bootstrap['projects'] if x['id'] == q['id'])['tasks'] == []
            assert (project_folder / 'TASKS.md').read_text(encoding='utf-8') == existing_spec
            try:
                request('/api/projects', {'repo_path': str(project_folder)})
                raise AssertionError('duplicate folder accepted')
            except urllib.error.HTTPError as error:
                assert error.code == 400
            scanned = request('/api/scan', {})
            assert {h['key'] for h in scanned['harnesses']} == set(app.ADAPTERS)
            order = ['droid', 'claude', 'codex', 'opencode']
            request('/api/harness-order', {'order': order})
            assert [h['key'] for h in request('/api/bootstrap')['harnesses']] == order
            try:
                request('/api/harness-order', {'order': ['codex'] * 4})
                raise AssertionError('invalid order accepted')
            except urllib.error.HTTPError as error:
                assert error.code == 400
            assert [h['key'] for h in request('/api/bootstrap')['harnesses']] == order
            updated = request(f"/api/projects/{p['id']}", {'default_mode': 'unattended', 'auto_failover': 0})
            assert updated['default_mode'] == 'unattended' and updated['auto_failover'] == 0
            request(f"/api/tasks/{first['id']}", {'text': 'Renamed account settings'})
            request(f"/api/projects/{p['id']}/task-order", {'order': [second['id'], first['id']]})
            reordered = next(item for item in request('/api/bootstrap')['projects'] if item['id'] == p['id'])['tasks']
            assert [item['id'] for item in reordered] == [second['id'], first['id']]
            request(f"/api/tasks/{second['id']}", method='DELETE')
            managed = next(item for item in request('/api/bootstrap')['projects'] if item['id'] == p['id'])['tasks']
            assert len(managed) == 1 and managed[0]['text'] == 'Renamed account settings'
            print('PASS: project automation defaults plus task rename, ordering, and deletion persist.', flush=True)
            print('PASS: fallback order persists and invalid orders are rejected.', flush=True)
            with patch.object(app, 'eligible_harnesses', return_value=[{'key': 'droid', 'model': 'default'}, {'key': 'codex', 'model': 'default'}]):
                assert app.choose_harness({'preferred_harness': None, 'preferred_model': None})[0]['key'] == 'droid'
                preferred, reason = app.choose_harness({'preferred_harness': 'codex', 'preferred_model': 'custom-model'})
                assert preferred['key'] == 'codex' and preferred['model'] == 'custom-model' and reason == 'preferred'
            request('/api/harnesses/codex', {'model': 'custom-model-test'})
            assert next(h for h in request('/api/bootstrap')['harnesses'] if h['key'] == 'codex')['model'] == 'custom-model-test'
            print('PASS: occupied-port fallback, task model preference, and custom model persistence.', flush=True)
            print('PASS: folder browsing, projects without Git, multiple goals, conversation persistence, project isolation, duplicate handling, real harness scan.', flush=True)
            if '--serve' in sys.argv:
                # Keep fixtures out of the user-facing preview. Only this
                # disposable test database is cleared; real app state is untouched.
                with app.db() as conn:
                    conn.execute('DELETE FROM task_messages')
                    conn.execute('DELETE FROM tasks')
                    conn.execute('DELETE FROM projects')
                assert request('/api/bootstrap')['projects'] == []
                print(f'PREVIEW {base}\nFOLDER {root}', flush=True)
                threading.Event().wait(600)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            print('Temporary server stopped and test data removed.', flush=True)


if __name__ == '__main__':
    main()
