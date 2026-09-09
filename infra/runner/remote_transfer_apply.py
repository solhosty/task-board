"""Apply a bounded runner handoff without changing a mismatched base checkout."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import re

root = Path(sys.argv[1]).resolve()
request = json.load(sys.stdin)

def git(*args, input=None):
    return subprocess.run(['git', '-C', str(root), *args], input=input, capture_output=True)

if not isinstance(request, dict) or not isinstance(request.get('base_sha'), str):
    print(json.dumps({'available': False, 'reason': 'Invalid workspace handoff.'}))
    raise SystemExit(0)
base = git('rev-parse', 'HEAD').stdout.decode().strip()
try:
    if not re.fullmatch(r'[a-f0-9]{40,64}', request['base_sha']): raise ValueError()
    files = request.get('files', [])
    deleted = request.get('deleted', [])
    if not isinstance(files, list):
        print(json.dumps({'available': False, 'reason': 'Invalid changed-file handoff.'}))
        raise SystemExit(0)
    if not isinstance(deleted, list): raise ValueError()
    def checked_path(name):
        if not isinstance(name, str): raise ValueError()
        relative = Path(name)
        if relative.is_absolute() or any(part in ('.git', '..') for part in relative.parts): raise ValueError()
        candidate = root / relative
        if candidate.is_symlink(): raise ValueError()
        path = candidate.resolve()
        if root not in path.parents: raise ValueError()
        return path
    removals = [checked_path(name) for name in deleted]
    writes = [(checked_path(item['path']), base64.b64decode(item['data'], validate=True)) for item in files]
    modes = [item.get('mode', 0o644) for item in files]
    if any(not isinstance(mode, int) or mode < 0 or mode > 0o777 for mode in modes): raise ValueError()
    if sum(len(data) for _, data in writes) > 250 * 1024 * 1024: raise ValueError()
    # An existing destination may contain user work. Never erase it on retry.
    status = git('status', '--porcelain', '--untracked-files=all')
    if status.returncode: raise ValueError()
    if status.stdout.strip():
        print(json.dumps({'available': False, 'reason': 'Destination contains existing changes; preserved for reconciliation.'}))
        raise SystemExit(0)
    if base != request['base_sha']:
        bundle = base64.b64decode(request.get('bundle', ''), validate=True)
        if not bundle or len(bundle) > 250 * 1024 * 1024: raise ValueError()
        with tempfile.TemporaryDirectory(prefix='harness-import-') as directory:
            bundle_path = Path(directory) / 'task.bundle'
            bundle_path.write_bytes(bundle)
            if git('bundle', 'verify', str(bundle_path)).returncode: raise ValueError()
            if git('fetch', '--no-tags', str(bundle_path), 'HEAD').returncode: raise ValueError()
            fetched = git('rev-parse', 'FETCH_HEAD').stdout.decode().strip()
            if fetched != request['base_sha']: raise ValueError()
            # Destination cleanliness was checked above. --keep refuses to
            # overwrite concurrent working changes and retains the old ref in
            # its reflog; the source copy is never touched.
            if git('reset', '--keep', fetched).returncode: raise ValueError()
            base = fetched
    for path in removals:
        if root not in path.parents or (path.exists() and not path.is_file()): raise ValueError()
        path.unlink(missing_ok=True)
    for (path, data), mode in zip(writes, modes):
        if root not in path.parents: raise ValueError()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
except (ValueError, KeyError, TypeError):
    print(json.dumps({'available': False, 'reason': 'Invalid encoded workspace handoff.'}))
    raise SystemExit(0)
print(json.dumps({'available': True, 'base_sha': base}))
