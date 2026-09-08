"""Create a bounded, credential-free manifest for a remote task worktree.

The controller uses this as the precondition for a cross-runner transfer.  It
does not copy bytes, so it is safe to run as a read-only probe before deciding
whether to create a transfer artifact.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
LIMIT = 250 * 1024 * 1024

def git(*args):
    return subprocess.run(['git', '-C', str(root), *args], text=True, capture_output=True)

if git('rev-parse', '--git-dir').returncode:
    print(json.dumps({'available': False, 'reason': 'Not a Git worktree.'}))
    raise SystemExit(0)

paths, total = [], 0
for name in git('ls-files', '--others', '--exclude-standard', '-z').stdout.split('\0'):
    if not name:
        continue
    path = (root / name).resolve()
    if root not in path.parents or not path.is_file() or path.is_symlink():
        print(json.dumps({'available': False, 'reason': 'Unsafe untracked path in task worktree.'}))
        raise SystemExit(0)
    size = path.stat().st_size
    total += size
    if total > LIMIT:
        print(json.dumps({'available': False, 'reason': 'Task untracked files exceed the 250 MB transfer limit.'}))
        raise SystemExit(0)
    paths.append({'path': name, 'size': size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})

base = git('rev-parse', '--verify', 'HEAD').stdout.strip()
diff = git('diff', 'HEAD', '--binary', '--').stdout
payload = (base + '\n' + diff + '\n' + json.dumps(paths, sort_keys=True)).encode()
print(json.dumps({'available': True, 'base_sha': base, 'diff_hash': hashlib.sha256(payload).hexdigest(),
                  'untracked': paths, 'untracked_bytes': total}, separators=(',', ':')))
