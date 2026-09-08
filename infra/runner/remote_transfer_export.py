"""Export bounded uncommitted task state for an isolated runner handoff."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(sys.argv[1]).resolve()
LIMIT = 250 * 1024 * 1024

def git(*args):
    return subprocess.run(['git', '-C', str(root), *args], text=True, capture_output=True)

if git('rev-parse', '--git-dir').returncode:
    print(json.dumps({'available': False, 'reason': 'Not a Git worktree.'}))
    raise SystemExit(0)
base = git('rev-parse', 'HEAD').stdout.strip()
with tempfile.TemporaryDirectory(prefix='harness-bundle-') as directory:
    bundle_path = Path(directory) / 'task.bundle'
    bundled = git('bundle', 'create', str(bundle_path), 'HEAD')
    if bundled.returncode or bundle_path.stat().st_size > LIMIT:
        print(json.dumps({'available': False, 'reason': 'Task Git history could not be exported within the handoff limit.'}))
        raise SystemExit(0)
    bundle = bundle_path.read_bytes()
raw = git('diff', '--name-status', '--find-renames', '-z', 'HEAD').stdout.split('\0')
changed, deleted, index = [], [], 0
while index < len(raw):
    status = raw[index]
    index += 1
    if not status:
        continue
    if status.startswith(('R', 'C')):
        if index + 1 >= len(raw):
            print(json.dumps({'available': False, 'reason': 'Invalid renamed-file handoff.'}))
            raise SystemExit(0)
        old, name = raw[index], raw[index + 1]
        index += 2
        if status.startswith('R'):
            deleted.append(old)
        changed.append(name)
    else:
        if index >= len(raw):
            print(json.dumps({'available': False, 'reason': 'Invalid changed-file handoff.'}))
            raise SystemExit(0)
        name = raw[index]
        index += 1
        (deleted if status.startswith('D') else changed).append(name)
changed += [name for name in git('ls-files', '--others', '--exclude-standard', '-z').stdout.split('\0') if name]
files, total = [], len(bundle)
for name in sorted(set(changed)):
    if (root / name).is_symlink():
        print(json.dumps({'available': False, 'reason': 'Symbolic links require workspace reconciliation.'}))
        raise SystemExit(0)
    path = (root / name).resolve()
    if root not in path.parents or not path.is_file() or path.is_symlink():
        print(json.dumps({'available': False, 'reason': 'Unsafe changed path in task worktree.'}))
        raise SystemExit(0)
    data = path.read_bytes()
    total += len(data)
    if total > LIMIT:
        print(json.dumps({'available': False, 'reason': 'Task changes exceed the 250 MB handoff limit.'}))
        raise SystemExit(0)
    files.append({'path': name, 'data': base64.b64encode(data).decode(), 'mode': path.stat().st_mode & 0o777})
print(json.dumps({'available': True, 'base_sha': base, 'files': files,
                  'bundle': base64.b64encode(bundle).decode(),
                  'deleted': sorted(set(deleted))}, separators=(',', ':')))
