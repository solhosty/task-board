"""Opt-in test using the installed Codex subscription, in a disposable repo."""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

with tempfile.TemporaryDirectory(prefix='rotation-real-codex-') as temporary:
    repo = Path(temporary)
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    prompt = ('This is a disposable execution smoke test. Create smoke-result.txt in the current directory '
              'containing exactly harness-ok followed by a newline. Do not modify other files, do not '
              'create a commit, and do not use the network. Reply with a short confirmation.')
    command = app.ADAPTERS['codex']['build'](repo, 'default', prompt)
    try:
        result = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise SystemExit('FAIL: real Codex invocation did not finish within 120 seconds')
    if result.returncode:
        print((result.stderr or result.stdout)[-5000:])
        raise SystemExit(f'FAIL: Codex exit code {result.returncode}')
    assert (repo / 'smoke-result.txt').read_text() == 'harness-ok\n'
    print('PASS: installed Codex executed the configured adapter command and created the expected file in a disposable repository.')
print('Disposable repository removed; no server was started.')
