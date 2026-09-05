"""Idempotent task checkout helper, sent over Coder SSH on stdin.

Only Git working state lives here. Provider credentials stay with the native CLIs.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse


def validate_source(source):
    parsed = urlparse(source)
    https = parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
    ssh = parsed.scheme == 'ssh' and parsed.hostname and not parsed.password and not parsed.query and not parsed.fragment
    scp = re.fullmatch(r'[\w.-]+@[\w.-]+:[\w./-]+', source)
    if not (https or ssh or scp) or any(c in source for c in '\n\r\x00'):
        raise ValueError('Use a credential-free HTTPS or SSH repository URL.')


def git(*args):
    result = subprocess.run(['git', *map(str, args)], text=True, capture_output=True,
                            env=dict(os.environ, GIT_TERMINAL_PROMPT='0'), timeout=120)
    if result.returncode:
        # Do not return Git's raw stderr: credential helpers/remotes can print secrets.
        raise RuntimeError('Remote Git operation failed. Check repository access and the selected branch.')
    return result.stdout.strip()


def prepare(root, source, base_ref, task_key, allow_local=False):
    if not allow_local:
        validate_source(source)
    if not re.fullmatch(r'task-[a-f0-9-]+', task_key):
        raise ValueError('Invalid task worktree key.')
    if not base_ref or base_ref.startswith(('-', '+')) or any(c in base_ref for c in '\n\r\x00'):
        raise ValueError('Invalid base ref.')
    if subprocess.run(['git', 'check-ref-format', '--allow-onelevel', base_ref], capture_output=True).returncode:
        raise ValueError('Use a branch, tag, full ref, or commit SHA; refspecs are not allowed.')
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Serializes shared Git metadata only, not execution in the resulting worktrees.
    with (root / 'checkout.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        repository = root / 'repos' / hashlib.sha256(source.encode()).hexdigest()[:24]
        worktree = root / 'tasks' / task_key
        metadata = root / 'tasks' / (task_key + '.json')
        branch = 'harness/' + task_key
        base = 'refs/harness/bases/' + task_key
        repository.parent.mkdir(exist_ok=True)
        worktree.parent.mkdir(exist_ok=True)
        if metadata.exists():
            saved = json.loads(metadata.read_text())
            if saved['source'] != source:
                raise ValueError('This task already has a different repository. Existing work was preserved.')
            if not worktree.exists():
                raise ValueError('The saved task worktree is missing; recovery is required.')
            # Do not fetch, reset, or move a task's original base on retries/fallbacks.
            actual = Path(git('-C', worktree, 'rev-parse', '--git-common-dir'))
            if not actual.is_absolute():
                actual = worktree / actual
            if actual.resolve() != repository.resolve():
                raise ValueError('Task worktree belongs to a different repository.')
            return {k: saved[k] for k in ('worktree_path', 'base_sha', 'branch_name')}
        if not repository.exists():
            git('init', '--bare', repository)
        origin = subprocess.run(['git', '-C', str(repository), 'remote', 'get-url', 'origin'],
                                capture_output=True, text=True)
        if origin.returncode:
            git('-C', repository, 'remote', 'add', 'origin', source)
        elif origin.stdout.strip() != source:
            raise ValueError('Repository cache has an unexpected origin; nothing was overwritten.')
        # A previous setup may have stopped after fetch or worktree creation.
        existing = subprocess.run(['git', '-C', str(repository), 'show-ref', '--verify', '--quiet', base])
        if existing.returncode:
            git('-C', repository, 'fetch', '--no-tags', '--', source, base_ref)
            git('-C', repository, 'update-ref', base, git('-C', repository, 'rev-parse', 'FETCH_HEAD^{commit}'))
        base_sha = git('-C', repository, 'rev-parse', base + '^{commit}')
        if not worktree.exists():
            git('-C', repository, 'worktree', 'add', '-b', branch, worktree, base_sha)
        else:
            actual = Path(git('-C', worktree, 'rev-parse', '--git-common-dir'))
            if not actual.is_absolute():
                actual = worktree / actual
            if actual.resolve() != repository.resolve() or git('-C', worktree, 'branch', '--show-current') != branch:
                raise ValueError('Unrecognized existing worktree; nothing was overwritten.')
        saved = {'source': source, 'worktree_path': str(worktree), 'base_sha': base_sha, 'branch_name': branch}
        staging = metadata.with_suffix('.tmp')
        staging.write_text(json.dumps(saved))
        staging.replace(metadata)
        return {k: saved[k] for k in ('worktree_path', 'base_sha', 'branch_name')}


if __name__ == '__main__':
    try:
        request = json.loads(sys.argv[1])
        print(json.dumps(prepare('/home/coder/.harness-runner', request['repo_url'],
                                 request['base_ref'], request['task_key'])))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
