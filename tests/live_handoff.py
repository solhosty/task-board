"""Opt-in live Droid -> Claude HTTP runner smoke, using disposable state only."""
import functools
import json
import os
import signal
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

with tempfile.TemporaryDirectory(prefix='rotation-live-handoff-') as temporary:
    root = Path(temporary).resolve()
    app.DATA_ROOT = root / 'state'
    app.DB_PATH = app.DATA_ROOT / 'state.sqlite3'
    app.init_db()
    folder = root / 'project'
    folder.mkdir()
    (folder / 'existing.txt').write_text('preserve me\n')
    app.execute('UPDATE harnesses SET enabled=0')
    for position, key in enumerate(['droid', 'claude']):
        app.execute('UPDATE harnesses SET installed=1,enabled=1,chain_position=? WHERE key=?', (position, key))
    server = app.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(app.API, directory=str(app.STATIC_ROOT)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def api(path, body=None):
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+path,
            data=json.dumps(body).encode() if body is not None else None, headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)
    try:
        project = api('/api/projects', {'name':'Live smoke', 'repo_path':str(folder), 'default_mode':'unattended'})
        task = api(f"/api/projects/{project['id']}/tasks", {'text':'Create smoke-result.txt containing exactly harness-ok followed by a newline. Use your file editing tool. Preserve existing.txt. Do not use shell commands or network. This is a disposable smoke test.', 'start':True})
        deadline = time.monotonic()+150
        previous = None
        while time.monotonic() < deadline:
            state = api(f"/api/tasks/{task['id']}")
            if state['run']['status'] != previous:
                previous = state['run']['status']
                print(previous, state['run']['message'], flush=True)
            if previous in ('complete','stopped','paused_cooldown'):
                break
            time.sleep(1)
        assert state['run']['status']=='complete', state['run']
        assert [a['harness_key'] for a in state['attempts']]==['droid','claude'], state['attempts']
        assert state['attempts'][0]['status']=='quota'
        assert (folder/'smoke-result.txt').read_text()=='harness-ok\n'
        assert (folder/'existing.txt').read_text()=='preserve me\n'
        assert any(m['role']=='assistant' for m in state['messages'])
        print('PASS: real HTTP task -> Droid quota -> Claude edit -> complete; same non-Git folder, existing file preserved.', flush=True)
    finally:
        for child in list(app.CHILDREN):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
        server.shutdown()
        server.server_close()
        thread.join()
print('Temporary server stopped and disposable data removed.')
