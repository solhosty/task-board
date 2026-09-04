"""Read-only Git workspace fingerprint for a persistent Coder task worktree."""
import hashlib
import json
import subprocess
import sys


root = sys.argv[1]


def git(*args):
    return subprocess.run(['git', '-C', root, *args], text=True, capture_output=True)


if git('rev-parse', '--git-dir').returncode:
    print(json.dumps({'available': False, 'reason': 'This remote working folder is not a Git repository.'}))
    raise SystemExit(0)

base_sha = git('rev-parse', '--verify', 'HEAD').stdout.strip() or None
status = git('status', '--porcelain=v1').stdout
diff = git('diff', 'HEAD', '--binary', '--').stdout
untracked = git('ls-files', '--others', '--exclude-standard').stdout
payload = (status + '\n' + diff + '\nUNTRACKED\n' + untracked).encode()
print(json.dumps({
    'available': True,
    'base_sha': base_sha,
    'status': status[-12000:],
    'diff_hash': hashlib.sha256(payload).hexdigest(),
    'changed_files': [line[3:] for line in status.splitlines() if len(line) > 3][:200],
}, separators=(',', ':')))
