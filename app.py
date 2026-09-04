#!/usr/bin/env python3
"""Harness Rotation v1: local project sessions, SQLite state, multi-harness runner."""

from __future__ import annotations

import json
import argparse
import errno
import hashlib
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from events import read_events


def browse_directory(location: Optional[str] = None) -> Dict[str, Any]:
    folder = Path(location).expanduser().resolve() if location else Path.home()
    if not folder.is_dir():
        raise ValueError("That folder does not exist")
    children = sorted((p for p in folder.iterdir() if p.is_dir() and not p.name.startswith('.')), key=lambda p: p.name.lower())
    return {"path": str(folder), "name": folder.name, "parent": str(folder.parent),
            "folders": [{"name": p.name, "path": str(p)} for p in children]}

APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = APP_ROOT / ".harness"
STATIC_ROOT = APP_ROOT / "static"
DB_PATH = DATA_ROOT / "state.sqlite3"
WORKTREE_ROOT = Path("/private/tmp/harness-rotation-worktrees")
HOST, PORT = "127.0.0.1", 4173
DB_LOCK = threading.RLock()
RUN_LOCK = threading.RLock()
CHILDREN = set()

QUOTA_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in [
        r"usage limit", r"quota.{0,40}(exceeded|limit|reset)", r"rate limit.{0,80}(retry|wait|reset)",
        r"too many requests", r"weekly.{0,30}limit", r"you.?ve hit.{0,30}limit",
    ]
]

# Commands are argument lists: no shell interpolation and no configurable command fragments.
# Each adapter retains its CLI permission checks; cwd is not an OS sandbox.
ADAPTERS: Dict[str, Dict[str, Any]] = {
    "codex": {
        "label": "Codex", "binary": "codex", "runnable": True,
        "models": ["default"],
        "build": lambda root, model, prompt: ["codex", "exec", "--json", "--approve-for-me", "--skip-git-repo-check", "--cd", str(root)] + ([] if model in ("", "default") else ["--model", model]) + [prompt],
        "safety_note": "Uses Codex workspace-write sandbox with automatic approval review.",
    },
    "claude": {
        "label": "Claude Code", "binary": "claude", "runnable": True,
        "models": ["opus", "sonnet", "haiku", "default"],
        "build": lambda root, model, prompt: ["claude", "-p", "--permission-mode", "acceptEdits", "--verbose", "--output-format", "stream-json"] + ([] if model in ("", "default") else ["--model", model]) + [prompt],
        "safety_note": "Uses the installed CLI with its permission checks intact.",
    },
    "droid": {
        "label": "Droid", "binary": "droid", "runnable": True,
        "models": ["default"],
        "build": lambda root, model, prompt: ["droid", "exec", "--auto", "medium", "--output-format", "stream-json", "--cwd", str(root)] + ([] if model in ("", "default") else ["--model", model]) + [prompt],
        "safety_note": "Uses the installed CLI with its permission checks intact.",
    },
    "opencode": {
        "label": "OpenCode", "binary": "opencode", "runnable": True,
        "models": ["default"],
        "build": lambda root, model, prompt: ["opencode", "run", "--format", "json"] + ([] if model in ("", "default") else ["--model", model]) + [prompt],
        "safety_note": "Uses OpenCode run with configured provider permissions.",
    },
}

CODER_SETUP_PROFILES = ("auto", "python", "node")
KEYCHAIN_SERVICE = "Harness Rotation Coder"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    DATA_ROOT.mkdir(exist_ok=True)
    with db() as conn:
        conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS projects (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, repo_path TEXT NOT NULL UNIQUE,
          verify_command TEXT NOT NULL, default_mode TEXT NOT NULL DEFAULT 'supervised',
          auto_failover INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS harnesses (
          id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, label TEXT NOT NULL, binary TEXT NOT NULL,
          version TEXT, installed INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'unavailable',
          detail TEXT, billing_confirmed INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 0,
          chain_position INTEGER NOT NULL DEFAULT 0, model TEXT NOT NULL DEFAULT 'default',
          cooldown_until TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          text TEXT NOT NULL, task_order INTEGER NOT NULL, source_line INTEGER,
          status TEXT NOT NULL DEFAULT 'pending', mode_override TEXT,
          preferred_harness TEXT, preferred_model TEXT, force_gate INTEGER NOT NULL DEFAULT 0,
          degradable INTEGER NOT NULL DEFAULT 0, execution_target TEXT NOT NULL DEFAULT 'project'
          CHECK(execution_target IN ('project','local','coder')),
          last_attempt_id INTEGER, created_at TEXT NOT NULL,
          UNIQUE(project_id, task_order)
        );
        CREATE TABLE IF NOT EXISTS attempts (
          id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id), harness_key TEXT,
          model TEXT, selection TEXT, status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
          worktree_path TEXT, branch_name TEXT, base_sha TEXT, commit_sha TEXT, diff_stat TEXT,
          diff_output TEXT, verify_output TEXT, error TEXT, log_path TEXT, run_id TEXT
        );
        CREATE TABLE IF NOT EXISTS task_messages (
          id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
          role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), task_id INTEGER REFERENCES tasks(id),
          mode TEXT NOT NULL, status TEXT NOT NULL, message TEXT, attempt_id INTEGER,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_leases (
          id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
          task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          backend TEXT NOT NULL CHECK(backend IN ('local','coder')),
          state TEXT NOT NULL DEFAULT 'planned' CHECK(state IN ('planned','provisioning','ready','recovering','released','failed')),
          workspace_id TEXT, workspace_name TEXT, workspace_url TEXT,
          template_name TEXT, template_version TEXT,
          worktree_path TEXT, base_sha TEXT, checkpoint_ref TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_checkpoints (
          id INTEGER PRIMARY KEY, lease_id TEXT NOT NULL REFERENCES execution_leases(id) ON DELETE CASCADE,
          attempt_id INTEGER REFERENCES attempts(id) ON DELETE SET NULL,
          kind TEXT NOT NULL CHECK(kind IN ('git_commit','patch','working_tree')),
          reference TEXT NOT NULL, base_sha TEXT, created_at TEXT NOT NULL,
          UNIQUE(lease_id, reference)
        );
        CREATE TABLE IF NOT EXISTS task_pull_requests (
          id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          provider TEXT NOT NULL DEFAULT 'github', url TEXT NOT NULL,
          number TEXT, branch_name TEXT, head_sha TEXT,
          state TEXT NOT NULL DEFAULT 'untracked' CHECK(state IN ('untracked','draft','open','merged','closed')),
          review_state TEXT NOT NULL DEFAULT 'unknown' CHECK(review_state IN ('unknown','pending','approved','changes_requested')),
          merged_at TEXT, last_synced_at TEXT, sync_error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(task_id, url)
        );
        CREATE TABLE IF NOT EXISTS coder_servers (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, base_url TEXT NOT NULL UNIQUE,
          organization TEXT NOT NULL DEFAULT 'default',
          status TEXT NOT NULL DEFAULT 'unverified' CHECK(status IN ('unverified','reachable','authorized','error')),
          version TEXT, detail TEXT, capabilities_json TEXT NOT NULL DEFAULT '{}',
          token_configured INTEGER NOT NULL DEFAULT 0, last_checked_at TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_coder_profiles (
          project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
          coder_server_id INTEGER REFERENCES coder_servers(id) ON DELETE SET NULL,
          setup_profile TEXT NOT NULL DEFAULT 'auto' CHECK(setup_profile IN ('auto','python','node')),
          repo_url TEXT, base_ref TEXT NOT NULL DEFAULT 'main', template_name TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 0, default_target TEXT NOT NULL DEFAULT 'local'
          CHECK(default_target IN ('local','coder')),
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """)
        columns = {item[1] for item in conn.execute("PRAGMA table_info(attempts)")}
        if "diff_output" not in columns:
            conn.execute("ALTER TABLE attempts ADD COLUMN diff_output TEXT")
        for pos, (key, adapter) in enumerate(ADAPTERS.items()):
            conn.execute("""INSERT INTO harnesses(key,label,binary,chain_position,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(key) DO NOTHING""", (key, adapter["label"], adapter["binary"], pos, now()))
        harness_columns = {item[1] for item in conn.execute("PRAGMA table_info(harnesses)")}
        if 'model_catalog' not in harness_columns:
            conn.execute("ALTER TABLE harnesses ADD COLUMN model_catalog TEXT")
            conn.execute("ALTER TABLE harnesses ADD COLUMN model_source TEXT")
        if 'pool_migrated' not in harness_columns:
            conn.execute("ALTER TABLE harnesses ADD COLUMN pool_migrated INTEGER DEFAULT 1")
            conn.execute("UPDATE harnesses SET enabled=installed")
        project_columns = {item[1] for item in conn.execute('PRAGMA table_info(projects)')}
        if 'execution_mode' not in project_columns:
            conn.execute("ALTER TABLE projects ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'local'")
        if 'auto_failover' not in project_columns:
            conn.execute("ALTER TABLE projects ADD COLUMN auto_failover INTEGER NOT NULL DEFAULT 1")
        message_columns = {item[1] for item in conn.execute('PRAGMA table_info(task_messages)')}
        if 'attempt_id' not in message_columns:
            conn.execute('ALTER TABLE task_messages ADD COLUMN attempt_id INTEGER REFERENCES attempts(id)')
        if 'tool_permissions' not in harness_columns:
            conn.execute("ALTER TABLE harnesses ADD COLUMN tool_permissions TEXT NOT NULL DEFAULT 'standard'")
        run_columns = {item[1] for item in conn.execute('PRAGMA table_info(runs)')}
        if 'permission_override' not in run_columns:
            conn.execute('ALTER TABLE runs ADD COLUMN permission_override TEXT')
        if 'permissions_json' not in run_columns:
            conn.execute('ALTER TABLE runs ADD COLUMN permissions_json TEXT')
        task_columns = {item[1] for item in conn.execute('PRAGMA table_info(tasks)')}
        if 'tool_permissions' not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN tool_permissions TEXT NOT NULL DEFAULT 'inherit'")
        if 'execution_target' not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN execution_target TEXT NOT NULL DEFAULT 'project'")
        coder_profile_columns = {item[1] for item in conn.execute('PRAGMA table_info(project_coder_profiles)')}
        if 'default_target' not in coder_profile_columns:
            conn.execute("ALTER TABLE project_coder_profiles ADD COLUMN default_target TEXT NOT NULL DEFAULT 'local'")
        attempt_columns = {item[1] for item in conn.execute('PRAGMA table_info(attempts)')}
        if 'tool_permissions' not in attempt_columns:
            conn.execute('ALTER TABLE attempts ADD COLUMN tool_permissions TEXT')


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def rows(sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    with DB_LOCK, closing(db()) as conn:
        return [dict(item) for item in conn.execute(sql, params).fetchall()]


def one(sql: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
    result = rows(sql, params)
    return result[0] if result else None


def execute(sql: str, params: Tuple[Any, ...] = ()) -> int:
    with DB_LOCK, db() as conn:
        cursor = conn.execute(sql, params)
        return cursor.lastrowid


def slug(value: str, fallback: str) -> str:
    result = re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')
    return (result or fallback)[:48]


def normalize_coder_url(value: Any) -> str:
    parsed = urlparse(str(value or '').strip())
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Enter a Coder server URL such as http://127.0.0.1:3000.')
    if parsed.path not in ('', '/'):
        raise ValueError('Use the Coder server base URL without a path.')
    return f'{parsed.scheme}://{parsed.netloc}'.rstrip('/')


def coder_template_name(server_name: str, project_name: str) -> str:
    candidate = f'harness-{slug(server_name, "server")}-{slug(project_name, "project")}'
    if len(candidate) <= 32:
        return candidate
    suffix = hashlib.sha1(candidate.encode('utf-8')).hexdigest()[:6]
    return f'{candidate[:25].rstrip("-")}-{suffix}'


def public_coder_server(server: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(server)
    try:
        result['capabilities'] = json.loads(result.pop('capabilities_json') or '{}')
    except json.JSONDecodeError:
        result['capabilities'] = {}
    result['token_configured'] = bool(result['token_configured'])
    return result


def coder_server_or_404(server_id: int) -> Dict[str, Any]:
    server = one('SELECT * FROM coder_servers WHERE id=?', (server_id,))
    if not server:
        raise ValueError('Coder server not found')
    return server


def project_coder_profile(project_id: int) -> Optional[Dict[str, Any]]:
    profile = one("""SELECT p.*,s.name AS server_name,s.base_url AS server_url,s.organization AS server_organization,
        s.status AS server_status,s.version AS server_version,s.detail AS server_detail,s.token_configured AS token_configured
        FROM project_coder_profiles p LEFT JOIN coder_servers s ON s.id=p.coder_server_id WHERE p.project_id=?""", (project_id,))
    if profile:
        profile['enabled'] = bool(profile['enabled'])
        profile['token_configured'] = bool(profile['token_configured'])
    return profile


def effective_execution_backend(project: Dict[str, Any], task: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
    profile = project_coder_profile(project['id'])
    target = task.get('execution_target') or 'project'
    if target == 'local':
        return 'local', profile
    if target == 'coder':
        return 'coder', profile
    return ('coder' if profile and profile['enabled'] and profile.get('default_target') == 'coder' else 'local'), profile


def keychain_available() -> bool:
    return bool(shutil.which('security'))


def keychain_account(server_id: int) -> str:
    return f'coder-server-{server_id}'


def save_coder_token(server_id: int, token: str) -> None:
    if not keychain_available():
        raise ValueError('macOS Keychain is unavailable; Coder tokens cannot be stored by this Harness server.')
    result = subprocess.run(['security', 'add-generic-password', '-U', '-s', KEYCHAIN_SERVICE,
                             '-a', keychain_account(server_id), '-w', token], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'Could not save the Coder token in macOS Keychain.')


def remove_coder_token(server_id: int) -> None:
    if not keychain_available():
        return
    subprocess.run(['security', 'delete-generic-password', '-s', KEYCHAIN_SERVICE,
                    '-a', keychain_account(server_id)], capture_output=True, text=True, timeout=10)


def read_coder_token(server: Dict[str, Any]) -> Optional[str]:
    scoped = os.environ.get(f'HARNESS_CODER_TOKEN_{server["id"]}')
    if scoped:
        return scoped
    if os.environ.get('HARNESS_CODER_TOKEN'):
        return os.environ['HARNESS_CODER_TOKEN']
    if not server.get('token_configured') or not keychain_available():
        return None
    result = subprocess.run(['security', 'find-generic-password', '-w', '-s', KEYCHAIN_SERVICE,
                             '-a', keychain_account(server['id'])], capture_output=True, text=True, timeout=10)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def coder_json(base_url: str, path: str, token: Optional[str] = None) -> Dict[str, Any]:
    headers = {'Accept': 'application/json'}
    if token:
        headers['Coder-Session-Token'] = token
    request = Request(base_url.rstrip('/') + path, headers=headers)
    with urlopen(request, timeout=4) as response:
        payload = json.loads(response.read().decode('utf-8'))
    return payload if isinstance(payload, dict) else {'value': payload}


def probe_coder(base_url: str, token: Optional[str] = None) -> Dict[str, Any]:
    info: Dict[str, Any] = {'reachable': False, 'authorized': False, 'version': None, 'detail': None, 'capabilities': {}}
    try:
        build = coder_json(base_url, '/api/v2/buildinfo', token)
        info['reachable'] = True
        info['version'] = build.get('version') or build.get('build_version')
        info['capabilities']['build_info'] = True
    except HTTPError as exc:
        if exc.code in (401, 403):
            info['reachable'] = True
            info['detail'] = 'Coder is reachable; an API token is required to inspect it.'
        else:
            info['detail'] = f'Coder returned HTTP {exc.code}.'
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        info['detail'] = f'Could not reach this Coder server: {exc}'
    if info['reachable'] and token:
        try:
            user = coder_json(base_url, '/api/v2/users/me', token)
            info['authorized'] = True
            info['capabilities']['authenticated_user'] = user.get('username') or user.get('email') or user.get('name') or 'authenticated'
            info['detail'] = f"Authorized as {info['capabilities']['authenticated_user']}."
        except HTTPError as exc:
            info['detail'] = 'Coder is reachable, but the saved API token was rejected.' if exc.code in (401, 403) else f'Coder returned HTTP {exc.code} while checking the token.'
        except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            info['detail'] = f'Could not verify the Coder token: {exc}'
    return info


def verify_coder_server(server_id: int) -> Dict[str, Any]:
    server = coder_server_or_404(server_id)
    probe = probe_coder(server['base_url'], read_coder_token(server))
    status = 'authorized' if probe['authorized'] else 'reachable' if probe['reachable'] else 'error'
    execute("""UPDATE coder_servers SET status=?,version=?,detail=?,capabilities_json=?,last_checked_at=?,updated_at=? WHERE id=?""",
            (status, probe['version'], probe['detail'], json.dumps(probe['capabilities']), now(), now(), server_id))
    return public_coder_server(coder_server_or_404(server_id))


def discover_local_coder_servers() -> List[Dict[str, Any]]:
    candidates = []
    for base_url in ('http://127.0.0.1:3000', 'http://localhost:3000'):
        probe = probe_coder(base_url)
        if probe['reachable']:
            candidates.append({'base_url': base_url, 'version': probe['version'], 'detail': probe['detail'] or 'Coder is reachable locally.'})
    return candidates


def git(args: List[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), text=True, capture_output=True, check=check)


def parse_tasks_file(repo: Path) -> List[Tuple[int, str, bool]]:
    task_file = repo / "TASKS.md"
    if not task_file.exists():
        raise ValueError("TASKS.md was not found at the repository root")
    entries = []
    for number, line in enumerate(task_file.read_text(encoding="utf-8").splitlines(), start=1):
        match = re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.+?)\s*$", line)
        if match:
            entries.append((number, match.group(2), match.group(1).lower() == "x"))
    return entries


def project_or_404(project_id: int) -> Dict[str, Any]:
    project = one("SELECT * FROM projects WHERE id=?", (project_id,))
    if not project:
        raise ValueError("Project not found")
    return project


def scan_harnesses() -> List[Dict[str, Any]]:
    found = []
    for key, adapter in ADAPTERS.items():
        location = shutil.which(adapter["binary"])
        installed, version, detail = 0, None, "Not found on PATH"
        if location:
            installed = 1
            try:
                output = subprocess.run([adapter["binary"], "--version"], text=True, capture_output=True, timeout=5)
                version = (output.stdout or output.stderr).strip().split("\n")[0][:160] or "installed"
                detail = adapter["safety_note"]
            except Exception as exc:
                version, detail = "installed", f"Version check failed: {exc}"
        status = "detected" if installed else "unavailable"
        previous = one('SELECT detail FROM harnesses WHERE key=?', (key,))
        if installed and not previous['detail']:
            execute('UPDATE harnesses SET enabled=1 WHERE key=?', (key,))
        execute("UPDATE harnesses SET installed=?, version=?, status=?, detail=?, updated_at=? WHERE key=?",
                (installed, version, status, detail, now(), key))
        models, source = discover_models(key, bool(installed))
        execute("UPDATE harnesses SET model_catalog=?,model_source=? WHERE key=?", (json.dumps(models), source, key))
        found.append({"key": key, "installed": bool(installed), "version": version, "detail": detail})
    return found


def discover_models(key: str, installed: bool) -> Tuple[List[str], str]:
    models = ['default']
    source = 'Harness default or custom model ID'
    if not installed:
        return models, 'Install this harness, then scan again'
    try:
        if key == 'codex':
            cache = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'models_cache.json'
            payload = json.loads(cache.read_text(encoding='utf-8'))
            models += [item['slug'] for item in payload.get('models', []) if item.get('slug') and item.get('visibility') != 'hide']
            source = 'Local Codex model cache · refreshed when you scan'
        elif key == 'droid':
            output = subprocess.run(['droid', 'exec', '--help'], capture_output=True, text=True, timeout=10).stdout
            section = output.split('Available Models:', 1)[-1].split('Model details:', 1)[0]
            if 'Available Models:' in output:
                models += [m.group(1) for line in section.splitlines() if (m := re.match(r'^\s{2,}(\S+)\s{2,}\S', line))]
                source = 'Installed Droid CLI catalog, including configured custom models'
        elif key == 'claude':
            models += ['opus', 'sonnet', 'haiku']
            source = 'Claude aliases · specific model IDs can be entered below'
        elif key == 'opencode':
            output = subprocess.run(['opencode', 'models'], capture_output=True, text=True, timeout=15)
            models += [line.strip() for line in output.stdout.splitlines() if re.match(r'^\S+/\S+$', line.strip())]
            source = 'Installed OpenCode provider catalog'
    except (OSError, ValueError, subprocess.TimeoutExpired):
        source = 'Model discovery unavailable · enter an ID or use default'
    return list(dict.fromkeys(models)), source


def effective_mode(project: Dict[str, Any], task: Dict[str, Any]) -> str:
    return task["mode_override"] or project["default_mode"]


def adapter_metadata() -> Dict[str, Any]:
    saved = {h['key']: h for h in rows('SELECT * FROM harnesses')}
    return {key: {'models': json.loads(saved[key]['model_catalog']) if saved[key].get('model_catalog') else value['models'],
                  'model_source': saved[key].get('model_source') or 'Scan to discover available models',
                  'runnable': value['runnable']} for key, value in ADAPTERS.items()}


def current_run(project_id: int) -> Optional[Dict[str, Any]]:
    return one("SELECT * FROM runs WHERE project_id=? AND status NOT IN ('complete','stopped','discarded','blocked') ORDER BY rowid DESC LIMIT 1", (project_id,))


def execution_lease(run_id: str) -> Optional[Dict[str, Any]]:
    return one("SELECT * FROM execution_leases WHERE run_id=?", (run_id,))


def task_pull_requests(task_id: int) -> List[Dict[str, Any]]:
    return rows("SELECT * FROM task_pull_requests WHERE task_id=? ORDER BY id DESC", (task_id,))


def pull_request_status(project: Dict[str, Any], pull_request: Dict[str, Any]) -> Dict[str, Any]:
    if pull_request['provider'] != 'github':
        raise ValueError('Only GitHub pull-request sync is supported in this phase.')
    if not shutil.which('gh'):
        raise ValueError('GitHub CLI (gh) is not installed on the harness server.')
    fields = 'url,number,state,isDraft,reviewDecision,mergedAt,headRefName,headRefOid'
    result = subprocess.run(['gh', 'pr', 'view', pull_request['url'], '--json', fields], cwd=project['repo_path'], text=True,
                            capture_output=True, timeout=20)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'GitHub CLI could not read this pull request.')
    payload = json.loads(result.stdout)
    raw_state = str(payload.get('state', '')).lower()
    pr_state = 'merged' if payload.get('mergedAt') or raw_state == 'merged' else 'closed' if raw_state == 'closed' else 'draft' if payload.get('isDraft') else 'open'
    review = {'approved': 'approved', 'changes_requested': 'changes_requested'}.get(str(payload.get('reviewDecision') or '').lower(), 'pending' if pr_state in ('draft', 'open') else 'unknown')
    return {'url': payload.get('url') or pull_request['url'], 'number': str(payload.get('number') or '') or None,
            'branch_name': payload.get('headRefName'), 'head_sha': payload.get('headRefOid'), 'state': pr_state,
            'review_state': review, 'merged_at': payload.get('mergedAt')}


def sync_task_pull_request(project: Dict[str, Any], pull_request: Dict[str, Any]) -> Dict[str, Any]:
    try:
        status = pull_request_status(project, pull_request)
    except Exception as exc:
        execute("UPDATE task_pull_requests SET last_synced_at=?, sync_error=?, updated_at=? WHERE id=?", (now(), str(exc), now(), pull_request['id']))
        return one("SELECT * FROM task_pull_requests WHERE id=?", (pull_request['id'],)) or pull_request
    execute("""UPDATE task_pull_requests SET url=?,number=?,branch_name=?,head_sha=?,state=?,review_state=?,merged_at=?,
        last_synced_at=?,sync_error=NULL,updated_at=? WHERE id=?""",
        (status['url'], status['number'], status['branch_name'], status['head_sha'], status['state'], status['review_state'],
         status['merged_at'], now(), now(), pull_request['id']))
    return one("SELECT * FROM task_pull_requests WHERE id=?", (pull_request['id'],)) or pull_request


def create_execution_lease(run_id: str, task_id: int, backend: str = 'local') -> Dict[str, Any]:
    if backend not in ('local', 'coder'):
        raise ValueError('Unknown execution backend')
    lease = execution_lease(run_id)
    if lease:
        return lease
    lease_id = str(uuid.uuid4())
    execute("""INSERT INTO execution_leases(id,run_id,task_id,backend,state,created_at,updated_at)
        VALUES(?,?,?,?,'planned',?,?)""", (lease_id, run_id, task_id, backend, now(), now()))
    return execution_lease(run_id) or raise_missing_lease(run_id)


def raise_missing_lease(run_id: str) -> Dict[str, Any]:
    raise RuntimeError(f'Execution lease was not created for run {run_id}')


def bind_local_execution_lease(run_id: str, worktree: Path, base_sha: Optional[str]) -> None:
    lease = execution_lease(run_id)
    if not lease:
        raise RuntimeError(f'Run {run_id} has no execution lease')
    if lease['backend'] != 'local':
        return
    execute("""UPDATE execution_leases SET state='ready', worktree_path=?, base_sha=?, updated_at=?
        WHERE run_id=?""", (str(worktree), base_sha, now(), run_id))


def provision_coder_execution(run_id: str, project: Dict[str, Any], task: Dict[str, Any]) -> str:
    profile = project_coder_profile(project['id'])
    if not profile or not profile.get('coder_server_id'):
        raise ValueError('This project has no Coder environment configured.')
    server = coder_server_or_404(profile['coder_server_id'])
    token = read_coder_token(server)
    if not token:
        raise ValueError('The Coder token is unavailable from Keychain.')
    workspace = f'harness-task-{task["id"]}-{run_id[:8]}'
    environment = dict(os.environ, CODER_URL=server['base_url'], CODER_SESSION_TOKEN=token,
                       CODER_ORGANIZATION=server['organization'])
    execute("""UPDATE execution_leases SET state='provisioning',workspace_name=?,workspace_url=?,
        template_name=?,updated_at=? WHERE run_id=?""",
        (workspace, f"{server['base_url']}/@{workspace}", profile['template_name'], now(), run_id))
    command = ['coder', 'create', workspace, '--template', profile['template_name'], '--parameter',
               f"repo_url={profile.get('repo_url') or ''}", '--parameter', f"base_ref={profile.get('base_ref') or 'main'}",
               '--stop-after', '8h', '--yes']
    created = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=300)
    if created.returncode:
        raise RuntimeError(created.stderr.strip() or created.stdout.strip() or 'Coder workspace creation failed.')
    checked = subprocess.run(['coder', 'ssh', '--wait', 'yes', workspace, '--', 'git', '-C', '/home/coder/task',
                              'rev-parse', 'HEAD'], env=environment, capture_output=True, text=True, timeout=180)
    if checked.returncode:
        raise RuntimeError(checked.stderr.strip() or 'Coder workspace checkout validation failed.')
    base_sha = checked.stdout.strip()
    execute("""UPDATE execution_leases SET state='ready',workspace_name=?,worktree_path='/home/coder/task',
        base_sha=?,updated_at=? WHERE run_id=?""", (workspace, base_sha, now(), run_id))
    return workspace


def harness_availability(harness: Dict[str, Any]) -> Dict[str, str]:
    label = harness['label']
    if not harness['installed']:
        return {'code': 'not_found', 'label': 'CLI not found', 'reason': f"{label}: this server cannot find {harness['binary']} on PATH. If it works in your terminal, start Rotation from that terminal and rescan."}
    if not harness['enabled']:
        return {'code': 'disabled', 'label': 'Not in rotation', 'reason': f'{label} is not in your rotation. Add it to use it for tasks.'}
    billing_vars = {'codex': ['OPENAI_API_KEY', 'CODEX_API_KEY'], 'claude': ['ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'], 'droid': [], 'opencode': []}
    detected = [name for name in billing_vars[harness['key']] if os.environ.get(name)]
    if detected:
        return {'code': 'api_billing', 'label': 'API credentials detected', 'reason': f"{label}: {', '.join(detected)} is set in the server environment. Restart without these overrides to use your CLI subscription."}
    if harness['cooldown_until'] and datetime.fromisoformat(harness['cooldown_until']) > datetime.now(timezone.utc):
        return {'code': 'cooldown', 'label': 'Cooling down', 'reason': f"{label}: Rotation paused this harness after a limit error until {harness['cooldown_until']}."}
    return {'code': 'ready', 'label': 'Available', 'reason': 'Uses your existing CLI login and permission settings. Billing is controlled by the CLI; Rotation cannot guarantee subscription billing.'}


def execution_blockers(project: Dict[str, Any], task: Optional[Dict[str, Any]] = None) -> List[str]:
    blockers = []
    repo = Path(project['repo_path'])
    if not repo.is_dir():
        return ['The project folder no longer exists.']
    head = git(['rev-parse', '--verify', 'HEAD'], repo, check=False)
    if project.get('execution_mode') == 'worktree' and head.returncode:
        blockers.append('This project needs a Git repository with an initial commit before a worktree can run.')
    elif project.get('execution_mode') == 'worktree' and git(['status', '--porcelain'], repo).stdout.strip():
        blockers.append('The project has uncommitted changes. Commit or stash them before running so the worktree includes the code you expect.')
    if task:
        backend, profile = effective_execution_backend(project, task)
        if backend == 'coder':
            if not profile or not profile.get('coder_server_id'):
                blockers.append('Choose a Coder server for this project before using remote execution.')
            elif profile.get('server_status') != 'authorized':
                blockers.append('Verify the project’s Coder server and API token before using remote execution.')
    if not eligible_harnesses():
        candidates = rows('SELECT * FROM harnesses ORDER BY chain_position')
        supported = [h for h in candidates if ADAPTERS[h['key']]['runnable']]
        blockers.extend(harness_availability(h)['reason'] for h in supported)
    return blockers


def request_run(project_id: int, task_id: Optional[int] = None) -> Dict[str, Any]:
    with DB_LOCK:
        project = project_or_404(project_id)
        busy = current_run(project_id)
        if busy and task_id and busy['task_id'] == int(task_id):
            return {'run_id': busy['id'], 'status': busy['status'], 'message': busy['message']}
        task = one("SELECT * FROM tasks WHERE project_id=? AND status='pending'" + (' AND id=?' if task_id else ' ORDER BY task_order LIMIT 1'),
                   (project_id, int(task_id)) if task_id else (project_id,))
        if not task:
            raise ValueError('No pending task to run')
        mode = effective_mode(project, task)
        blockers = execution_blockers(project, task)
        if busy:
            blockers.append('Another task in this project has an active or paused run. Finish that run first.')
        harness, _ = choose_harness(task)
        permissions = permission_snapshot(task)
        if harness:
            problem = permission_blocker(harness['key'], permissions[harness['key']])
            if problem:
                blockers.append(problem)
        status = 'blocked' if blockers else ('awaiting_dispatch' if mode == 'supervised' or task['force_gate'] else 'queued')
        message = ' '.join(blockers) if blockers else (f"Ready to start {harness['label']} ({harness['model']}). Approve dispatch to begin." if status == 'awaiting_dispatch' else 'Queued for execution.')
        run_id = str(uuid.uuid4())
        execute('INSERT INTO runs(id,project_id,task_id,mode,status,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                (run_id, project_id, task['id'], mode, status, message, now(), now()))
        execute('UPDATE runs SET permissions_json=? WHERE id=?', (json.dumps(permissions), run_id))
        create_execution_lease(run_id, task['id'], effective_execution_backend(project, task)[0])
    if status == 'queued':
        threading.Thread(target=run_attempt, args=(run_id, project_id, task['id']), daemon=True).start()
    return {'run_id': run_id, 'status': status, 'message': message}


def update_run(run_id: str, status: str, message: str, attempt_id: Optional[int] = None) -> None:
    execute("UPDATE runs SET status=?, message=?, attempt_id=COALESCE(?,attempt_id), updated_at=? WHERE id=?",
            (status, message, attempt_id, now(), run_id))


def claim_run(run_id: str, expected: str, status: str, message: str) -> None:
    with DB_LOCK, db() as conn:
        result = conn.execute('UPDATE runs SET status=?,message=?,updated_at=? WHERE id=? AND status=?',
                              (status, message, now(), run_id, expected))
        if result.rowcount != 1:
            raise ValueError('This run already changed state. Refresh before trying again.')


def eligible_harnesses() -> List[Dict[str, Any]]:
    all_items = rows("SELECT * FROM harnesses WHERE enabled=1 AND installed=1 ORDER BY chain_position")
    moment = datetime.now(timezone.utc)
    available = []
    for item in all_items:
        if harness_availability(item)['code'] != 'ready':
            continue
        if item["cooldown_until"] and datetime.fromisoformat(item["cooldown_until"]) > moment:
            continue
        available.append(item)
    return available


def choose_harness(task: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    candidates = eligible_harnesses()
    if task["preferred_harness"]:
        for candidate in candidates:
            if candidate["key"] == task["preferred_harness"]:
                chosen = dict(candidate)
                if task["preferred_model"]:
                    chosen['model'] = task['preferred_model']
                return chosen, "preferred"
    return (candidates[0], "fallback") if candidates else (None, "none")


def task_prompt(task: Dict[str, Any], project: Dict[str, Any]) -> str:
    messages = rows("SELECT role,content FROM task_messages WHERE task_id=? ORDER BY id", (task['id'],))
    conversation = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)
    prior = rows("SELECT * FROM attempts WHERE task_id=? AND status!='running' ORDER BY id DESC LIMIT 6", (task['id'],))
    handoff = []
    for attempt in reversed(prior):
        events = read_events(attempt.get('log_path'))
        notes = [e['text'] for e in events if e['kind']=='message'][-8:]
        actions = [e['kind']+': '+e['text']+' ('+e['status']+')' for e in events if e['kind']!='message'][-12:]
        handoff.append(f"{attempt['harness_key']} attempt #{attempt['id']} — {attempt['status']}\n" + '\n'.join(notes+actions) + '\n' + (attempt.get('error') or ''))
    handoff_context = '\n\n'.join(handoff)[-20000:]
    return f"""You are continuing a task session in the working directory provided to this process.

Task: {task['text']}

Conversation:
{conversation or task['text']}

Previous harness progress (may be incomplete; verify against the code):
{handoff_context or 'No previous attempts.'}

Read the existing code and current diff before editing. Another harness may have worked on this same session. Preserve all existing uncommitted and untracked work. Work only inside this project folder. Do not commit, stash, reset, clean, or push. Keep changes focused and run relevant tests. Before meaningful tool batches, send a short user-visible progress update explaining what you are checking or changing; do not reveal private chain-of-thought. Finish with a concise summary of changes and tests run. If permissions prevent completing the task, clearly report that rather than claiming success."""


def log_file(attempt_id: int) -> Path:
    directory = DATA_ROOT / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"attempt-{attempt_id}.log"


def permission_blocker(key: str, mode: str) -> Optional[str]:
    if mode == 'ask':
        return f"{ADAPTERS[key]['label']}: live approval in chat is not supported by this adapter yet. No command was started. Choose a supported permission mode in task settings."
    if mode == 'auto' and key == 'opencode':
        return 'OpenCode: automatic review is not supported by this adapter. No command was started. Use harness defaults with your configured OpenCode policy, or choose another harness.'
    return None


def permission_snapshot(task: Dict[str, Any]) -> Dict[str, str]:
    requested = task.get('tool_permissions') or 'inherit'
    return {h['key']: (h.get('tool_permissions') or 'standard') if requested == 'inherit' else requested
            for h in rows('SELECT * FROM harnesses')}


def configured_command(harness: Dict[str, Any], root: Path, prompt: str, override: Optional[str] = None) -> List[str]:
    mode = override or harness.get('tool_permissions') or 'standard'
    if mode not in ('standard', 'auto', 'ask'):
        raise ValueError('Unknown permission mode')
    problem = permission_blocker(harness['key'], mode)
    if problem:
        raise ValueError(problem)
    command = ADAPTERS[harness['key']]['build'](root, harness['model'], prompt)
    if harness['key']=='claude' and mode=='auto' and '--permission-mode' in command:
        command[command.index('--permission-mode')+1]='auto'
    return command


def stream_process(command: List[str], cwd: Path, output_file: Path) -> Tuple[int, str]:
    captured: List[str] = []
    with output_file.open("w", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(command) + "\n\n")
        handle.flush()
        process = subprocess.Popen(command, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
        CHILDREN.add(process)
        def timed_out():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        timer = threading.Timer(600, timed_out)
        timer.daemon = True
        timer.start()
        assert process.stdout
        with process.stdout:
            for line in process.stdout:
                captured.append(line)
                handle.write(line)
                handle.flush()
        returncode = process.wait()
        timer.cancel()
        CHILDREN.discard(process)
        if returncode == -signal.SIGTERM:
            captured.append('\nHarness exceeded the 10-minute execution timeout. Files were preserved. Retry or split this task into a smaller step.\n')
    return returncode, "".join(captured)


def make_worktree(project: Dict[str, Any], run_id: str) -> Tuple[Path, str, str]:
    repo = Path(project["repo_path"])
    base = git(["rev-parse", "HEAD"], repo).stdout.strip()
    branch = f"harness/{run_id[:8]}"
    destination = WORKTREE_ROOT / f"project-{project['id']}" / run_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = git(["worktree", "add", "-b", branch, str(destination), base], repo, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Could not create Git worktree")
    return destination, branch, base


def worktree_diff(attempt: Dict[str, Any]) -> str:
    root = Path(attempt['worktree_path'])
    if not root.exists():
        return attempt.get('diff_output') or 'Worktree is no longer available.'
    if not attempt['base_sha']:
        return 'This folder has no Git baseline. Changes are in the project folder; no commit was created.'
    diff = git(['diff', attempt['base_sha'], '--'], root, check=False).stdout
    untracked = git(['ls-files', '--others', '--exclude-standard', '-z'], root).stdout.split('\0')
    for path in filter(None, untracked):
        diff += git(['diff', '--no-index', '--', '/dev/null', path], root, check=False).stdout
    return diff


def cleanup_worktree(repo: Path, worktree: Path) -> None:
    if repo.resolve() == worktree.resolve():
        return  # Local runs must NEVER remove or reset project files.
    git(["worktree", "remove", "--force", str(worktree)], repo, check=False)


def decode_result(key: str, output: str) -> Tuple[str, Optional[str]]:
    """Interpret structured CLI output, including failures that exit with code zero."""
    messages, failure = [], None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'item.completed' and event.get('item', {}).get('type') == 'agent_message':
            messages.append(event['item'].get('text', ''))
        if event.get('type') == 'assistant':
            content = event.get('message', {}).get('content', [])
            messages.extend(c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text')
        if event.get('type') == 'text':
            messages.append(event.get('part', {}).get('text', ''))
        if event.get('type') == 'message' and event.get('role') == 'assistant':
            messages.append(event.get('text', ''))
        if isinstance(event.get('result'), str):
            messages = [event['result']]
        if event.get('is_error') or event.get('type') == 'error' or event.get('permission_denials'):
            failure = event.get('result') or event.get('message') or str(event.get('error') or event.get('permission_denials'))
    return ('\n\n'.join(messages).strip() or output[-16000:]), failure


def log_details(attempt: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    path = Path(attempt['log_path']) if attempt and attempt.get('log_path') else None
    if not path or not path.exists():
        return {'log': 'No log yet.', 'activity': [], 'session_id': None, 'resume_command': None}
    with path.open('rb') as handle:
        prefix = handle.read(65536).decode('utf-8', errors='replace')
        handle.seek(max(0, path.stat().st_size - 30000))
        tail = handle.read().decode('utf-8', errors='replace')
    session_id = None
    for line in prefix.splitlines():
        match = re.match(r'^session id:\s*([0-9a-f-]{36})\s*$', line)
        if match:
            session_id = match[1]
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'thread.started':
            candidate = event.get('thread_id', '')
            if re.fullmatch(r'[0-9a-f-]{36}', candidate):
                session_id = candidate
    activity = []
    for line in tail.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get('item') or {}
        if event.get('type') in ('item.started', 'item.completed'):
            kind = item.get('type')
            if kind == 'command_execution':
                activity.append({'kind': 'command', 'text': item.get('command', '')[:240], 'status': item.get('status', '')})
            elif kind == 'file_change':
                activity.append({'kind': 'edit', 'text': ', '.join(c.get('path', '') for c in item.get('changes', []))[:240], 'status': item.get('status', '')})
            elif kind == 'agent_message':
                activity.append({'kind': 'message', 'text': item.get('text', '')[:1500], 'status': ''})
        if event.get('type') == 'assistant':
            for part in event.get('message', {}).get('content', []):
                if part.get('type') == 'text':
                    activity.append({'kind':'message', 'text':part.get('text', '')[:1500], 'status':''})
                elif part.get('type') == 'tool_use':
                    inputs = part.get('input') or {}
                    activity.append({'kind':'tool', 'text':part.get('name', 'Tool') + ' · ' + str(inputs.get('command') or inputs.get('file_path') or inputs.get('path') or '')[:240], 'status':'working'})
        if event.get('type') == 'message' and event.get('role') == 'assistant':
            activity.append({'kind':'message', 'text':event.get('text', '')[:1500], 'status':''})
        if event.get('type') in ('tool_call', 'tool_use'):
            activity.append({'kind':'tool', 'text':str(event.get('toolName') or event.get('tool_name') or event.get('name') or 'Tool')[:240], 'status':'working'})
        if event.get('type') == 'text':
            activity.append({'kind':'message', 'text':event.get('part', {}).get('text', '')[:1500], 'status':''})
        if event.get('type') == 'tool_use' and isinstance(event.get('part'), dict):
            part = event['part']
            activity[-1] = {'kind':'tool', 'text':str(part.get('tool') or 'Tool')[:240], 'status':str(part.get('state', {}).get('status', 'working'))}
    history = read_events(path)
    return {'log': tail, 'activity': activity[-6:], 'events': history, 'session_id': session_id,
            'resume_command': f'codex resume {session_id}' if session_id and attempt['harness_key'] == 'codex' else None}


def commit_and_merge(project: Dict[str, Any], task: Dict[str, Any], attempt: Dict[str, Any]) -> Tuple[Optional[str], str, str]:
    repo, worktree = Path(project["repo_path"]), Path(attempt["worktree_path"])
    if git(['status', '--porcelain'], repo).stdout.strip() or git(['rev-parse', 'HEAD'], repo).stdout.strip() != attempt['base_sha']:
        raise RuntimeError('The main project changed during execution. Verified work is preserved in the worktree for review.')
    git(["add", "-A"], worktree)
    status = git(["status", "--porcelain"], worktree).stdout.strip()
    if status:
        message = "harness: " + task["text"][:68]
        result = git(["commit", "-m", message], worktree, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Could not commit worktree changes")
    head = git(["rev-parse", "HEAD"], worktree).stdout.strip()
    merged = git(["merge", "--ff-only", attempt["branch_name"]], repo, check=False)
    if merged.returncode:
        raise RuntimeError("Main repository changed during this run; worktree kept. " + merged.stderr.strip())
    mark_task_complete(repo, task)
    return (head,
            git(["diff", "--stat", attempt["base_sha"], "HEAD"], repo).stdout.strip(),
            git(["diff", attempt["base_sha"], "HEAD", "--"], repo).stdout)


def mark_task_complete(repo: Path, task: Dict[str, Any]) -> None:
    if task['source_line'] is None:
        return
    file = repo / "TASKS.md"
    content = file.read_text(encoding="utf-8").splitlines(keepends=True)
    preferred_line = (task["source_line"] or 1) - 1
    locations = [preferred_line] + [i for i in range(len(content)) if i != preferred_line]
    for index in locations:
        if 0 <= index < len(content) and re.match(r"^\s*[-*]\s+\[ \]\s+" + re.escape(task["text"]) + r"\s*$", content[index].rstrip("\n")):
            content[index] = re.sub(r"(\[) (\])", r"\1x\2", content[index], count=1)
            file.write_text("".join(content), encoding="utf-8")
            git(["add", "TASKS.md"], repo)
            result = git(["commit", "-m", "chore: complete task"], repo, check=False)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "Could not commit TASKS.md")
            return
    raise RuntimeError("Merged work, but could not locate the unchecked task in TASKS.md")


def run_attempt(run_id: str, project_id: int, task_id: int, resume_attempt_id: Optional[int] = None, permission_retry: bool = False) -> None:
    try:
        _run_attempt(run_id, project_id, task_id, resume_attempt_id, permission_retry)
    except Exception as exc:
        execute("UPDATE attempts SET status='failed',ended_at=?,error=? WHERE run_id=? AND status IN ('running','verified')", (now(), str(exc), run_id))
        update_run(run_id, 'stopped', 'Execution stopped: ' + str(exc))


def _run_attempt(run_id: str, project_id: int, task_id: int, resume_attempt_id: Optional[int] = None, permission_retry: bool = False) -> None:
    """Run a single attempt. Failures are persisted rather than raised into the HTTP thread."""
    with RUN_LOCK:
        project, task = project_or_404(project_id), one("SELECT * FROM tasks WHERE id=?", (task_id,))
        assert task
        backend, _ = effective_execution_backend(project, task)
        if backend == 'coder':
            workspace = provision_coder_execution(run_id, project, task)
            update_run(run_id, 'stopped', f'Coder workspace {workspace} is ready with the project checked out. Install and authenticate a remote harness CLI before agent dispatch can be enabled.')
            return
        harness, selection = choose_harness(task)
        if permission_retry:
            prior = one('SELECT * FROM attempts WHERE id=? AND task_id=?', (resume_attempt_id, task_id))
            harness = one('SELECT * FROM harnesses WHERE key=?', (prior['harness_key'],)) if prior else None
            if harness and harness_availability(harness)['code'] != 'ready':
                raise ValueError(harness_availability(harness)['reason'])
            if harness:
                harness['model'] = prior['model']
            selection = 'permission retry'
        if not harness:
            waiting = rows("SELECT cooldown_until FROM harnesses WHERE enabled=1 AND cooldown_until IS NOT NULL ORDER BY cooldown_until LIMIT 1")
            message = "No harness in your rotation is currently available."
            if waiting:
                message += " Next cooldown expires " + waiting[0]["cooldown_until"] + "."
            update_run(run_id, "paused_cooldown", message)
            return
        run = one('SELECT * FROM runs WHERE id=?', (run_id,))
        # Old runs retain their previous adapter behavior; new runs freeze all defaults.
        permissions = json.loads(run['permissions_json']) if run.get('permissions_json') else permission_snapshot(task)
        permission_mode = run.get('permission_override') or permissions[harness['key']]
        problem = permission_blocker(harness['key'], permission_mode)
        if problem:
            update_run(run_id, 'stopped', problem)
            return
        if resume_attempt_id:
            prior = one("SELECT * FROM attempts WHERE id=?", (resume_attempt_id,))
            if not prior or not Path(prior["worktree_path"]).exists():
                update_run(run_id, "stopped", "The preserved worktree is no longer available.")
                return
            worktree, branch, base = Path(prior["worktree_path"]), prior["branch_name"], prior["base_sha"]
        else:
            try:
                if project.get('execution_mode') == 'worktree':
                    worktree, branch, base = make_worktree(project, run_id)
                else:
                    worktree, branch = Path(project['repo_path']), None
                    base = git(['rev-parse', '--verify', 'HEAD'], worktree, check=False).stdout.strip() or None
            except Exception as exc:
                update_run(run_id, "stopped", f"Worktree setup failed: {exc}")
                return
        bind_local_execution_lease(run_id, worktree, base)
        attempt_id = execute("""INSERT INTO attempts(task_id,harness_key,model,selection,status,started_at,worktree_path,branch_name,base_sha,run_id)
          VALUES(?,?,?,?,?,?,?,?,?,?)""", (task_id, harness["key"], harness["model"], selection + (" · resumed" if resume_attempt_id else ""), "running", now(), str(worktree), branch, base, run_id))
        execute("UPDATE tasks SET last_attempt_id=? WHERE id=?", (attempt_id, task_id))
        execute('UPDATE attempts SET tool_permissions=? WHERE id=?', (permission_mode, attempt_id))
        execute("INSERT INTO task_messages(task_id,role,content,created_at,attempt_id) VALUES(?,'system',?,?,?)",
                (task_id, f"{harness['label']} started with {'automatic permissions' if permission_mode == 'auto' else 'adapter default permissions'}.", now(), attempt_id))
        file = log_file(attempt_id)
        execute("UPDATE attempts SET log_path=? WHERE id=?", (str(file), attempt_id))
        update_run(run_id, "running", f"{harness['label']} · {harness['model']} is working in {worktree}. Waiting for CLI output…", attempt_id)
        run = one('SELECT * FROM runs WHERE id=?', (run_id,))
        command = configured_command(harness, worktree, task_prompt(task, project), permission_mode)
        reply_file = file.with_suffix('.reply.txt')
        if command[0] == 'codex':
            command = command[:-1] + ['--output-last-message', str(reply_file)] + command[-1:]
        try:
            code, output = stream_process(command, worktree, file)
        except Exception as exc:
            code, output = -1, str(exc)
        reply, failure = decode_result(harness['key'], output)
        if failure:
            code = code or 1
            output += '\n' + failure
        execute("INSERT INTO task_messages(task_id,role,content,created_at) VALUES(?,?,?,?)",
                (task_id, "system", f"{harness['label']} attempt #{attempt_id} ended with exit code {code}. See its attempt log for output.", now()))
        if code == 0:
            reply = reply_file.read_text(encoding='utf-8') if reply_file.exists() else reply
            if reply.strip():
                execute("INSERT INTO task_messages(task_id,role,content,created_at,attempt_id) VALUES(?,'assistant',?,?,?)", (task_id, reply.strip(), now(), attempt_id))
        if code:
            is_quota = any(pattern.search(output) for pattern in QUOTA_PATTERNS)
            outcome = "quota" if is_quota else "failed"
            error = "Harness reported a quota/rate limit." if is_quota else f"Harness exited with code {code}."
            execute("UPDATE attempts SET status=?, ended_at=?, error=? WHERE id=?", (outcome, now(), error, attempt_id))
            if is_quota:
                reset = re.search(r'resets? in (\d+)\s*(day|hour|minute)', output, re.I)
                seconds = int(reset[1]) * {'day': 86400, 'hour': 3600, 'minute': 60}[reset[2].lower()] if reset else 4 * 3600
                until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
                execute("UPDATE harnesses SET cooldown_until=?, updated_at=? WHERE id=?", (until, now(), harness["id"]))
                if effective_mode(project, task) == "supervised" or task["force_gate"] or not project.get("auto_failover", 1):
                    update_run(run_id, "awaiting_resume", f"{harness['label']} reached its limit (retry after {until}). Files are preserved at {worktree}. Resume on the next harness.", attempt_id)
                else:
                    update_run(run_id, "rotating", f"{harness['label']} reached its limit (retry after {until}). Continuing in the same folder on the next harness.", attempt_id)
                    threading.Thread(target=run_attempt, args=(run_id, project_id, task_id, attempt_id), daemon=True).start()
            else:
                update_run(run_id, "stopped", f"{harness['label']} stopped (exit {code}). {output[-1500:].strip()}\nFiles remain at {worktree}.", attempt_id)
            return
        update_run(run_id, 'verifying', 'Harness finished. Running the project verification command.', attempt_id)
        verify_command = project['verify_command'].strip()
        if verify_command == 'git diff --check' and git(['rev-parse', '--git-dir'], worktree, check=False).returncode:
            verify_command = ''
        verify = subprocess.run(verify_command or 'true', cwd=str(worktree), shell=True, text=True, capture_output=True, timeout=600)
        verification = (verify.stdout + verify.stderr)[-12000:]
        with file.open("a", encoding="utf-8") as handle:
            handle.write("\n\n$ " + project["verify_command"] + "\n" + verification)
        if verify.returncode:
            execute("UPDATE attempts SET status='verify_failed', ended_at=?, verify_output=?, error=? WHERE id=?",
                    (now(), verification, f"Verify command exited with code {verify.returncode}", attempt_id))
            update_run(run_id, "stopped", f"Verification failed (exit {verify.returncode}). {verification[-1500:]}\nFiles remain at {worktree}.", attempt_id)
            return
        execute("UPDATE attempts SET status='verified', verify_output=? WHERE id=?", (verification, attempt_id))
        if effective_mode(project, task) == "supervised" or task["force_gate"]:
            local = branch is None
            update_run(run_id, "awaiting_review" if local else "awaiting_commit", ('Changes are in your project folder. ' + ('Verification passed. ' if verify_command else 'No verification command ran. ') + 'Review the result, then finish this run. No commit will be created.') if local else 'Verification passed. Review and approve the isolated commit.', attempt_id)
            return
        finish_commit(run_id, project_id, task_id, attempt_id)


def finish_commit(run_id: str, project_id: int, task_id: int, attempt_id: int) -> None:
    with RUN_LOCK:
        project, task = project_or_404(project_id), one("SELECT * FROM tasks WHERE id=?", (task_id,))
        attempt = one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
        if not task or not attempt:
            return
        try:
            local = Path(attempt['worktree_path']).resolve() == Path(project['repo_path']).resolve()
            if local:
                commit_sha, stat, diff_output = None, 'Local changes (includes pre-existing edits)', worktree_diff(attempt)
            else:
                commit_sha, stat, diff_output = commit_and_merge(project, task, attempt)
            execute("UPDATE attempts SET status='completed', ended_at=?, commit_sha=?, diff_stat=?, diff_output=? WHERE id=?", (now(), commit_sha, stat, diff_output, attempt_id))
            execute("UPDATE tasks SET status='completed', last_attempt_id=? WHERE id=?", (attempt_id, task_id))
            cleanup_worktree(Path(project["repo_path"]), Path(attempt["worktree_path"]))
            update_run(run_id, "complete", "Run finished. Changes remain in your project folder; no commit was created. Send a follow-up to continue." if local else "Task verified and merged. Send a follow-up to continue.", attempt_id)
        except Exception as exc:
            execute("UPDATE attempts SET status='merge_failed', ended_at=?, error=? WHERE id=?", (now(), str(exc), attempt_id))
            update_run(run_id, "stopped", f"Commit/merge stopped safely: {exc}", attempt_id)


def serialize_project(project: Dict[str, Any]) -> Dict[str, Any]:
    project["tasks"] = rows("SELECT * FROM tasks WHERE project_id=? ORDER BY task_order", (project["id"],))
    project['coder_profile'] = project_coder_profile(project['id'])
    for task in project['tasks']:
        task['pull_requests'] = task_pull_requests(task['id'])
        task['run'] = one("SELECT * FROM runs WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (task['id'],))
        backend, profile = effective_execution_backend(project, task)
        task['execution_backend'] = backend
        task['execution_target_label'] = profile.get('server_name') if backend == 'coder' and profile else 'Local'
    project["run"] = current_run(project["id"])
    return project


def conversation_messages(task_id: int) -> List[Dict[str, Any]]:
    messages = rows('SELECT m.*,a.harness_key,a.model FROM task_messages m LEFT JOIN attempts a ON a.id=m.attempt_id WHERE m.task_id=? ORDER BY m.id', (task_id,))
    attempts = {a['id']: a for a in rows('SELECT * FROM attempts WHERE task_id=?', (task_id,))}
    previous = None
    for message in messages:
        # Older versions saved the attempt ID in progress text, not a column.
        match = re.search(r' attempt #(\d+) ended with exit code ', message['content']) if message['role']=='system' else None
        if match and int(match[1]) in attempts:
            previous = attempts[int(match[1])]
            message['attempt_id'] = previous['id']
        elif message['role']=='user':
            previous = None
        if message['role']=='assistant' and not message.get('attempt_id') and previous:
            message['attempt_id'] = previous['id']
        attempt = attempts.get(message.get('attempt_id'))
        if attempt:
            message.update(harness_key=attempt['harness_key'], model=attempt['model'])
    return messages


def present_attempt(attempt: Dict[str, Any]) -> Dict[str, Any]:
    attempt = dict(attempt)
    if attempt['status']=='failed':
        permissions = [e for e in read_events(attempt.get('log_path')) if e['kind']=='permission']
        if permissions:
            attempt['display_status']='Needs permission'
            attempt['display_reason']=permissions[-1]['text']+'. The CLI could not complete its tools; any edits remain in the project.'
    return attempt


class API(SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        print("[http] " + fmt % args)

    def send_json(self, value: Any, status: int = 200) -> None:
        raw = json.dumps(value, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        try:
            if route == "/api/bootstrap":
                harnesses = rows('SELECT * FROM harnesses ORDER BY chain_position')
                for harness in harnesses:
                    harness['availability'] = harness_availability(harness)
                self.send_json({"api_version": 9, "projects": [serialize_project(item) for item in rows("SELECT * FROM projects ORDER BY id DESC")], "harnesses": harnesses,
                                "coder_servers": [public_coder_server(item) for item in rows('SELECT * FROM coder_servers ORDER BY name')], "adapters": adapter_metadata()})
                return
            match = re.match(r"^/api/tasks/(\d+)$", route)
            if match:
                task_id = int(match.group(1))
                task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
                if not task:
                    raise ValueError("Task not found")
                selected, selection = choose_harness(task)
                blockers = execution_blockers(project_or_404(task['project_id']), task)
                if selected:
                    problem = permission_blocker(selected['key'], permission_snapshot(task)[selected['key']])
                    if problem:
                        blockers.append(problem)
                task_run = one('SELECT * FROM runs WHERE task_id=? ORDER BY rowid DESC LIMIT 1', (task_id,))
                self.send_json({"task": task, "next_harness": {'key': selected['key'], 'label': selected['label'], 'model': selected['model'], 'selection': selection} if selected else None,
                    "run": task_run,
                    "lease": execution_lease(task_run['id']) if task_run else None,
                    "pull_requests": task_pull_requests(task_id),
                    "blockers": blockers,
                    "messages": conversation_messages(task_id),
                    "attempts": [present_attempt(a) for a in rows("SELECT * FROM attempts WHERE task_id=? ORDER BY id", (task_id,))]})
                return
            match = re.match(r"^/api/projects/(\d+)/attempts$", route)
            if match:
                self.send_json(rows("""SELECT a.*, t.text AS task_text FROM attempts a JOIN tasks t ON t.id=a.task_id
                    WHERE t.project_id=? ORDER BY a.id DESC""", (int(match.group(1)),)))
                return
            match = re.match(r"^/api/attempts/(\d+)/diff$", route)
            if match:
                attempt = one("SELECT * FROM attempts WHERE id=?", (int(match.group(1)),))
                if not attempt:
                    raise ValueError("Attempt not found")
                if attempt["diff_output"]:
                    self.send_json({"diff": attempt["diff_output"], "worktree": attempt["worktree_path"]})
                    return
                self.send_json({"diff": worktree_diff(attempt), "worktree": attempt["worktree_path"]})
                return
            match = re.match(r"^/api/attempts/(\d+)/log$", route)
            if match:
                attempt = one("SELECT * FROM attempts WHERE id=?", (int(match.group(1)),))
                self.send_json(log_details(attempt))
                return
            match = re.match(r"^/api/attempts/(\d+)/raw$", route)
            if match:
                attempt = one('SELECT * FROM attempts WHERE id=?', (int(match.group(1)),))
                path = Path(attempt['log_path']) if attempt and attempt.get('log_path') else None
                if not path or not path.is_file():
                    raise ValueError('No saved transcript for this attempt')
                self.send_response(200)
                self.send_header('Content-Type','text/plain; charset=utf-8')
                self.send_header('Content-Disposition',f'attachment; filename="attempt-{attempt["id"]}.log"')
                self.end_headers()
                with path.open('rb') as handle:
                    shutil.copyfileobj(handle,self.wfile)
                return
            self.path = "/index.html" if route == "/" else route
            return super().do_GET()
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        try:
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self.send_json({"error": "Requests must come from this dashboard"}, 403)
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise ValueError("Expected a JSON request")
            payload = self.body()
            if route == "/api/scan":
                self.send_json({"harnesses": scan_harnesses()})
                return
            if route == '/api/coder-servers/discover':
                self.send_json({'servers': discover_local_coder_servers()})
                return
            if route == '/api/coder-servers':
                name = str(payload.get('name') or '').strip()[:80]
                if not name:
                    raise ValueError('Give this Coder server a name.')
                base_url = normalize_coder_url(payload.get('base_url'))
                organization = str(payload.get('organization') or 'default').strip()[:120] or 'default'
                token = str(payload.get('token') or '').strip()
                if token and not keychain_available():
                    raise ValueError('macOS Keychain is unavailable; add this server without a token or start Harness on macOS.')
                server_id = execute("""INSERT INTO coder_servers(name,base_url,organization,created_at,updated_at)
                    VALUES(?,?,?,?,?)""", (name, base_url, organization, now(), now()))
                if token:
                    try:
                        save_coder_token(server_id, token)
                    except Exception:
                        execute('DELETE FROM coder_servers WHERE id=?', (server_id,))
                        raise
                    execute('UPDATE coder_servers SET token_configured=1,updated_at=? WHERE id=?', (now(), server_id))
                self.send_json({'server': public_coder_server(coder_server_or_404(server_id))}, 201)
                return
            match = re.match(r'^/api/coder-servers/(\d+)/verify$', route)
            if match:
                self.send_json({'server': verify_coder_server(int(match.group(1)))})
                return
            match = re.match(r'^/api/coder-servers/(\d+)$', route)
            if match:
                server_id = int(match.group(1))
                coder_server_or_404(server_id)
                fields = {name: payload[name] for name in ('name', 'base_url', 'organization') if name in payload}
                if 'name' in fields:
                    fields['name'] = str(fields['name']).strip()[:80]
                    if not fields['name']:
                        raise ValueError('Coder server name cannot be empty.')
                if 'base_url' in fields:
                    fields['base_url'] = normalize_coder_url(fields['base_url'])
                if 'organization' in fields:
                    fields['organization'] = str(fields['organization']).strip()[:120] or 'default'
                token = str(payload.get('token') or '').strip()
                if token:
                    save_coder_token(server_id, token)
                    fields['token_configured'] = 1
                if payload.get('clear_token'):
                    remove_coder_token(server_id)
                    fields['token_configured'] = 0
                if not fields:
                    raise ValueError('No Coder server updates supplied.')
                assignments = ', '.join(f'{name}=?' for name in fields) + ', updated_at=?'
                execute(f'UPDATE coder_servers SET {assignments} WHERE id=?', tuple(fields.values()) + (now(), server_id))
                self.send_json({'server': public_coder_server(coder_server_or_404(server_id))})
                return
            if route == '/api/harness-order':
                order = payload.get('order')
                if not isinstance(order, list) or len(order) != len(ADAPTERS) or any(not isinstance(key, str) for key in order) or set(order) != set(ADAPTERS):
                    raise ValueError('Order must contain each harness exactly once')
                with DB_LOCK, db() as conn:
                    for position, key in enumerate(order):
                        conn.execute('UPDATE harnesses SET chain_position=?,updated_at=? WHERE key=?', (position, now(), key))
                self.send_json({'ok': True})
                return
            if route == "/api/directories":
                self.send_json(browse_directory(payload.get("path")))
                return
            if route == "/api/projects":
                repo = Path(payload["repo_path"]).expanduser().resolve()
                if not repo.is_dir():
                    raise ValueError("Choose an existing project folder")
                if one("SELECT id FROM projects WHERE repo_path=?", (str(repo),)):
                    raise ValueError("This folder is already in your projects")
                default_mode = payload.get("default_mode") or "supervised"
                if default_mode not in ("supervised", "unattended"):
                    raise ValueError("Default mode must be supervised or unattended")
                project_id = execute("INSERT INTO projects(name,repo_path,verify_command,default_mode,auto_failover,created_at) VALUES(?,?,?,?,?,?)",
                    (payload.get("name") or repo.name, str(repo), payload.get("verify_command") or "git diff --check", default_mode, int(bool(payload.get("auto_failover", True))), now()))
                if payload.get('execution_mode') == 'worktree':
                    execute("UPDATE projects SET execution_mode='worktree' WHERE id=?", (project_id,))
                self.send_json(serialize_project(project_or_404(project_id)), 201)
                return
            match = re.match(r"^/api/projects/(\d+)$", route)
            if match:
                project_id = int(match.group(1))
                project_or_404(project_id)
                fields = {name: payload[name] for name in ("name", "verify_command", "default_mode", "execution_mode", "auto_failover") if name in payload}
                if not fields:
                    raise ValueError("No project settings supplied")
                if "name" in fields:
                    fields["name"] = str(fields["name"]).strip()[:120]
                    if not fields["name"]:
                        raise ValueError("Project name cannot be empty")
                if "verify_command" in fields:
                    fields["verify_command"] = str(fields["verify_command"]).strip() or "git diff --check"
                if fields.get("default_mode") not in (None, "supervised", "unattended"):
                    raise ValueError("Default mode must be supervised or unattended")
                if fields.get("execution_mode") not in (None, "local", "worktree"):
                    raise ValueError("Working location must be local or worktree")
                if "auto_failover" in fields:
                    fields["auto_failover"] = int(bool(fields["auto_failover"]))
                assignments = ", ".join(f"{name}=?" for name in fields)
                execute(f"UPDATE projects SET {assignments} WHERE id=?", tuple(fields.values()) + (project_id,))
                self.send_json(serialize_project(project_or_404(project_id)))
                return
            match = re.match(r'^/api/projects/(\d+)/coder-profile$', route)
            if match:
                project_id = int(match.group(1))
                project = project_or_404(project_id)
                raw_server_id = payload.get('coder_server_id')
                if raw_server_id in (None, '', 0, '0'):
                    execute('DELETE FROM project_coder_profiles WHERE project_id=?', (project_id,))
                    self.send_json({'coder_profile': None})
                    return
                server = coder_server_or_404(int(raw_server_id))
                setup_profile = str(payload.get('setup_profile') or 'auto')
                if setup_profile not in CODER_SETUP_PROFILES:
                    raise ValueError('Choose an approved Coder setup profile.')
                default_target = str(payload.get('default_target') or 'local')
                if default_target not in ('local', 'coder'):
                    raise ValueError('Choose local or Coder as the project default.')
                repo_url = str(payload.get('repo_url') or '').strip() or None
                if repo_url and len(repo_url) > 500:
                    raise ValueError('Repository URL is too long.')
                if not repo_url:
                    remote = git(['remote', 'get-url', 'origin'], Path(project['repo_path']), check=False)
                    repo_url = remote.stdout.strip() or None
                base_ref = str(payload.get('base_ref') or 'main').strip()[:160] or 'main'
                template_name = coder_template_name(server['name'], project['name'])
                execute("""INSERT INTO project_coder_profiles(project_id,coder_server_id,setup_profile,repo_url,base_ref,template_name,enabled,default_target,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,1,?,?,?) ON CONFLICT(project_id) DO UPDATE SET coder_server_id=excluded.coder_server_id,
                    setup_profile=excluded.setup_profile,repo_url=excluded.repo_url,base_ref=excluded.base_ref,template_name=excluded.template_name,
                    enabled=1,default_target=excluded.default_target,updated_at=excluded.updated_at""",
                    (project_id, server['id'], setup_profile, repo_url, base_ref, template_name, default_target, now(), now()))
                self.send_json({'coder_profile': project_coder_profile(project_id)})
                return
            match = re.match(r"^/api/projects/(\d+)/sync$", route)
            if match:
                sync_project_tasks(int(match.group(1)))
                self.send_json({"ok": True})
                return
            match = re.match(r"^/api/projects/(\d+)/tasks$", route)
            if match:
                project_id = int(match.group(1))
                project = project_or_404(project_id)
                task_text = str(payload.get("text", "")).strip()
                if not task_text:
                    raise ValueError("Describe what you want to work on")
                permissions = payload.get('tool_permissions', 'inherit')
                if permissions not in ('inherit', 'standard', 'auto', 'ask'):
                    raise ValueError('Unknown task permission mode')
                with DB_LOCK:
                    order = one("SELECT COALESCE(MAX(task_order),-1)+1 AS n FROM tasks WHERE project_id=?", (project_id,))["n"]
                    task_id = execute("INSERT INTO tasks(project_id,text,task_order,status,created_at) VALUES(?,?,?,'pending',?)",
                        (project_id, task_text.splitlines()[0][:120], order, now()))
                    execute('UPDATE tasks SET tool_permissions=? WHERE id=?', (permissions, task_id))
                    execute("INSERT INTO task_messages(task_id,role,content,created_at) VALUES(?,'user',?,?)", (task_id, task_text, now()))
                submission = request_run(project_id, task_id) if payload.get('start') else None
                self.send_json({"id": task_id, 'submission': submission}, 201)
                return
            match = re.match(r"^/api/tasks/(\d+)/pull-requests/sync$", route)
            if match:
                task_id = int(match.group(1))
                task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
                if not task:
                    raise ValueError("Task not found")
                pull_requests = task_pull_requests(task_id)
                if not pull_requests:
                    raise ValueError('Link a pull request before syncing its status.')
                project = project_or_404(task['project_id'])
                self.send_json({'pull_requests': [sync_task_pull_request(project, item) for item in pull_requests]})
                return
            match = re.match(r"^/api/tasks/(\d+)/pull-requests$", route)
            if match:
                task_id = int(match.group(1))
                if not one("SELECT id FROM tasks WHERE id=?", (task_id,)):
                    raise ValueError("Task not found")
                url = str(payload.get('url') or '').strip()
                parsed = urlparse(url)
                if parsed.scheme != 'https' or parsed.netloc.lower() not in ('github.com', 'www.github.com') or not re.match(r'^/[^/]+/[^/]+/pull/\d+/?$', parsed.path):
                    raise ValueError('Enter a GitHub pull-request URL, such as https://github.com/owner/repo/pull/123.')
                number = parsed.path.rstrip('/').split('/')[-1]
                execute("""INSERT INTO task_pull_requests(task_id,provider,url,number,created_at,updated_at)
                    VALUES(?,'github',?,?,?,?) ON CONFLICT(task_id,url) DO UPDATE SET updated_at=excluded.updated_at""",
                    (task_id, url, number, now(), now()))
                pull_request = one("SELECT * FROM task_pull_requests WHERE task_id=? AND url=?", (task_id, url))
                self.send_json({'pull_request': pull_request}, 201)
                return
            match = re.match(r"^/api/tasks/(\d+)/messages$", route)
            if match:
                task_id = int(match.group(1))
                task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
                if not task:
                    raise ValueError("Task not found")
                content = str(payload.get("content", "")).strip()
                if not content:
                    raise ValueError("Write a message first")
                active = current_run(task['project_id'])
                if active and active['task_id'] == task_id:
                    raise ValueError("This task has an active run; wait for it to finish before adding new instructions")
                execute("INSERT INTO task_messages(task_id,role,content,created_at) VALUES(?,'user',?,?)", (task_id, content, now()))
                execute("UPDATE tasks SET status='pending' WHERE id=?", (task_id,))
                submission = request_run(task['project_id'], task_id) if payload.get('start') else None
                self.send_json({"ok": True, 'submission': submission}, 201)
                return
            match = re.match(r"^/api/projects/(\d+)/run$", route)
            if match:
                self.send_json(request_run(int(match.group(1)), payload.get('task_id')))
                return
            match = re.match(r"^/api/runs/([\w-]+)/approve-dispatch$", route)
            if match:
                run = one("SELECT * FROM runs WHERE id=?", (match.group(1),))
                if not run or run["status"] != "awaiting_dispatch": raise ValueError("Run is not awaiting dispatch")
                blockers = execution_blockers(project_or_404(run['project_id']), one('SELECT * FROM tasks WHERE id=?', (run['task_id'],)))
                if blockers:
                    update_run(run['id'], 'blocked', ' '.join(blockers))
                    self.send_json({'ok': False, 'message': ' '.join(blockers)})
                    return
                claim_run(run['id'], run['status'], 'queued', 'Dispatch approved; starting harness.')
                threading.Thread(target=run_attempt, args=(run["id"], run["project_id"], run["task_id"]), daemon=True).start()
                self.send_json({"ok": True}); return
            match = re.match(r'^/api/runs/([\w-]+)/retry-permissions$', route)
            if match:
                with DB_LOCK:
                    run = one('SELECT * FROM runs WHERE id=?', (match.group(1),))
                    if not run or run['status']!='stopped':
                        raise ValueError('Only a stopped permission-blocked run can be retried')
                    latest = one('SELECT id FROM runs WHERE task_id=? ORDER BY rowid DESC LIMIT 1', (run['task_id'],))
                    if latest['id'] != run['id'] or current_run(run['project_id']):
                        raise ValueError('A newer or active run exists. Use the latest task state.')
                    attempt = one('SELECT * FROM attempts WHERE id=?', (run['attempt_id'],))
                    if not attempt or attempt['harness_key']!='claude' or present_attempt(attempt).get('display_status')!='Needs permission':
                        raise ValueError('This retry action is only for Claude permission denials')
                    if not Path(attempt['worktree_path']).is_dir():
                        raise ValueError('The previous working folder no longer exists')
                    if payload.get('mode')!='auto':
                        raise ValueError('Choose automatic permission review explicitly')
                    claim_run(run['id'],'stopped','queued','Retrying Claude with automatic permission review. Existing work is preserved.')
                    execute("UPDATE runs SET permission_override='auto' WHERE id=?",(run['id'],))
                    execute("INSERT INTO task_messages(task_id,role,content,created_at) VALUES(?,'system',?,?)",(run['task_id'],'You requested a retry with Claude automatic permission review for this run only.',now()))
                threading.Thread(target=run_attempt,args=(run['id'],run['project_id'],run['task_id'],attempt['id'],True),daemon=True).start()
                self.send_json({'ok':True});return
            match = re.match(r"^/api/runs/([\w-]+)/(?:approve-commit|complete)$", route)
            if match:
                run = one("SELECT * FROM runs WHERE id=?", (match.group(1),))
                if not run or run["status"] not in ('awaiting_commit', 'awaiting_review'): raise ValueError("Run is not awaiting review")
                claim_run(run['id'], run['status'], 'committing', 'Finishing reviewed run.')
                threading.Thread(target=finish_commit, args=(run["id"], run["project_id"], run["task_id"], run["attempt_id"]), daemon=True).start()
                self.send_json({"ok": True}); return
            match = re.match(r"^/api/runs/([\w-]+)/resume$", route)
            if match:
                run = one("SELECT * FROM runs WHERE id=?", (match.group(1),))
                if not run or run["status"] not in ('awaiting_resume', 'paused_cooldown'): raise ValueError("Run is not awaiting a resume decision")
                claim_run(run['id'], run['status'], 'queued', 'Resuming this task on the next available harness.')
                threading.Thread(target=run_attempt, args=(run["id"], run["project_id"], run["task_id"], run["attempt_id"]), daemon=True).start()
                self.send_json({"ok": True}); return
            match = re.match(r"^/api/runs/([\w-]+)/discard$", route)
            if match:
                run = one("SELECT * FROM runs WHERE id=?", (match.group(1),))
                if not run: raise ValueError("Run not found")
                if run['status'] in ('running', 'verifying', 'queued', 'rotating', 'committing'):
                    raise ValueError('Cannot discard a worktree while its worker is active')
                if run["attempt_id"]:
                    attempt = one("SELECT * FROM attempts WHERE id=?", (run["attempt_id"],))
                    if attempt: cleanup_worktree(Path(project_or_404(run["project_id"])["repo_path"]), Path(attempt["worktree_path"]))
                    execute("UPDATE attempts SET status='discarded', ended_at=COALESCE(ended_at,?) WHERE id=?", (now(), run["attempt_id"]))
                update_run(run["id"], "discarded", "Run closed; task remains pending. Local project files were not reverted.")
                self.send_json({"ok": True}); return
            match = re.match(r"^/api/harnesses/(\w+)$", route)
            if match:
                key = match.group(1)
                if key not in ADAPTERS: raise ValueError("Unknown harness")
                if payload.get("enabled") and not ADAPTERS[key]["runnable"]:
                    raise ValueError('This adapter is unavailable.')
                if 'cooldown_until' in payload and payload['cooldown_until'] is not None:
                    raise ValueError('Only clearing a cooldown is supported')
                if 'tool_permissions' in payload:
                    if payload['tool_permissions'] not in ('standard','auto','ask'):
                        raise ValueError('Unknown permission mode; bypass is not offered')
                    problem = permission_blocker(key, payload['tool_permissions'])
                    if problem:
                        raise ValueError(problem)
                fields = {name: payload[name] for name in ("enabled", "chain_position", "model", "cooldown_until", "tool_permissions") if name in payload}
                if not fields: raise ValueError("No harness updates supplied")
                assignments = ", ".join(f"{name}=?" for name in fields) + ", updated_at=?"
                execute(f"UPDATE harnesses SET {assignments} WHERE key=?", tuple(fields.values()) + (now(), key))
                self.send_json({"ok": True}); return
            match = re.match(r"^/api/tasks/(\d+)$", route)
            if match:
                task_id = int(match.group(1))
                task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
                if not task:
                    raise ValueError("Task not found")
                fields = {name: payload[name] for name in ("text", "mode_override", "preferred_harness", "preferred_model", "force_gate", "degradable", "tool_permissions", "execution_target") if name in payload}
                if 'tool_permissions' in fields and fields['tool_permissions'] not in ('inherit','standard','auto','ask'):
                    raise ValueError('Unknown task permission mode')
                if not fields: raise ValueError("No task updates supplied")
                if "text" in fields:
                    fields["text"] = str(fields["text"]).strip()[:120]
                    if not fields["text"]:
                        raise ValueError("Task name cannot be empty")
                if fields.get("mode_override") not in (None, "", "supervised", "unattended"):
                    raise ValueError("Mode must be supervised or unattended")
                if fields.get('execution_target') not in (None, 'project', 'local', 'coder'):
                    raise ValueError('Execution target must be project default, local, or Coder.')
                assignments = ", ".join(f"{name}=?" for name in fields)
                execute(f"UPDATE tasks SET {assignments} WHERE id=?", tuple(fields.values()) + (task_id,))
                self.send_json({"ok": True}); return
            match = re.match(r"^/api/projects/(\d+)/task-order$", route)
            if match:
                project_id = int(match.group(1))
                order = payload.get("order")
                tasks = rows("SELECT id FROM tasks WHERE project_id=? ORDER BY task_order", (project_id,))
                expected = {item["id"] for item in tasks}
                if not isinstance(order, list) or set(order) != expected or len(order) != len(expected):
                    raise ValueError("Order must contain each task in this project exactly once")
                with DB_LOCK, db() as conn:
                    for position, task_id in enumerate(order):
                        conn.execute("UPDATE tasks SET task_order=? WHERE id=? AND project_id=?", (-(position + 1), task_id, project_id))
                    for position, task_id in enumerate(order):
                        conn.execute("UPDATE tasks SET task_order=? WHERE id=? AND project_id=?", (position, task_id, project_id))
                self.send_json({"ok": True}); return
            raise ValueError("Unknown API route")
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_DELETE(self) -> None:
        route = urlparse(self.path).path
        try:
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self.send_json({"error": "Requests must come from this dashboard"}, 403)
                return
            match = re.match(r"^/api/tasks/(\d+)$", route)
            if not match:
                raise ValueError("Unknown API route")
            task_id = int(match.group(1))
            task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
            if not task:
                raise ValueError("Task not found")
            active = current_run(task["project_id"])
            if active and active["task_id"] == task_id:
                raise ValueError("Finish or close this task's active run before deleting it")
            with DB_LOCK, db() as conn:
                conn.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
                conn.execute("DELETE FROM attempts WHERE task_id=?", (task_id,))
                conn.execute("DELETE FROM runs WHERE task_id=?", (task_id,))
                conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                remaining = conn.execute("SELECT id FROM tasks WHERE project_id=? ORDER BY task_order", (task["project_id"],)).fetchall()
                for position, item in enumerate(remaining):
                    conn.execute("UPDATE tasks SET task_order=? WHERE id=?", (position, item["id"]))
            self.send_json({"ok": True})
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)


def sync_project_tasks(project_id: int) -> None:
    project = project_or_404(project_id)
    repo = Path(project["repo_path"])
    if not (repo / "TASKS.md").exists():
        return
    parsed = parse_tasks_file(repo)
    existing = {item["task_order"]: item for item in rows("SELECT * FROM tasks WHERE project_id=?", (project_id,))}
    for order, (line, text, checked) in enumerate(parsed):
        if order in existing:
            execute("UPDATE tasks SET text=?, source_line=? WHERE id=?", (text, line, existing[order]["id"]))
        else:
            execute("INSERT INTO tasks(project_id,text,task_order,source_line,status,created_at) VALUES(?,?,?,?,?,?)",
                    (project_id, text, order, line, "completed" if checked else "pending", now()))


def create_server(port: int) -> ThreadingHTTPServer:
    try:
        return ThreadingHTTPServer((HOST, port), API)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(f"Port {port} is busy; choosing a free port.", flush=True)
        return ThreadingHTTPServer((HOST, 0), API)


def main() -> None:
    global DATA_ROOT, DB_PATH
    parser = argparse.ArgumentParser(description="Run the local Harness Rotation dashboard")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--data-dir", type=Path, default=DATA_ROOT)
    args = parser.parse_args()
    DATA_ROOT = args.data_dir.resolve()
    DB_PATH = DATA_ROOT / "state.sqlite3"
    init_db()
    execute("UPDATE runs SET status='stopped',message='The server restarted during this run. Project files were preserved. Review them before retrying.',updated_at=? WHERE status IN ('running','queued','verifying','rotating','committing')", (now(),))
    execute("UPDATE attempts SET status='interrupted',ended_at=? WHERE status='running'", (now(),))
    scan_harnesses()
    os.chdir(STATIC_ROOT)
    server = create_server(args.port)
    print(f"Harness Rotation is running at http://{HOST}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        for process in list(CHILDREN):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        server.server_close()


if __name__ == "__main__":
    main()
