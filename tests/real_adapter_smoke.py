"""Opt-in: tiny live CLI request in a disposable folder; uses existing login."""
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

key = sys.argv[1]
assert key in app.ADAPTERS
with tempfile.TemporaryDirectory(prefix='rotation-live-' + key + '-') as temporary:
    root = Path(temporary)
    (root / 'existing.txt').write_text('preserve this user work\n')
    prompt = ('This is a disposable execution test. Create smoke-result.txt in the current directory '
              'containing exactly harness-ok followed by a newline. Use your file editing tool. '
              'Do not modify any other files, run shell commands, create commits, or use the network. '
              'Reply with a short confirmation.')
    command = app.ADAPTERS[key]['build'](root, 'default', prompt)
    process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise SystemExit(f'FAIL {key}: timed out after 120 seconds; test process stopped')
    reply, error = app.decode_result(key, output)
    if process.returncode or error or not (root / 'smoke-result.txt').exists():
        print(output[-4000:])
        raise SystemExit(f'FAIL {key}: exit={process.returncode}, error={error}, expected file missing or failed')
    assert (root / 'smoke-result.txt').read_text() == 'harness-ok\n'
    assert (root / 'existing.txt').read_text() == 'preserve this user work\n'
    print(f'PASS {key}: actual CLI created expected file and preserved existing work. Parsed reply: {reply[-500:]}')
print('Disposable folder cleaned up. No server started.')
