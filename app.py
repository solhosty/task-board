#!/usr/bin/env python3
"""Harness Rotation v1: local project sessions, SQLite state, multi-harness runner."""

from __future__ import annotations

import json
import argparse
import errno
import hashlib
import mimetypes
import os
import re
import shutil
import shlex
import signal
import subprocess
import threading
import time
import uuid
from queue import Empty
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, quote, parse_qs
from urllib.request import Request, urlopen
from events import read_events
from harness_rotation import persistence
from harness_rotation.adapters import (
    ADAPTERS,
    configured_command as build_configured_command,
    permission_blocker as adapter_permission_blocker,
)
from harness_rotation.coder_bridges import (
    CoderExternalAuthRequired,
    REMOTE_CODEX_BIN,
    RemoteClaudeLogin,
    RemoteCodexAppServer,
)
from harness_rotation.run_state import REMOTE_SLOT_STATUSES, RunStateStore
from harness_rotation.harness_output import (
    decode_result as decode_harness_result,
    log_details as read_log_details,
)
from harness_rotation import attachments as attachment_store
from harness_rotation import memories as memory_store
from harness_rotation import remote_transport
from harness_rotation import worktrees
from harness_rotation.sessions import SessionService
from harness_rotation.coder_config import (
    slug, normalize_coder_url, coder_template_name, public_coder_server, safe_install_url,
)
from harness_rotation.credentials import (
    KEYCHAIN_SERVICE, keychain_available, keychain_account,
    save_coder_token, remove_coder_token, read_coder_token,
)
from infra.runner.remote_worktree import validate_source


def memory_scope(value: Optional[str]) -> Optional[Dict[str, Any]]:
    """Resolve a memory scope key from the UI into a project row, or None for
    the global store."""
    if not value or value == "global":
        return None
    match = re.fullmatch(r"project:(\d+)", value)
    if not match:
        raise ValueError("Unknown memory scope")
    return project_or_404(int(match.group(1)))


def harness_order_scope(value: Optional[str]) -> Tuple[str, int]:
    """Parse a scope key from the UI into (scope, id).  Global has no id."""
    if not value or value == "global":
        return "global", 0
    match = re.fullmatch(r"(project|task):(\d+)", value or "")
    if not match:
        raise ValueError("Unknown harness order scope")
    scope, scope_id = match.group(1), int(match.group(2))
    if scope == "project":
        project_or_404(scope_id)
    elif not one("SELECT id FROM tasks WHERE id=?", (scope_id,)):
        raise ValueError("Task not found")
    return scope, scope_id


def memory_harness(value: str) -> str:
    if value not in ("claude", "codex"):
        raise ValueError("That harness does not keep memory Aludra can edit")
    return value


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
CODER_RUNNER_LOCK = threading.RLock()
CHILDREN = set()
REMOTE_RESULT_MARKER = '__HARNESS_REMOTE_RESULT__'
REMOTE_DELIVERY_MARKER = '__HARNESS_REMOTE_DELIVERY_RESULT__'
RUNNER_CAPACITY_REFRESH_SECONDS = 60

QUOTA_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in [
        r"usage limit", r"quota.{0,40}(exceeded|limit|reset)", r"rate limit.{0,80}(retry|wait|reset)",
        r"too many requests", r"weekly.{0,30}limit", r"you.?ve hit.{0,30}limit",
    ]
]

CODER_SETUP_PROFILES = ("auto", "python", "node")
AUTH_FLOWS = {}
AUTH_FLOW_LOCK = threading.RLock()
MODEL_AUTH_FLOWS = {}
MODEL_AUTH_LOCK = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    persistence.initialize(DATA_ROOT, DB_PATH, ADAPTERS, now)


def db():
    """Compatibility connection factory used by tests and maintenance scripts."""
    return persistence.connect(DB_PATH)


def rows(sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    return persistence.fetch_all(DB_PATH, DB_LOCK, sql, params)


def one(sql: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
    return persistence.fetch_one(DB_PATH, DB_LOCK, sql, params)


def execute(sql: str, params: Tuple[Any, ...] = ()) -> int:
    return persistence.execute(DB_PATH, DB_LOCK, sql, params)


def session_service() -> SessionService:
    """Build the domain service from the app's replaceable infrastructure seams."""
    return SessionService(rows, one, execute, now, git, read_events)


def sessions_for_task(task_id: int) -> List[Dict[str, Any]]:
    return session_service().for_task(task_id)


def active_session(task_id: int) -> Dict[str, Any]:
    return session_service().active(task_id)


def session_message_chars(session_id: int) -> int:
    return session_service().message_chars(session_id)


def workspace_snapshot(root: Path) -> Dict[str, Any]:
    return session_service().workspace_snapshot(root)


def session_attempt_summary(session_id: int) -> List[Dict[str, Any]]:
    return session_service().attempt_summary(session_id)


def handoff_pack(task: Dict[str, Any], session: Dict[str, Any], root: Optional[Path], snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return session_service().handoff_pack(task, session, root, snapshot)


def seal_session(task: Dict[str, Any], session: Dict[str, Any], reason: str, root: Optional[Path], snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return session_service().seal(task, session, reason, root, snapshot)


def rotate_session(task: Dict[str, Any], reason: str, root: Optional[Path], snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return session_service().rotate(task, reason, root, snapshot)


def latest_handoff(task_id: int) -> Optional[Dict[str, Any]]:
    return session_service().latest_handoff(task_id)


def reconcile_session_snapshot(task: Dict[str, Any], session: Dict[str, Any], actual: Dict[str, Any]) -> Tuple[bool, str]:
    return session_service().reconcile_snapshot(task, session, actual)


def reconcile_session(task: Dict[str, Any], session: Dict[str, Any], root: Path) -> Tuple[bool, str]:
    return session_service().reconcile(task, session, root)


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


def coder_profile_for_server(server_id: int) -> Dict[str, Any]:
    profile = one('SELECT * FROM project_coder_profiles WHERE coder_server_id=? AND enabled=1 ORDER BY project_id LIMIT 1', (server_id,))
    if not profile:
        raise ValueError('Enable this Coder server for a project before connecting a model.')
    return profile


def effective_execution_backend(project: Dict[str, Any], task: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
    profile = project_coder_profile(project['id'])
    target = task.get('execution_target') or 'project'
    if target == 'local':
        return 'local', profile
    if target == 'coder':
        return 'coder', profile
    return ('coder' if profile and profile['enabled'] and profile.get('default_target') == 'coder' else 'local'), profile


def coder_json(base_url: str, path: str, token: Optional[str] = None) -> Dict[str, Any]:
    headers = {'Accept': 'application/json'}
    if token:
        headers['Coder-Session-Token'] = token
    request = Request(base_url.rstrip('/') + path, headers=headers)
    with urlopen(request, timeout=4) as response:
        payload = json.loads(response.read().decode('utf-8'))
    return payload if isinstance(payload, dict) else {'value': payload}


def coder_external_auth_status(server: Dict[str, Any], provider: str = 'github') -> Dict[str, Any]:
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', provider):
        raise ValueError('Invalid Coder external-auth provider.')
    token = read_coder_token(server)
    if not token:
        raise ValueError('The Coder token is unavailable from Keychain.')
    payload = coder_json(server['base_url'], f'/api/v2/external-auth/{provider}', token)
    return {
        'provider': provider,
        'display_name': payload.get('display_name') or provider.replace('-', ' ').title(),
        'type': payload.get('type') or 'external',
        'authenticated': bool(payload.get('authenticated')),
        'login_url': f"{server['base_url'].rstrip('/')}/external-auth/{provider}",
        'install_url': safe_install_url(payload.get('app_install_url')),
        'installation_count': len(payload.get('installations') or []),
        'app_installable': bool(payload.get('app_installable')),
    }


def device_exchange(server, token, provider, device_code):
    request = Request(server['base_url'].rstrip('/') + f'/api/v2/external-auth/{provider}/device',
                      data=json.dumps({'device_code': device_code}).encode(),
                      headers={'Coder-Session-Token': token, 'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(request, timeout=10) as response:
            response.read()
        return 'complete'
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get('detail')
        except (ValueError, AttributeError):
            detail = None
        if detail in ('authorization_pending', 'slow_down', 'expired_token', 'access_denied'):
            return detail
        raise ValueError(f'Coder device exchange failed (HTTP {exc.code}). Check Coder server logs.') from None


def start_device_flow(server, provider):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', provider):
        raise ValueError('Invalid provider ID')
    key = (server['id'], provider)
    with AUTH_FLOW_LOCK:
        previous = AUTH_FLOWS.get(key)
        if previous and previous['status'] == 'pending' and previous['expires_at'] > time.time():
            return dict(previous)
        token = read_coder_token(server)
        if not token:
            raise ValueError('Saved Coder token is unavailable')
        device = coder_json(server['base_url'], f'/api/v2/external-auth/{provider}/device', token)
        url = safe_install_url(device.get('verification_uri'))
        if not url or not device.get('device_code') or not device.get('user_code'):
            raise ValueError('Coder did not return a supported device authorization challenge')
        flow = {'status': 'pending', 'user_code': device['user_code'], 'verification_url': url,
                'expires_at': time.time() + int(device.get('expires_in') or 900),
                'message': 'Waiting for GitHub/provider approval. Harness will finish the connection automatically.'}
        AUTH_FLOWS[key] = flow
        threading.Thread(target=finish_device_flow,
                         args=(server, token, provider, device['device_code'], flow, max(5, int(device.get('interval') or 5))),
                         daemon=True).start()
        return dict(flow)


def finish_device_flow(server, token, provider, device_code, flow, interval):
    try:
        while time.time() < flow['expires_at']:
            time.sleep(interval)
            if time.time() >= flow['expires_at']:
                break
            result = device_exchange(server, token, provider, device_code)
            if result == 'authorization_pending':
                continue
            if result == 'slow_down':
                interval += 5
                continue
            with AUTH_FLOW_LOCK:
                flow['status'] = 'complete' if result == 'complete' else 'failed'
                flow['message'] = 'Authorization saved in Coder. Repository access is a separate check.' if result == 'complete' else result.replace('_', ' ')
            return
        with AUTH_FLOW_LOCK:
            flow.update(status='expired', message='Code expired. Start a new connection.')
    except Exception:
        with AUTH_FLOW_LOCK:
            flow.update(status='failed', message='Coder could not finish the device exchange. Check server connectivity and retry.')


def coder_external_auth_providers(server: Dict[str, Any]) -> List[Dict[str, Any]]:
    token = read_coder_token(server)
    if not token:
        raise ValueError('The Coder token is unavailable from Keychain.')
    payload = coder_json(server['base_url'], '/api/v2/external-auth', token)
    providers = payload.get('providers') or []
    result = []
    for provider in providers:
        provider_id = str(provider.get('id') or '') if isinstance(provider, dict) else ''
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', provider_id):
            continue
        status = coder_external_auth_status(server, provider_id)
        status['display_name'] = provider.get('display_name') or status['display_name']
        status['type'] = provider.get('type') or status['type']
        result.append(status)
    return result


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
                  'runnable': value['runnable'],
                  'attachment_note': value.get('attachment_note', '')} for key, value in ADAPTERS.items()}


def run_state_store() -> RunStateStore:
    """Build the run store from the app's replaceable persistence seams."""
    return RunStateStore(rows, one, execute, now, project_coder_profile)


def current_run(project_id: int) -> Optional[Dict[str, Any]]:
    return run_state_store().current(project_id)


def active_runs(project_id: int) -> List[Dict[str, Any]]:
    return run_state_store().active(project_id)


def remote_parallel_capacity(profile: Optional[Dict[str, Any]]) -> int:
    return run_state_store().remote_capacity(profile)


def runner_slot_runs(coder_server_id: int) -> List[Dict[str, Any]]:
    return run_state_store().runner_slots(coder_server_id)


def run_backend(run_id: str) -> Optional[str]:
    return run_state_store().backend(run_id)


def execution_lease(run_id: str) -> Optional[Dict[str, Any]]:
    return run_state_store().lease(run_id)


def run_connection_action(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return run_state_store().connection_action(run)


def task_pull_requests(task_id: int) -> List[Dict[str, Any]]:
    return rows("SELECT * FROM task_pull_requests WHERE task_id=? ORDER BY id DESC", (task_id,))


def pull_request_status(project: Dict[str, Any], pull_request: Dict[str, Any]) -> Dict[str, Any]:
    if pull_request['provider'] != 'github':
        raise ValueError('Only GitHub pull-request sync is supported in this phase.')
    remote_worktree = one('SELECT * FROM coder_task_worktrees WHERE task_id=?', (pull_request['task_id'],))
    if remote_worktree:
        profile = project_coder_profile(project['id'])
        if not profile or not profile.get('coder_server_id'):
            raise ValueError('This remote task no longer has a Coder profile for pull-request sync.')
        runner, environment = ensure_coder_runner(coder_server_or_404(profile['coder_server_id']), profile)
        payload = run_remote_pr_status(runner, environment, remote_pr_status_request(pull_request, profile))
        raw_state = str(payload.get('state', '')).lower()
        pr_state = 'merged' if payload.get('merged_at') or raw_state == 'merged' else 'closed' if raw_state == 'closed' else 'open'
        return {'url': payload.get('url') or pull_request['url'], 'number': str(payload.get('number') or '') or None,
                'branch_name': payload.get('branch_name'), 'head_sha': payload.get('head_sha'), 'state': pr_state,
                'review_state': 'pending' if pr_state == 'open' else 'unknown', 'merged_at': payload.get('merged_at')}
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
    return run_state_store().create_lease(run_id, task_id, backend)


def raise_missing_lease(run_id: str) -> Dict[str, Any]:
    raise RuntimeError(f'Execution lease was not created for run {run_id}')


def bind_local_execution_lease(run_id: str, worktree: Path, base_sha: Optional[str]) -> None:
    run_state_store().bind_local(run_id, worktree, base_sha)


def coder_runner_context(server: Dict[str, Any]):
    token = read_coder_token(server)
    if not token:
        raise ValueError('The Coder token is unavailable from Keychain.')
    owner = coder_json(server['base_url'], '/api/v2/users/me', token)
    if not owner.get('id'):
        raise ValueError('Coder did not identify the authenticated user.')
    organizations = coder_json(server['base_url'], '/api/v2/organizations', token).get('value', [])
    matches = [org for org in organizations if server['organization'] in (org.get('id'), org.get('name'))
               or (server['organization'] == 'default' and org.get('is_default'))]
    if len(matches) != 1:
        raise ValueError('The registered Coder organization could not be resolved uniquely.')
    owner['_runner_organization_id'] = matches[0]['id']
    runner = one('''SELECT * FROM coder_runners WHERE coder_server_id=? AND deployment_url=?
        AND organization=? AND owner_id=?''',
        (server['id'], server['base_url'], server['organization'], owner['id']))
    return token, owner, runner


def validate_runner_workspace(server, owner, workspace):
    if workspace.get('owner_id') != owner['id']:
        raise ValueError('A runner must belong to the authenticated Coder user.')
    expected_org = owner.get('_runner_organization_id') or server['organization']
    if expected_org not in (workspace.get('organization_id'), workspace.get('organization_name')):
        raise ValueError('The runner must belong to the registered Coder organization.')
    if workspace.get('shared_with'):
        raise ValueError('Use a private workspace for the runner, not a shared workspace.')
    if not workspace.get('id') or not workspace.get('name'):
        raise ValueError('Coder returned incomplete workspace metadata.')


def save_coder_runner(server, owner, workspace, allow_migration=False):
    validate_runner_workspace(server, owner, workspace)
    with CODER_RUNNER_LOCK:
        existing = one('''SELECT * FROM coder_runners WHERE coder_server_id=? AND deployment_url=?
            AND organization=? AND owner_id=?''',
            (server['id'], server['base_url'], server['organization'], owner['id']))
        if existing and existing['workspace_id'] != workspace['id']:
            if not allow_migration:
                raise ValueError('This account already has a runner. Moving its tasks and logins requires an explicit migration.')
            workspace_url = server['base_url'].rstrip('/') + '/@' + quote(owner['username'], safe='') + '/' + quote(workspace['name'], safe='')
            execute('''UPDATE coder_runners SET workspace_id=?,workspace_name=?,workspace_url=?,template_name=?,
                detected_cpu_count=NULL,detected_memory_bytes=NULL,detected_max_tasks=NULL,capacity_checked_at=NULL,updated_at=? WHERE id=?''',
                    (workspace['id'], workspace['name'], workspace_url, workspace.get('template_name') or '', now(), existing['id']))
            return one('SELECT * FROM coder_runners WHERE id=?', (existing['id'],))
        workspace_url = server['base_url'].rstrip('/') + '/@' + quote(owner['username'], safe='') + '/' + quote(workspace['name'], safe='')
        execute('''INSERT INTO coder_runners(coder_server_id,deployment_url,organization,owner_id,
            workspace_id,workspace_name,workspace_url,template_name,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(coder_server_id,deployment_url,organization,owner_id)
            DO UPDATE SET workspace_name=excluded.workspace_name,workspace_url=excluded.workspace_url,
            template_name=excluded.template_name,updated_at=excluded.updated_at''',
            (server['id'], server['base_url'], server['organization'], owner['id'], workspace['id'],
             workspace['name'], workspace_url, workspace.get('template_name') or '', now(), now()))
        return one('SELECT * FROM coder_runners WHERE coder_server_id=? AND deployment_url=? AND organization=? AND owner_id=?',
                   (server['id'], server['base_url'], server['organization'], owner['id']))


def runner_capacity(runner: Dict[str, Any]) -> int:
    return max(1, int(runner.get('detected_max_tasks') or 1))


def refresh_runner_capacity(runner: Dict[str, Any], environment: Dict[str, str], force: bool = False) -> Dict[str, Any]:
    """Measure limits from inside the runner; a failed probe preserves the last safe value."""
    checked = runner.get('capacity_checked_at')
    if not force and checked:
        try:
            if (datetime.now(timezone.utc) - datetime.fromisoformat(checked)).total_seconds() < RUNNER_CAPACITY_REFRESH_SECONDS:
                return runner
        except ValueError:
            pass
    command = shlex.join(['python3', '-'])
    probed = subprocess.run(['coder', 'ssh', '--wait', 'yes', runner['workspace_name'], '--', command],
                            input=remote_transport.payload(APP_ROOT, 'remote_runner_capacity.py'), env=environment,
                            capture_output=True, text=True, timeout=45)
    if probed.returncode:
        return runner
    try:
        details = json.loads(probed.stdout)
        cpu = int(details['cpu_count'])
        memory = int(details['memory_bytes']) if details.get('memory_bytes') else None
        capacity = int(details['max_tasks'])
        per_task = int(details['memory_per_task_bytes'])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return runner
    if cpu < 1 or capacity < 1 or per_task != 2 * 1024 ** 3 or capacity > cpu:
        return runner
    if memory is not None and memory < 1:
        return runner
    execute('''UPDATE coder_runners SET detected_cpu_count=?,detected_memory_bytes=?,detected_max_tasks=?,capacity_checked_at=?,updated_at=? WHERE id=?''',
            (cpu, memory, capacity, now(), now(), runner['id']))
    return one('SELECT * FROM coder_runners WHERE id=?', (runner['id'],))


def refresh_saved_runner_capacity(server: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Refresh an existing runner before scheduling; never create one just to inspect capacity."""
    token, _, runner = coder_runner_context(server)
    if not runner:
        return None
    environment = dict(os.environ, CODER_URL=server['base_url'], CODER_SESSION_TOKEN=token,
                       CODER_ORGANIZATION=server['organization'])
    return refresh_runner_capacity(runner, environment)


def ensure_coder_runner(server, profile):
    with CODER_RUNNER_LOCK:
        token, owner, runner = coder_runner_context(server)
        environment = dict(os.environ, CODER_URL=server['base_url'], CODER_SESSION_TOKEN=token,
                           CODER_ORGANIZATION=server['organization'])
        if runner:
            workspace = coder_json(server['base_url'], '/api/v2/workspaces/' + quote(runner['workspace_id'], safe=''), token)
            validate_runner_workspace(server, owner, workspace)
            if workspace['id'] != runner['workspace_id']:
                raise ValueError('The saved runner no longer matches Coder. Recovery is required.')
            if profile.get('template_name') and workspace.get('template_name') and workspace['template_name'] != profile['template_name']:
                raise ValueError('The saved runner uses a different template. Use runner migration; the old workspace will be preserved.')
            # Keep the same workspace ID through renames; never replace a missing runner.
            runner = save_coder_runner(server, owner, workspace)
        else:
            identity = json.dumps([server['base_url'], server['organization'], owner['id']])
            name = 'harness-runner-' + hashlib.sha256(identity.encode()).hexdigest()[:12]
            path = '/api/v2/users/me/workspace/' + name
            try:
                workspace = coder_json(server['base_url'], path, token)
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                # Bootstrap once from the approved blueprint, without a project checkout.
                command = ['coder', 'create', name, '--template', profile['template_name'], '--stop-after', '8h', '--yes']
                created = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=300)
                if created.returncode:
                    raise RuntimeError('Coder runner creation failed. Inspect its build in Coder; no existing runner was deleted.')
                workspace = coder_json(server['base_url'], path, token)
            runner = save_coder_runner(server, owner, workspace)
        return refresh_runner_capacity(runner, environment), environment


def migrate_coder_runner(server: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    """Create a fresh private runner from the selected template; never delete the old one."""
    with CODER_RUNNER_LOCK:
        token, owner, previous = coder_runner_context(server)
        template = str(profile.get('template_name') or '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', template):
            raise ValueError('Choose a valid Coder template name before migrating.')
        marker = json.dumps([server['base_url'], server['organization'], owner['id'], template, uuid.uuid4().hex])
        name = 'harness-runner-' + hashlib.sha256(marker.encode()).hexdigest()[:12]
        environment = dict(os.environ, CODER_URL=server['base_url'], CODER_SESSION_TOKEN=token,
                           CODER_ORGANIZATION=server['organization'])
        created = subprocess.run(['coder', 'create', name, '--template', template, '--stop-after', '8h', '--yes'],
                                 env=environment, capture_output=True, text=True, timeout=600)
        if created.returncode:
            raise RuntimeError('Coder could not create the new runner. The old runner remains unchanged; inspect the template build in Coder.')
        workspace = coder_json(server['base_url'], '/api/v2/users/me/workspace/' + quote(name, safe=''), token)
        runner = refresh_runner_capacity(save_coder_runner(server, owner, workspace, allow_migration=True), environment, force=True)
        return {'runner': runner, 'previous_workspace_id': previous['workspace_id'] if previous else None,
                'previous_workspace_name': previous['workspace_name'] if previous else None}


def remote_codex_account(runner: Dict[str, Any], environment: Dict[str, str], refresh: bool = False) -> Dict[str, Any]:
    """Read only the public account shape; never return an access token."""
    installed = subprocess.run(['coder', 'ssh', '--wait', 'yes', runner['workspace_name'], '--',
                               'test', '-x', REMOTE_CODEX_BIN], env=environment,
                              capture_output=True, text=True, timeout=45)
    if installed.returncode:
        return {'installed': False, 'authenticated': False,
                'detail': 'Codex is not installed in this runner. Publish a template revision with Codex before connecting it.'}
    bridge = RemoteCodexAppServer(runner['workspace_name'], environment)
    try:
        result = bridge.request('account/read', {'refreshToken': refresh})
        account = result.get('account') if isinstance(result.get('account'), dict) else None
        return {'installed': True, 'authenticated': bool(account),
                'auth_mode': account.get('type') if account else None,
                'plan_type': account.get('planType') if account else None,
                'email': account.get('email') if account else None,
                'requires_openai_auth': bool(result.get('requiresOpenaiAuth'))}
    finally:
        bridge.close()


def remote_claude_account(runner: Dict[str, Any], environment: Dict[str, str]) -> Dict[str, Any]:
    """Ask Claude Code for its native status without reading its credentials."""
    present = subprocess.run(['coder', 'ssh', '--wait', 'yes', runner['workspace_name'], '--',
                              'ls', '-l', '/home/coder/.local/bin/claude'], env=environment,
                              capture_output=True, text=True, timeout=45)
    if present.returncode or 'claude' not in present.stdout:
        return {'installed': False, 'authenticated': False, 'detail': 'Claude Code is not installed in this runner.'}
    checked = subprocess.run(['coder', 'ssh', '--wait', 'yes', runner['workspace_name'], '--',
                              '/home/coder/.local/bin/claude', 'auth', 'status', '--json'], env=environment,
                             capture_output=True, text=True, timeout=45)
    try:
        payload = json.loads(checked.stdout)
    except (ValueError, TypeError):
        return {'installed': checked.returncode == 0, 'authenticated': checked.returncode == 0,
                'detail': 'Claude Code did not return a readable native status.'}
    # Keep only non-sensitive display fields and tolerate CLI version differences.
    return {'installed': True, 'authenticated': bool(payload.get('loggedIn') or payload.get('authenticated') or payload.get('account')),
            'auth_mode': str(payload.get('authMethod') or payload.get('auth_method') or '') or None,
            'email': payload.get('email') if isinstance(payload.get('email'), str) else None,
            'detail': None}


def model_auth_status(server: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    runner, environment = ensure_coder_runner(server, profile)
    try:
        codex = remote_codex_account(runner, environment)
    except Exception as exc:
        codex = {'installed': False, 'authenticated': False, 'detail': 'Codex status could not be read from this runner.'}
    try:
        claude = remote_claude_account(runner, environment)
    except Exception:
        claude = {'installed': False, 'authenticated': False, 'detail': 'Claude Code status could not be read from this runner.'}
    return {'runner': {'id': runner['id'], 'workspace_id': runner['workspace_id'], 'workspace_name': runner['workspace_name'],
                       'workspace_url': runner['workspace_url']}, 'providers': {'codex': codex, 'claude': claude}}


def start_remote_codex_login(server: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    runner, environment = ensure_coder_runner(server, profile)
    probe = remote_codex_account(runner, environment)
    if not probe.get('installed'):
        raise ValueError(probe.get('detail') or 'Codex is not installed in this runner.')
    if probe.get('authenticated'):
        return {'status': 'complete', 'provider': 'codex', 'message': 'Codex is already connected in this persistent runner.'}
    key = (server['id'], runner['workspace_id'], 'codex')
    with MODEL_AUTH_LOCK:
        prior = MODEL_AUTH_FLOWS.get(key)
        if prior and prior['status'] == 'pending':
            return {k: v for k, v in prior.items() if k != 'bridge'}
        bridge = RemoteCodexAppServer(runner['workspace_name'], environment)
        try:
            result = bridge.request('account/login/start', {'type': 'chatgptDeviceCode'})
        except Exception:
            bridge.close()
            raise
        url, code, login_id = result.get('verificationUrl'), result.get('userCode'), result.get('loginId')
        parsed = urlparse(str(url or ''))
        if not login_id or not code or parsed.scheme != 'https' or parsed.hostname != 'auth.openai.com':
            bridge.close()
            raise RuntimeError('Codex returned an unsupported login challenge.')
        flow = {'status': 'pending', 'provider': 'codex', 'login_id': login_id,
                'verification_url': url, 'user_code': code, 'message': 'Waiting for ChatGPT approval in the browser.',
                'bridge': bridge, 'started_at': time.time()}
        MODEL_AUTH_FLOWS[key] = flow
        threading.Thread(target=watch_remote_codex_login, args=(key, flow), daemon=True).start()
        return {k: v for k, v in flow.items() if k != 'bridge'}


def watch_remote_codex_login(key, flow) -> None:
    bridge: RemoteCodexAppServer = flow['bridge']
    try:
        while time.time() - flow['started_at'] < 15 * 60:
            try:
                notice = bridge.notifications.get(timeout=5)
            except Empty:
                continue
            if notice.get('method') != 'account/login/completed':
                continue
            params = notice.get('params') or {}
            if params.get('loginId') != flow['login_id']:
                continue
            with MODEL_AUTH_LOCK:
                flow['status'] = 'complete' if params.get('success') else 'failed'
                flow['message'] = 'Codex is connected in this persistent runner.' if params.get('success') else 'Codex login did not complete.'
            return
        with MODEL_AUTH_LOCK:
            flow.update(status='expired', message='The Codex sign-in expired. Start it again.')
    finally:
        bridge.close()


def remote_codex_login_flow(server_id: int, workspace_id: str) -> Optional[Dict[str, Any]]:
    with MODEL_AUTH_LOCK:
        flow = MODEL_AUTH_FLOWS.get((server_id, workspace_id, 'codex'))
        return {k: v for k, v in flow.items() if k != 'bridge'} if flow else None


def start_remote_claude_login(server: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    runner, environment = ensure_coder_runner(server, profile)
    probe = remote_claude_account(runner, environment)
    if not probe.get('installed'):
        raise ValueError(probe.get('detail') or 'Claude Code is not installed in this runner.')
    if probe.get('authenticated'):
        return {'status': 'complete', 'provider': 'claude', 'message': 'Claude Code is already connected in this persistent runner.'}
    key = (server['id'], runner['workspace_id'], 'claude')
    with MODEL_AUTH_LOCK:
        prior = MODEL_AUTH_FLOWS.get(key)
        if prior and prior['status'] == 'pending':
            return {k: v for k, v in prior.items() if k != 'bridge'}
        bridge = RemoteClaudeLogin(runner['workspace_name'], environment)
        flow = {'status': 'pending', 'provider': 'claude', 'message': 'Claude Code is starting its native sign-in.',
                'bridge': bridge, 'started_at': time.time()}
        MODEL_AUTH_FLOWS[key] = flow
        threading.Thread(target=advance_remote_claude_onboarding, args=(flow,), daemon=True).start()
        threading.Thread(target=watch_remote_claude_login, args=(key, flow), daemon=True).start()
        return {k: v for k, v in flow.items() if k != 'bridge'}


def advance_remote_claude_onboarding(flow) -> None:
    """Skip Claude's cosmetic first-run theme picker only when it is visible."""
    bridge: RemoteClaudeLogin = flow['bridge']
    for _ in range(40):
        if bridge.process.poll() is not None:
            return
        compact = re.sub(r'\s+', '', bridge.snapshot()).lower()
        if 'choosethetextstyle' in compact:
            bridge.accept_default()
            with MODEL_AUTH_LOCK:
                if flow['status'] == 'pending':
                    flow['message'] = 'Preparing Claude Code sign-in.'
            threading.Thread(target=advance_remote_claude_login_method, args=(flow,), daemon=True).start()
            return
        time.sleep(.25)


def advance_remote_claude_login_method(flow) -> None:
    """Accept Claude's default subscription login method after first-run setup."""
    bridge: RemoteClaudeLogin = flow['bridge']
    for _ in range(40):
        if bridge.process.poll() is not None:
            return
        compact = re.sub(r'\s+', '', bridge.snapshot()).lower()
        if 'selectloginmethod' in compact and 'claudecodecanbeused' in compact:
            bridge.accept_default()
            with MODEL_AUTH_LOCK:
                if flow['status'] == 'pending':
                    flow['message'] = 'Opening Claude browser sign-in.'
            return
        time.sleep(.25)


def watch_remote_claude_login(key, flow) -> None:
    bridge: RemoteClaudeLogin = flow['bridge']
    try:
        while time.time() - flow['started_at'] < 15 * 60:
            if bridge.process.poll() is not None:
                with MODEL_AUTH_LOCK:
                    if flow['status'] == 'pending':
                        flow.update(status='finished', message='Claude Code closed before the dashboard confirmed authentication.')
                return
            time.sleep(1)
        with MODEL_AUTH_LOCK:
            flow.update(status='expired', message='The Claude sign-in session expired. Start it again.')
    finally:
        bridge.close()


def remote_claude_login_flow(server_id: int, workspace_id: str) -> Optional[Dict[str, Any]]:
    with MODEL_AUTH_LOCK:
        flow = MODEL_AUTH_FLOWS.get((server_id, workspace_id, 'claude'))
        if not flow:
            return None
        result = {k: v for k, v in flow.items() if k != 'bridge'}
        bridge = flow.get('bridge')
        if bridge and result['status'] == 'pending':
            result['screen'] = bridge.snapshot()
            result['verification_url'] = bridge.verification_url()
        return result


def send_remote_claude_login_input(server_id: int, workspace_id: str, value: str) -> Dict[str, Any]:
    with MODEL_AUTH_LOCK:
        flow = MODEL_AUTH_FLOWS.get((server_id, workspace_id, 'claude'))
        if not flow or flow['status'] != 'pending':
            raise ValueError('No active Claude Code sign-in is waiting for input.')
        bridge = flow['bridge']
    bridge.send(value)
    return remote_claude_login_flow(server_id, workspace_id) or {'status': 'pending'}


def reserve_runner_worktree(run_id, task, runner, repo_url):
    """Snapshot task ownership and enforce the measured process-slot upper bound."""
    with DB_LOCK, db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        occupied = conn.execute('''SELECT COUNT(*) FROM execution_leases l JOIN runs r ON r.id=l.run_id
            WHERE l.runner_id=? AND r.id!=? AND r.status IN ('queued','running','verifying','rotating','committing')''',
            (runner['id'], run_id)).fetchone()[0]
        if occupied >= runner_capacity(runner):
            raise ValueError('Runner capacity is occupied. Existing tasks are preserved; retry after a slot is free.')
        saved = conn.execute('SELECT * FROM coder_task_worktrees WHERE task_id=?', (task['id'],)).fetchone()
        if saved and (saved['runner_id'] != runner['id'] or saved['repo_url'] != repo_url):
            raise ValueError('This task belongs to another runner or repository. Its work was preserved; create a new task or migrate explicitly.')
        if not saved:
            conn.execute('''INSERT INTO coder_task_worktrees(task_id,runner_id,task_key,repo_url,created_at,updated_at)
                VALUES(?,?,?,?,?,?)''', (task['id'], runner['id'], 'task-' + uuid.uuid4().hex, repo_url, now(), now()))
        conn.execute('''UPDATE execution_leases SET state='provisioning',runner_id=?,workspace_id=?,
            workspace_name=?,workspace_url=?,template_name=?,updated_at=? WHERE run_id=?''',
            (runner['id'], runner['workspace_id'], runner['workspace_name'], runner['workspace_url'], runner['template_name'], now(), run_id))
    return one('SELECT * FROM coder_task_worktrees WHERE task_id=?', (task['id'],))


def provision_coder_execution(run_id: str, project: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
    profile = project_coder_profile(project['id'])
    if not profile or not profile.get('coder_server_id'):
        raise ValueError('This project has no Coder environment configured.')
    server = coder_server_or_404(profile['coder_server_id'])
    repo_url = profile.get('repo_url') or ''
    validate_source(repo_url)
    legacy = one("""SELECT id FROM execution_leases WHERE task_id=? AND backend='coder'
        AND workspace_name IS NOT NULL AND runner_id IS NULL LIMIT 1""", (task['id'],))
    if legacy:
        raise ValueError('This task has a legacy task workspace. It was preserved; create a new task or explicitly migrate its work to a persistent runner.')
    runner, environment = ensure_coder_runner(server, profile)
    saved = reserve_runner_worktree(run_id, task, runner, repo_url)
    request = {'repo_url': repo_url, 'base_ref': profile.get('base_ref') or 'main', 'task_key': saved['task_key']}
    command = shlex.join(['python3', '-', json.dumps(request)])
    checked = subprocess.run(['coder', 'ssh', '--wait', 'yes', runner['workspace_name'], '--', command],
                             input=remote_transport.payload(APP_ROOT, 'remote_worktree.py'), env=environment,
                             capture_output=True, text=True, timeout=300)
    if checked.returncode:
        execute("UPDATE execution_leases SET state='failed',updated_at=? WHERE run_id=?", (now(), run_id))
        raise RuntimeError('Runner task checkout failed. Check repository access and branch; existing work was preserved.')
    result = json.loads(checked.stdout)
    expected_path = '/home/coder/.harness-runner/tasks/' + saved['task_key']
    if result.get('worktree_path') != expected_path or not re.fullmatch(r'[a-f0-9]{40,64}', result.get('base_sha', '')):
        raise RuntimeError('Runner returned an unexpected worktree or Git revision.')
    execute('''UPDATE coder_task_worktrees SET worktree_path=?,base_sha=?,branch_name=?,updated_at=? WHERE task_id=?''',
            (result['worktree_path'], result['base_sha'], result['branch_name'], now(), task['id']))
    execute("UPDATE execution_leases SET state='ready',worktree_path=?,base_sha=?,updated_at=? WHERE run_id=?",
            (result['worktree_path'], result['base_sha'], now(), run_id))
    return {'runner': runner, 'environment': environment,
            'worktree': one('SELECT * FROM coder_task_worktrees WHERE task_id=?', (task['id'],))}


def remote_workspace_snapshot(runner: Dict[str, Any], environment: Dict[str, str], worktree_path: str) -> Dict[str, Any]:
    return remote_transport.workspace_snapshot(runner, environment, worktree_path, APP_ROOT)





def saved_remote_workspace_snapshot(project: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
    """Inspect an existing task worktree; never create a runner just to rotate a session."""
    saved = one('SELECT * FROM coder_task_worktrees WHERE task_id=?', (task['id'],))
    profile = project_coder_profile(project['id'])
    if not saved or not saved.get('worktree_path') or not profile or not profile.get('coder_server_id'):
        return {'available': False, 'reason': 'The remote task worktree is unavailable.'}
    try:
        server = coder_server_or_404(profile['coder_server_id'])
        token, _, runner = coder_runner_context(server)
    except (ValueError, HTTPError, URLError, TimeoutError, OSError):
        return {'available': False, 'reason': 'Could not access the saved Coder runner.'}
    if not runner or runner['id'] != saved['runner_id']:
        return {'available': False, 'reason': 'The saved Coder runner no longer matches this task.'}
    environment = dict(os.environ, CODER_URL=server['base_url'], CODER_SESSION_TOKEN=token,
                       CODER_ORGANIZATION=server['organization'])
    return remote_workspace_snapshot(runner, environment, saved['worktree_path'])


def choose_remote_harness(task: Dict[str, Any], runner: Dict[str, Any], environment: Dict[str, str],
                          excluded_keys: Tuple[str, ...] = (), locked_key: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
    """Choose only CLIs installed and signed in inside this runner, never locally."""
    configured = {item['key']: item for item in rows("SELECT * FROM harnesses WHERE enabled=1 AND key IN ('codex','claude')")}
    order = ([locked_key] if locked_key else [])
    order += ([task['preferred_harness']] if task.get('preferred_harness') else [])
    order += [key for key in ('codex', 'claude') if key not in order]
    statuses = {}
    for key in order:
        if key in excluded_keys:
            continue
        harness = configured.get(key)
        if not harness:
            continue
        status = remote_codex_account(runner, environment) if key == 'codex' else remote_claude_account(runner, environment)
        statuses[key] = status
        if status.get('installed') and status.get('authenticated'):
            selected = with_effective_model(harness, model_snapshot(task))
            if locked_key == key:
                return selected, 'remote session continuity'
            return selected, 'remote preferred' if task.get('preferred_harness') == key else 'remote fallback'
    details = '; '.join(f"{key}: {value.get('detail') or ('not connected' if not value.get('authenticated') else 'unavailable')}" for key, value in statuses.items())
    raise ValueError('No supported authenticated harness is available in this persistent runner. Connect Codex or Claude Code in Coder first.' + (f' ({details})' if details else ''))


def remote_agent_request(harness: Dict[str, Any], worktree: Dict[str, Any], task: Dict[str, Any], project: Dict[str, Any], permission_mode: str,
                         attachments: Optional[List[Dict[str, Any]]] = None,
                         screenshot_path: str = '') -> str:
    return remote_transport.agent_request(harness, worktree, task_prompt(task, project, attachments, screenshot_path), project, permission_mode, attachments, screenshot_path)


def stage_remote_attachments(runner: Dict[str, Any], environment: Dict[str, str], worktree: Dict[str, Any], task_id: int) -> List[Dict[str, Any]]:
    """Return remote paths for every stored attachment, or fail before an agent starts."""
    records = rows("SELECT * FROM task_attachments WHERE task_id=? ORDER BY id", (task_id,))
    existing = attachment_store.existing(records)
    if len(existing) != len(records):
        raise RuntimeError('An attachment is no longer stored locally. Remove it or add it again before retrying.')
    if not existing:
        return []
    return remote_transport.stage_attachments(runner, environment, worktree['task_key'], existing, APP_ROOT)





def run_remote_agent(runner: Dict[str, Any], environment: Dict[str, str], command: str, output_file: Path) -> Tuple[Dict[str, Any], str]:
    return remote_transport.run_agent(runner, environment, command, output_file, APP_ROOT, REMOTE_RESULT_MARKER)





def remote_delivery_request(worktree: Dict[str, Any], task: Dict[str, Any], profile: Dict[str, Any]) -> str:
    return remote_transport.delivery_request(worktree, task, profile)





def run_remote_delivery(runner: Dict[str, Any], environment: Dict[str, str], command: str) -> Dict[str, Any]:
    return remote_transport.run_delivery(runner, environment, command, APP_ROOT, REMOTE_DELIVERY_MARKER)





def remote_pr_status_request(pull_request: Dict[str, Any], profile: Dict[str, Any]) -> str:
    return remote_transport.pr_status_request(pull_request, profile)





def run_remote_pr_status(runner: Dict[str, Any], environment: Dict[str, str], command: str) -> Dict[str, Any]:
    return remote_transport.run_pr_status(runner, environment, command, APP_ROOT, REMOTE_DELIVERY_MARKER)





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
    # Do this outside the scheduler lock: Coder SSH may take a moment, and an
    # existing runner is enough to measure without provisioning anything.
    preflight_project = project_or_404(project_id)
    preflight_task = one("SELECT * FROM tasks WHERE project_id=? AND status='pending'" + (' AND id=?' if task_id else ' ORDER BY task_order LIMIT 1'),
                         (project_id, int(task_id)) if task_id else (project_id,))
    if preflight_task:
        preflight_backend, preflight_profile = effective_execution_backend(preflight_project, preflight_task)
        if preflight_backend == 'coder' and preflight_profile and preflight_profile.get('coder_server_id'):
            try:
                refresh_saved_runner_capacity(coder_server_or_404(preflight_profile['coder_server_id']))
            except (ValueError, RuntimeError, HTTPError, URLError, OSError, subprocess.TimeoutExpired):
                # Execution blockers and provisioning report Coder failures; a
                # failed telemetry read must not discard a known safe capacity.
                pass
    with DB_LOCK:
        project = project_or_404(project_id)
        active = active_runs(project_id)
        busy = active[0] if active else None
        if busy and task_id and busy['task_id'] == int(task_id):
            return {'run_id': busy['id'], 'status': busy['status'], 'message': busy['message']}
        task = one("SELECT * FROM tasks WHERE project_id=? AND status='pending'" + (' AND id=?' if task_id else ' ORDER BY task_order LIMIT 1'),
                   (project_id, int(task_id)) if task_id else (project_id,))
        if not task:
            raise ValueError('No pending task to run')
        mode = effective_mode(project, task)
        blockers = execution_blockers(project, task)
        backend, profile = effective_execution_backend(project, task)
        capacity_full = False
        if active:
            if backend != 'coder' or any(run_backend(run['id']) != 'coder' for run in active):
                blockers.append('Another task in this project has an active or paused run. Finish that run first.')
            elif len(runner_slot_runs(profile['coder_server_id'])) >= remote_parallel_capacity(profile):
                capacity_full = True
        harness, _ = choose_harness(task)
        permissions = permission_snapshot(task)
        if harness:
            problem = permission_blocker(harness['key'], permissions[harness['key']])
            if problem:
                blockers.append(problem)
        status = 'blocked' if blockers else ('awaiting_capacity' if capacity_full else ('awaiting_dispatch' if mode == 'supervised' or task['force_gate'] else 'queued'))
        message = ' '.join(blockers) if blockers else (
            f'The persistent Coder runner is full ({remote_parallel_capacity(profile)} task(s)). Waiting for the next slot.' if status == 'awaiting_capacity'
            else f"Ready to start {harness['label']} ({harness['model']}). Approve dispatch to begin." if status == 'awaiting_dispatch'
            else 'Queued for execution.')
        run_id = str(uuid.uuid4())
        execute('INSERT INTO runs(id,project_id,task_id,mode,status,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                (run_id, project_id, task['id'], mode, status, message, now(), now()))
        execute('UPDATE runs SET permissions_json=? WHERE id=?', (json.dumps(permissions), run_id))
        create_execution_lease(run_id, task['id'], backend)
    if status == 'queued':
        threading.Thread(target=run_attempt, args=(run_id, project_id, task['id']), daemon=True).start()
    return {'run_id': run_id, 'status': status, 'message': message}


def update_run(run_id: str, status: str, message: str, attempt_id: Optional[int] = None) -> None:
    execute("UPDATE runs SET status=?, message=?, attempt_id=COALESCE(?,attempt_id), updated_at=? WHERE id=?",
            (status, message, attempt_id, now(), run_id))
    if status in ('complete', 'stopped', 'discarded', 'blocked'):
        servers = rows('''SELECT DISTINCT p.coder_server_id FROM execution_leases l
            JOIN runs r ON r.id=l.run_id JOIN project_coder_profiles p ON p.project_id=r.project_id
            WHERE l.run_id=? AND l.backend='coder' AND p.coder_server_id IS NOT NULL''', (run_id,))
        for server in servers:
            dispatch_capacity_waiters(server['coder_server_id'])


def dispatch_capacity_waiters(coder_server_id: int) -> None:
    """Promote FIFO remote work when an admitted task releases a runner slot."""
    to_start = []
    with DB_LOCK:
        with db() as conn:
            runner = conn.execute('''SELECT detected_max_tasks FROM coder_runners
                WHERE coder_server_id=? ORDER BY rowid DESC LIMIT 1''', (coder_server_id,)).fetchone()
            capacity = max(1, int(runner['detected_max_tasks'] or 1)) if runner else 1
            placeholders = ','.join('?' for _ in REMOTE_SLOT_STATUSES)
            occupied = conn.execute(f'''SELECT COUNT(*) FROM runs r JOIN execution_leases l ON l.run_id=r.id
                JOIN project_coder_profiles p ON p.project_id=r.project_id
                WHERE l.backend='coder' AND r.status IN ({placeholders}) AND p.coder_server_id=?''',
                (*REMOTE_SLOT_STATUSES, coder_server_id)).fetchone()[0]
            slots = capacity - occupied
            if slots < 1:
                return
            waiting = conn.execute('''SELECT r.*,t.force_gate FROM runs r JOIN execution_leases l ON l.run_id=r.id
                JOIN project_coder_profiles p ON p.project_id=r.project_id JOIN tasks t ON t.id=r.task_id
                WHERE l.backend='coder' AND p.coder_server_id=? AND r.status='awaiting_capacity'
                ORDER BY r.created_at,r.rowid''', (coder_server_id,)).fetchall()
            for row in waiting[:slots]:
                run = dict(row)
                next_status = 'awaiting_dispatch' if run['mode'] == 'supervised' or run['force_gate'] else 'queued'
                message = ('A runner slot is available. Approve dispatch to begin.' if next_status == 'awaiting_dispatch'
                           else 'A runner slot is available. Starting execution.')
                changed = conn.execute("UPDATE runs SET status=?,message=?,updated_at=? WHERE id=? AND status='awaiting_capacity'",
                                       (next_status, message, now(), run['id']))
                if changed.rowcount and next_status == 'queued':
                    to_start.append((run['id'], run['project_id'], run['task_id']))
    for args in to_start:
        threading.Thread(target=run_attempt, args=args, daemon=True).start()


def recover_capacity_waiters() -> None:
    """Upgrade previously blocked capacity-only runs into the durable FIFO queue."""
    with DB_LOCK, db() as conn:
        conn.execute("""UPDATE runs SET status='awaiting_capacity',
            message='Waiting for the next available Coder runner slot.',updated_at=?
            WHERE status='blocked' AND message LIKE 'The persistent Coder runner has reached its environment capacity (%'""", (now(),))
        servers = conn.execute('''SELECT DISTINCT p.coder_server_id FROM runs r
            JOIN execution_leases l ON l.run_id=r.id JOIN project_coder_profiles p ON p.project_id=r.project_id
            WHERE l.backend='coder' AND r.status='awaiting_capacity' AND p.coder_server_id IS NOT NULL''').fetchall()
    for server in servers:
        dispatch_capacity_waiters(server['coder_server_id'])


def claim_run(run_id: str, expected: str, status: str, message: str) -> None:
    with DB_LOCK, db() as conn:
        result = conn.execute('UPDATE runs SET status=?,message=?,updated_at=? WHERE id=? AND status=?',
                              (status, message, now(), run_id, expected))
        if result.rowcount != 1:
            raise ValueError('This run already changed state. Refresh before trying again.')


def global_harness_order() -> List[str]:
    return [item["key"] for item in rows("SELECT key FROM harnesses ORDER BY chain_position")]


def scoped_harness_order(scope: str, scope_id: int) -> Optional[List[str]]:
    """The ordering stored for one scope, or None when it inherits."""
    stored = rows("SELECT harness_key FROM harness_orders WHERE scope=? AND scope_id=? ORDER BY position",
                  (scope, scope_id))
    keys = [item["harness_key"] for item in stored if item["harness_key"] in ADAPTERS]
    if not keys:
        return None
    # A harness added since this override was saved is unknown to it; append it
    # in global order so a new harness is never silently dropped from a scope.
    return keys + [key for key in global_harness_order() if key not in keys]


def resolve_harness_order(task: Optional[Dict[str, Any]] = None) -> Tuple[List[str], str]:
    """Task order, else project order, else the global chain."""
    # Callers sometimes pass a partial task (smoke tests, synthetic rotation
    # checks); an absent id simply means there is no override at that scope.
    if task and task.get("id"):
        own = scoped_harness_order("task", task["id"])
        if own:
            return own, "task"
    if task and task.get("project_id"):
        project = scoped_harness_order("project", task["project_id"])
        if project:
            return project, "project"
    return global_harness_order(), "global"


def set_harness_order(scope: str, scope_id: int, order: List[str]) -> None:
    if not isinstance(order, list) or set(order) != set(ADAPTERS) or len(order) != len(ADAPTERS):
        raise ValueError("Order must contain each harness exactly once")
    with DB_LOCK, db() as conn:
        conn.execute("DELETE FROM harness_orders WHERE scope=? AND scope_id=?", (scope, scope_id))
        for position, key in enumerate(order):
            conn.execute(
                "INSERT INTO harness_orders(scope,scope_id,harness_key,position,updated_at) VALUES(?,?,?,?,?)",
                (scope, scope_id, key, position, now()))


def clear_harness_order(scope: str, scope_id: int) -> None:
    execute("DELETE FROM harness_orders WHERE scope=? AND scope_id=?", (scope, scope_id))


def eligible_harnesses(task: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    order, _ = resolve_harness_order(task)
    by_key = {item["key"]: item for item in rows("SELECT * FROM harnesses WHERE enabled=1 AND installed=1")}
    all_items = [by_key[key] for key in order if key in by_key]
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
    candidates = eligible_harnesses(task)
    models = model_snapshot(task)
    if task["preferred_harness"]:
        for candidate in candidates:
            if candidate["key"] == task["preferred_harness"]:
                return with_effective_model(candidate, models), "preferred"
    return (with_effective_model(candidates[0], models), "fallback") if candidates else (None, "none")


def choose_session_harness(task: Dict[str, Any], session: Dict[str, Any], allow_failover: bool = False) -> Tuple[Optional[Dict[str, Any]], str]:
    """Keep normal session rollover on its established harness.

    The generic rotation pool is consulted only before a session has a harness,
    after a quota/unavailability failover, or when the established harness is
    no longer runnable.
    """
    key = session.get('harness_key')
    if key and not allow_failover:
        chosen = one('SELECT * FROM harnesses WHERE key=?', (key,))
        if chosen and chosen.get('enabled') and chosen.get('installed') and harness_availability(chosen)['code'] == 'ready':
            if chosen.get('cooldown_until') and datetime.fromisoformat(chosen['cooldown_until']) > datetime.now(timezone.utc):
                chosen = None
            else:
                # A model chosen after this session opened applies to its next
                # attempt; only an unchanged configuration keeps the recorded one.
                chosen = with_effective_model(chosen, model_snapshot(task))
                return chosen, 'session continuity'
    return choose_harness(task)


def task_attachments(task_id: int) -> List[Dict[str, Any]]:
    """Every attachment still on disk for this task, oldest first.

    Attachments stay with the task rather than one session: a screenshot added
    before a rollover is usually still the thing being worked on afterwards.
    """
    return attachment_store.existing(
        rows("SELECT * FROM task_attachments WHERE task_id=? ORDER BY id", (task_id,)))


def task_attachment_details(task_id: int) -> List[Dict[str, Any]]:
    """Attachment records for the UI, including locally missing files it can remove."""
    records = rows("SELECT * FROM task_attachments WHERE task_id=? ORDER BY id", (task_id,))
    return [dict(item, available=Path(item.get('stored_path') or '').is_file()) for item in records]


# A new task carries its first files in the create request itself, so they are
# stored before the harness can start; a larger set is added from the task page.
MAX_NEW_TASK_ATTACHMENTS = 10


def store_task_attachment(task_id: int, session_id: int, filename: str, data: bytes,
                          message_id: Optional[int] = None) -> int:
    stored = attachment_store.save(DATA_ROOT, task_id, filename, data)
    return execute("""INSERT INTO task_attachments(task_id,session_id,message_id,filename,media_type,kind,
        byte_size,sha256,stored_path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (task_id, session_id, message_id, stored['filename'], stored['media_type'], stored['kind'],
         stored['byte_size'], stored['sha256'], stored['stored_path'], now()))


def task_attachment_or_404(task_id: int, attachment_id: int) -> Dict[str, Any]:
    record = one("SELECT * FROM task_attachments WHERE id=? AND task_id=?", (attachment_id, task_id))
    if not record:
        raise ValueError("Attachment not found")
    return record


RESULT_SCREENSHOT_MAX_BYTES = 10 * 1024 * 1024


def result_screenshot_path(task_id: int, attempt_id: int, remote_task_key: Optional[str] = None) -> Path:
    """The one non-Git PNG location advertised to a UI-capable harness."""
    if remote_task_key:
        return Path('/home/coder/.harness-runner/results') / remote_task_key / str(attempt_id) / 'after.png'
    return DATA_ROOT / 'results' / ('task-%d' % task_id) / str(attempt_id) / 'after.png'


def save_attempt_screenshot(attempt_id: int, path: Path, encoded: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Persist a captured after-state only when it is a bounded PNG artifact."""
    try:
        data = attachment_store.decode(encoded) if encoded else path.read_bytes()
    except (OSError, ValueError):
        return None
    if not data or len(data) > RESULT_SCREENSHOT_MAX_BYTES or not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return None
    attempt = one('SELECT task_id FROM attempts WHERE id=?', (attempt_id,))
    if not attempt:
        return None
    target = result_screenshot_path(attempt['task_id'], attempt_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    execute('DELETE FROM attempt_result_screenshots WHERE attempt_id=?', (attempt_id,))
    screenshot_id = execute("""INSERT INTO attempt_result_screenshots(attempt_id,filename,media_type,byte_size,stored_path,created_at)
        VALUES(?,?,?,?,?,?)""", (attempt_id, 'after.png', 'image/png', len(data), str(target), now()))
    return one('SELECT * FROM attempt_result_screenshots WHERE id=?', (screenshot_id,))


def attempt_screenshots(attempt_id: int) -> List[Dict[str, Any]]:
    return [dict(item, available=Path(item['stored_path']).is_file()) for item in
            rows('SELECT * FROM attempt_result_screenshots WHERE attempt_id=? ORDER BY id', (attempt_id,))]


def task_prompt(task: Dict[str, Any], project: Dict[str, Any], attachments: Optional[List[Dict[str, Any]]] = None,
                screenshot_path: str = '') -> str:
    session = active_session(task['id'])
    messages = rows("SELECT role,content FROM task_messages WHERE session_id=? ORDER BY id", (session['id'],))
    conversation = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)
    current_progress = json.dumps(session_attempt_summary(session['id']), indent=2)[-12000:]
    handoff = latest_handoff(task['id'])
    handoff_context = json.dumps(handoff, indent=2)[-16000:] if handoff else 'No previous session.'
    described = attachment_store.describe(task_attachments(task['id']) if attachments is None else attachments)
    attached = ('\n' + described + '\n') if described else ''
    return f"""You are continuing a task session in the working directory provided to this process.

Task: {task['text']}

Conversation:
{conversation or task['text']}
{attached}

Previous sealed-session handoff (may be incomplete; verify against the code):
{handoff_context}

Current-session harness progress (may be incomplete; verify against the code):
{current_progress or 'No harness attempts in this session.'}

Read the existing code and current diff before editing. Another harness may have worked on this same session. Preserve all existing uncommitted and untracked work. Work only inside this project folder. Do not commit, stash, reset, clean, or push. Keep changes focused and run relevant tests. Before meaningful tool batches, send a short user-visible progress update explaining what you are checking or changing; do not reveal private chain-of-thought. For a user-facing UI task, use browser tooling to capture the finished route at 1440x900 and save one PNG to {screenshot_path or 'the result-screenshot path supplied by the runner'}. This path is outside the project and will be displayed in the completion chat; do not add it to Git. If the UI cannot be run or captured, do not fabricate an image; explain that in your final response. Finish with a concise summary of changes and tests run. If permissions prevent completing the task, clearly report that rather than claiming success."""


def log_file(attempt_id: int) -> Path:
    directory = DATA_ROOT / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"attempt-{attempt_id}.log"


def permission_blocker(key: str, mode: str) -> Optional[str]:
    return adapter_permission_blocker(ADAPTERS, key, mode)


def permission_snapshot(task: Dict[str, Any]) -> Dict[str, str]:
    requested = task.get('tool_permissions') or 'inherit'
    return {h['key']: (h.get('tool_permissions') or 'standard') if requested == 'inherit' else requested
            for h in rows('SELECT * FROM harnesses')}


MODEL_TABLES = {'project': ('project_harness_models', 'project_id'), 'task': ('task_harness_models', 'task_id')}


def scoped_models(scope: str, owner_id: int) -> Dict[str, str]:
    """The model choices recorded at one scope. A missing key means inherit."""
    table, column = MODEL_TABLES[scope]
    return {item['harness_key']: item['model'] for item in
            rows('SELECT harness_key,model FROM %s WHERE %s=?' % (table, column), (owner_id,))}


def save_scoped_model(scope: str, owner_id: int, key: str, model: Optional[str]) -> None:
    """Record or clear one scope's model choice. Clearing restores inheritance
    rather than freezing the wider scope's current value."""
    table, column = MODEL_TABLES[scope]
    if model is None:
        execute('DELETE FROM %s WHERE %s=? AND harness_key=?' % (table, column), (owner_id, key))
        return
    execute('INSERT INTO %s(%s,harness_key,model,updated_at) VALUES(?,?,?,?) '
            'ON CONFLICT(%s,harness_key) DO UPDATE SET model=excluded.model,updated_at=excluded.updated_at'
            % (table, column, column), (owner_id, key, model, now()))


def requested_model(value: Any) -> Optional[str]:
    """A model choice is an explicit ID, or None for "inherit the wider scope"."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == 'inherit':
        return None
    if len(text) > 160:
        raise ValueError('Model ID is too long')
    return text


def model_snapshot(task: Optional[Dict[str, Any]] = None, project_id: Optional[int] = None) -> Dict[str, Dict[str, str]]:
    """Effective model per harness, with the scope that decided it.

    Rotation membership stays global; each scope only narrows which model the
    harness runs. A project narrows it for its own work and a task narrows it
    again for one goal, so nothing a user sets leaks outward.
    """
    snapshot = {item['key']: {'model': item['model'] or 'default', 'source': 'global'}
                for item in rows('SELECT key,model FROM harnesses')}
    owner = project_id if project_id is not None else (task or {}).get('project_id')
    for scope, owner_id in (('project', owner), ('task', (task or {}).get('id'))):
        for key, model in (scoped_models(scope, owner_id).items() if owner_id else ()):
            if key in snapshot:
                snapshot[key] = {'model': model, 'source': scope}
    if task and task.get('preferred_model') and task.get('preferred_harness') in snapshot:
        # The task's own harness pin predates scoped models and still applies.
        if snapshot[task['preferred_harness']]['source'] != 'task':
            snapshot[task['preferred_harness']] = {'model': task['preferred_model'], 'source': 'task'}
    return snapshot


def with_effective_model(harness: Dict[str, Any], snapshot: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    chosen = dict(harness)
    chosen['model'] = snapshot.get(harness['key'], {}).get('model') or harness.get('model') or 'default'
    return chosen


def configured_command(harness: Dict[str, Any], root: Path, prompt: str, override: Optional[str] = None,
                       memory: Optional[str] = None,
                       attachments: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    return build_configured_command(ADAPTERS, harness, root, prompt, override, memory, attachments)


def stream_process(command: List[str], cwd: Path, output_file: Path) -> Tuple[int, str]:
    return worktrees.stream_process(command, cwd, output_file, CHILDREN)


def make_worktree(project: Dict[str, Any], run_id: str) -> Tuple[Path, str, str]:
    return worktrees.make_worktree(project, run_id, WORKTREE_ROOT, git)


def worktree_diff(attempt: Dict[str, Any]) -> str:
    return worktrees.worktree_diff(attempt, git)


def attempt_files(attempt: Dict[str, Any]) -> Dict[str, Any]:
    """The files one attempt produced, as the dashboard shows them.

    A remote attempt keeps its work in the Coder runner, and a merged local one
    may have had its worktree removed, so the answer says why a list is empty
    rather than implying the harness changed nothing.
    """
    root = Path(attempt.get('worktree_path') or '')
    if not root.is_dir():
        remote = str(attempt.get('worktree_path') or '').startswith('/home/coder/')
        return {'files': [], 'available': False,
                'reason': 'This attempt worked in a Coder runner; review its files through the diff and transcript.'
                          if remote else 'This attempt’s workspace is no longer on this machine.'}
    files = []
    for item in worktrees.changed_files(attempt, git):
        media, _ = mimetypes.guess_type(item['path'])
        files.append(dict(item, media_type=media or 'text/plain',
                          kind='image' if (media or '').startswith('image/') else 'file'))
    return {'files': files, 'available': True, 'worktree': str(root)}


def cleanup_worktree(repo: Path, worktree: Path) -> None:
    worktrees.cleanup_worktree(repo, worktree, git)


def decode_result(key: str, output: str) -> Tuple[str, Optional[str]]:
    return decode_harness_result(key, output)


def log_details(attempt: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return read_log_details(attempt, read_events)


def commit_and_merge(project: Dict[str, Any], task: Dict[str, Any], attempt: Dict[str, Any]) -> Tuple[Optional[str], str, str]:
    return worktrees.commit_and_merge(project, task, attempt, git)


def mark_task_complete(repo: Path, task: Dict[str, Any]) -> None:
    worktrees.mark_task_complete(repo, task, git)


def run_attempt(run_id: str, project_id: int, task_id: int, resume_attempt_id: Optional[int] = None, permission_retry: bool = False) -> None:
    try:
        _run_attempt(run_id, project_id, task_id, resume_attempt_id, permission_retry)
    except CoderExternalAuthRequired as exc:
        update_run(run_id, 'awaiting_external_auth', f'Connect {exc.display_name} to this Coder account, then continue. {exc.login_url}')
    except Exception as exc:
        execute("UPDATE attempts SET status='failed',ended_at=?,error=? WHERE run_id=? AND status IN ('running','verified')", (now(), str(exc), run_id))
        update_run(run_id, 'stopped', 'Execution stopped: ' + str(exc))


def _run_attempt(run_id: str, project_id: int, task_id: int, resume_attempt_id: Optional[int] = None, permission_retry: bool = False) -> None:
    """Run a single attempt. Failures are persisted rather than raised into the HTTP thread."""
    preflight_project, preflight_task = project_or_404(project_id), one("SELECT * FROM tasks WHERE id=?", (task_id,))
    assert preflight_task
    preflight_backend, _ = effective_execution_backend(preflight_project, preflight_task)
    # Local tasks share the project folder; Coder tasks use recorded isolated worktrees.
    with (RUN_LOCK if preflight_backend != 'coder' else nullcontext()):
        project, task = project_or_404(project_id), one("SELECT * FROM tasks WHERE id=?", (task_id,))
        assert task
        backend, _ = effective_execution_backend(project, task)
        if backend == 'coder':
            session = active_session(task_id)
            prepared = provision_coder_execution(run_id, project, task)
            runner, environment, remote_worktree = prepared['runner'], prepared['environment'], prepared['worktree']
            snapshot = remote_workspace_snapshot(runner, environment, remote_worktree['worktree_path'])
            if not resume_attempt_id and not permission_retry and task.get('session_budget_chars', 0) and session_message_chars(session['id']) >= int(task['session_budget_chars']):
                session = rotate_session(task, 'context budget reached', None, snapshot)
            if not resume_attempt_id and not permission_retry:
                reconciled, detail = reconcile_session_snapshot(task, session, snapshot)
                if not reconciled:
                    update_run(run_id, 'stopped', 'State needs review. ' + detail)
                    return
            prior = one('SELECT * FROM attempts WHERE id=? AND task_id=?', (resume_attempt_id, task_id)) if resume_attempt_id else None
            excluded = (prior['harness_key'],) if prior and prior.get('harness_key') in ('codex', 'claude') else ()
            harness, selection = choose_remote_harness(task, runner, environment, excluded,
                session.get('harness_key') if not resume_attempt_id else None)
            permissions = json.loads(one('SELECT * FROM runs WHERE id=?', (run_id,))['permissions_json'] or '{}')
            permission_mode = permissions.get(harness['key'], 'standard')
            problem = permission_blocker(harness['key'], permission_mode)
            if problem:
                update_run(run_id, 'stopped', problem)
                return
            try:
                remote_attachments = stage_remote_attachments(runner, environment, remote_worktree, task_id)
            except RuntimeError as exc:
                update_run(run_id, 'blocked', str(exc))
                return
            execute('UPDATE task_sessions SET harness_key=?,model=? WHERE id=?', (harness['key'], harness.get('model'), session['id']))
            attempt_id = execute("""INSERT INTO attempts(task_id,session_id,harness_key,model,selection,status,started_at,worktree_path,branch_name,base_sha,run_id,tool_permissions)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (task_id, session['id'], harness['key'], harness['model'], selection, 'running', now(),
                remote_worktree['worktree_path'], remote_worktree['branch_name'], remote_worktree['base_sha'], run_id, permission_mode))
            execute('UPDATE tasks SET last_attempt_id=? WHERE id=?', (attempt_id, task_id))
            file = log_file(attempt_id)
            execute('UPDATE attempts SET log_path=? WHERE id=?', (str(file), attempt_id))
            execute("INSERT INTO task_messages(task_id,session_id,role,content,created_at,attempt_id) VALUES(?,?,'system',?,?,?)",
                    (task_id, session['id'], f"{harness['label']} started in persistent Coder runner {runner['workspace_name']}.", now(), attempt_id))
            update_run(run_id, 'running', f"{harness['label']} is working in persistent runner {runner['workspace_name']}.", attempt_id)
            remote_screenshot = result_screenshot_path(task_id, attempt_id, remote_worktree.get('task_key') or ('task-%d' % task_id))
            result, output = run_remote_agent(runner, environment, remote_agent_request(
                harness, remote_worktree, task, project, permission_mode, remote_attachments, str(remote_screenshot)), file)
            output += result.get('output') or ''
            if result.get('screenshot'):
                save_attempt_screenshot(attempt_id, remote_screenshot, result['screenshot'])
            decoded_reply, decoded_failure = decode_result(harness['key'], result.get('output') or '')
            reply = result.get('reply') or decoded_reply
            failure = result.get('error') or decoded_failure
            if failure or result.get('code'):
                error = failure or ('Harness exceeded the remote execution timeout.' if result.get('timed_out') else f"Harness exited with code {result.get('code')}.")
                is_quota = any(pattern.search(output) for pattern in QUOTA_PATTERNS)
                outcome = 'quota' if is_quota else 'failed'
                execute("UPDATE attempts SET status=?,ended_at=?,error=?,diff_output=? WHERE id=?", (outcome, now(), error, result.get('diff') or '', attempt_id))
                if not is_quota:
                    update_run(run_id, 'stopped', f'{harness["label"]} stopped. {error}\nFiles remain in persistent runner {runner["workspace_name"]}.', attempt_id)
                    return
                reset = re.search(r'resets? in (\d+)\s*(day|hour|minute)', output, re.I)
                seconds = int(reset[1]) * {'day': 86400, 'hour': 3600, 'minute': 60}[reset[2].lower()] if reset else 4 * 3600
                until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='seconds')
                execute("UPDATE harnesses SET cooldown_until=?,updated_at=? WHERE id=?", (until, now(), harness['id']))
                if effective_mode(project, task) == 'supervised' or task['force_gate'] or not project.get('auto_failover', 1):
                    update_run(run_id, 'awaiting_resume', f'{harness["label"]} reached its limit (retry after {until}). The remote worktree is preserved; resume on the next authenticated CLI.', attempt_id)
                else:
                    update_run(run_id, 'rotating', f'{harness["label"]} reached its limit (retry after {until}). Continuing in the same remote worktree on the next authenticated CLI.', attempt_id)
                    threading.Thread(target=run_attempt, args=(run_id, project_id, task_id, attempt_id), daemon=True).start()
                return
            verification = result.get('verification') or ''
            if result.get('verify_code'):
                execute("UPDATE attempts SET status='verify_failed',ended_at=?,verify_output=?,error=?,diff_output=? WHERE id=?",
                        (now(), verification, f"Verify command exited with code {result.get('verify_code')}", result.get('diff') or '', attempt_id))
                update_run(run_id, 'stopped', f'Verification failed. Files remain in persistent runner {runner["workspace_name"]}.', attempt_id)
                return
            execute("UPDATE attempts SET status='verified',verify_output=?,diff_output=? WHERE id=?", (verification, result.get('diff') or '', attempt_id))
            if reply.strip():
                execute("INSERT INTO task_messages(task_id,session_id,role,content,created_at,attempt_id) VALUES(?,?,'assistant',?,?,?)", (task_id, session['id'], reply.strip(), now(), attempt_id))
            update_run(run_id, 'awaiting_review', f'Verification passed in persistent runner {runner["workspace_name"]}. Review the remote diff before committing or creating a pull request.', attempt_id)
            return
        session = active_session(task_id)
        harness, selection = choose_session_harness(task, session, allow_failover=bool(resume_attempt_id and not permission_retry))
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
        # A normal rollover starts a fresh conversation on the same harness;
        # only quota/unavailability paths may consult the next harness.
        if not resume_attempt_id and not permission_retry and task.get('session_budget_chars', 0) and session_message_chars(session['id']) >= int(task['session_budget_chars']):
            session = rotate_session(task, 'context budget reached', worktree)
            harness, selection = choose_session_harness(task, session)
        if not resume_attempt_id and not permission_retry:
            reconciled, detail = reconcile_session(task, session, worktree)
            if not reconciled:
                update_run(run_id, 'stopped', 'State needs review. ' + detail)
                return
        if not harness:
            update_run(run_id, 'paused_cooldown', 'No eligible harness is available for this task session.')
            return
        if session.get('harness_key') != harness['key'] or session.get('model') != harness.get('model'):
            execute('UPDATE task_sessions SET harness_key=?,model=? WHERE id=?', (harness['key'], harness.get('model'), session['id']))
        bind_local_execution_lease(run_id, worktree, base)
        attempt_id = execute("""INSERT INTO attempts(task_id,session_id,harness_key,model,selection,status,started_at,worktree_path,branch_name,base_sha,run_id)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (task_id, session['id'], harness["key"], harness["model"], selection + (" · resumed" if resume_attempt_id else ""), "running", now(), str(worktree), branch, base, run_id))
        execute("UPDATE tasks SET last_attempt_id=? WHERE id=?", (attempt_id, task_id))
        execute('UPDATE attempts SET tool_permissions=? WHERE id=?', (permission_mode, attempt_id))
        execute("INSERT INTO task_messages(task_id,session_id,role,content,created_at,attempt_id) VALUES(?,?,'system',?,?,?)",
                (task_id, session['id'], f"{harness['label']} started with {'automatic permissions' if permission_mode == 'auto' else 'adapter default permissions'}.", now(), attempt_id))
        file = log_file(attempt_id)
        execute("UPDATE attempts SET log_path=? WHERE id=?", (str(file), attempt_id))
        update_run(run_id, "running", f"{harness['label']} · {harness['model']} is working in {worktree}. Waiting for CLI output…", attempt_id)
        run = one('SELECT * FROM runs WHERE id=?', (run_id,))
        local_screenshot = result_screenshot_path(task_id, attempt_id)
        local_screenshot.parent.mkdir(parents=True, exist_ok=True)
        command = configured_command(harness, worktree, task_prompt(task, project, screenshot_path=str(local_screenshot)), permission_mode,
                                     task.get('memory_mode'), task_attachments(task_id))
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
        if code == 0:
            save_attempt_screenshot(attempt_id, local_screenshot)
        execute("INSERT INTO task_messages(task_id,session_id,role,content,created_at) VALUES(?,?,?,?,?)",
                (task_id, session['id'], "system", f"{harness['label']} attempt #{attempt_id} ended with exit code {code}. See its attempt log for output.", now()))
        if code == 0:
            reply = reply_file.read_text(encoding='utf-8') if reply_file.exists() else reply
            if reply.strip():
                execute("INSERT INTO task_messages(task_id,session_id,role,content,created_at,attempt_id) VALUES(?,?,'assistant',?,?,?)", (task_id, session['id'], reply.strip(), now(), attempt_id))
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
            lease = execution_lease(run_id)
            if lease and lease['backend'] == 'coder':
                profile = project_coder_profile(project_id)
                if not profile:
                    raise RuntimeError('This project no longer has a Coder profile. The remote worktree was preserved.')
                prepared = provision_coder_execution(run_id, project, task)
                result = run_remote_delivery(prepared['runner'], prepared['environment'],
                                             remote_delivery_request(prepared['worktree'], task, profile))
                execute("""INSERT INTO task_pull_requests(task_id,provider,url,number,branch_name,head_sha,state,review_state,created_at,updated_at)
                    VALUES(?,'github',?,?,?,?,'open','pending',?,?) ON CONFLICT(task_id,url) DO UPDATE SET
                    number=excluded.number,branch_name=excluded.branch_name,head_sha=excluded.head_sha,state='open',
                    review_state='pending',sync_error=NULL,updated_at=excluded.updated_at""",
                        (task_id, result['pr_url'], result.get('pr_number'), result.get('branch_name'), result.get('head_sha'), now(), now()))
                execute("UPDATE attempts SET status='completed',ended_at=?,commit_sha=?,diff_stat=?,diff_output=?,error=NULL WHERE id=?",
                        (now(), result['commit_sha'], result.get('diff_stat') or '', result.get('diff') or '', attempt_id))
                execute("UPDATE tasks SET status='completed',last_attempt_id=? WHERE id=?", (attempt_id, task_id))
                update_run(run_id, 'complete', 'Remote task committed, pushed, and opened for review: ' + result['pr_url'], attempt_id)
                return
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


def retry_remote_delivery(run_id: str) -> None:
    """Retry only a failed remote delivery; never run the agent again."""
    run = one('SELECT * FROM runs WHERE id=?', (run_id,))
    if not run or run['status'] != 'stopped':
        raise ValueError('This run is not awaiting delivery recovery.')
    attempt = one('SELECT * FROM attempts WHERE id=? AND run_id=?', (run.get('attempt_id'), run_id))
    lease = execution_lease(run_id)
    if not attempt or attempt['status'] != 'merge_failed' or not lease or lease['backend'] != 'coder':
        raise ValueError('Only a failed remote commit or pull-request delivery can be retried.')
    claim_run(run_id, 'stopped', 'committing', 'Retrying remote delivery from the preserved task commit.')
    threading.Thread(target=finish_commit, args=(run_id, run['project_id'], run['task_id'], attempt['id']), daemon=True).start()


def serialize_project(project: Dict[str, Any]) -> Dict[str, Any]:
    project["tasks"] = rows("SELECT * FROM tasks WHERE project_id=? ORDER BY task_order", (project["id"],))
    project['coder_profile'] = project_coder_profile(project['id'])
    project['board_views'] = [dict(item, config=json.loads(item['config_json'])) for item in rows('SELECT * FROM board_views WHERE project_id=? ORDER BY id', (project['id'],))]
    project['harness_models'] = model_snapshot(project_id=project['id'])
    task_models: Dict[int, Dict[str, str]] = {}
    for item in rows('SELECT m.task_id,m.harness_key,m.model FROM task_harness_models m '
                     'JOIN tasks t ON t.id=m.task_id WHERE t.project_id=?', (project['id'],)):
        task_models.setdefault(item['task_id'], {})[item['harness_key']] = item['model']
    for task in project['tasks']:
        task['harness_models'] = task_models.get(task['id'], {})
        if task['preferred_harness'] in ADAPTERS and task['preferred_model']:
            task['harness_models'].setdefault(task['preferred_harness'], task['preferred_model'])
        history = rows('SELECT harness_key,status,integrity_status FROM task_sessions WHERE task_id=? ORDER BY session_number', (task['id'],))
        task['session_count'] = len(history)
        task['harness_history'] = list(dict.fromkeys(item['harness_key'] for item in history if item['harness_key']))
        task['active_harness'] = next((item['harness_key'] for item in reversed(history) if item['status'] == 'active'), None)
        task['integrity_status'] = history[-1]['integrity_status'] if history else 'not_checked'
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

    def is_dashboard_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        if origin == "http://" + host:
            return True
        # The Vite dev server serves the same product on 5173 and proxies /api
        # here, under either loopback hostname.
        loopback = ("127.0.0.1", "localhost")
        return host.split(":")[0] in loopback and origin in ["http://%s:5173" % name for name in loopback]

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
            if route == "/api/harness-order":
                scope, scope_id = harness_order_scope(
                    parse_qs(urlparse(self.path).query).get("scope", ["global"])[0])
                if scope == "global":
                    self.send_json({"order": global_harness_order(), "source": "global",
                                    "inherited": global_harness_order(), "overridden": False})
                    return
                own = scoped_harness_order(scope, scope_id)
                if scope == "task":
                    task = one("SELECT * FROM tasks WHERE id=?", (scope_id,))
                    inherited = scoped_harness_order("project", task["project_id"]) or global_harness_order()
                else:
                    inherited = global_harness_order()
                self.send_json({"order": own or inherited, "source": scope if own else "inherited",
                                "inherited": inherited, "overridden": bool(own)})
                return
            if route == "/api/memories":
                self.send_json(memory_store.overview(rows("SELECT * FROM projects ORDER BY id DESC")))
                return
            match = re.fullmatch(r"/api/memories/(\w+)", route)
            if match:
                scope = parse_qs(urlparse(self.path).query).get("scope", ["global"])[0]
                self.send_json(memory_store.entries(memory_harness(match.group(1)), memory_scope(scope)))
                return
            match = re.match(r'^/api/coder-servers/(\d+)/runner$', route)
            if match:
                server = coder_server_or_404(int(match.group(1)))
                token, owner, runner = coder_runner_context(server)
                if runner:
                    runner = refresh_runner_capacity(runner, dict(os.environ, CODER_URL=server['base_url'],
                        CODER_SESSION_TOKEN=token, CODER_ORGANIZATION=server['organization']))
                catalog = coder_json(server['base_url'], '/api/v2/workspaces?q=owner%3Ame', token)
                workspaces = []
                for workspace in catalog.get('workspaces', []):
                    try:
                        validate_runner_workspace(server, owner, workspace)
                    except ValueError:
                        continue
                    workspaces.append({'name': workspace['name'], 'id': workspace['id'],
                                       'status': workspace.get('latest_build', {}).get('status', 'unknown')})
                self.send_json({'runner': runner, 'workspaces': workspaces,
                                'execution_ready': False, 'scheduler_limit': runner_capacity(runner) if runner else 1})
                return
            match = re.match(r'^/api/coder-servers/(\d+)/model-auth$', route)
            if match:
                server = coder_server_or_404(int(match.group(1)))
                profile = coder_profile_for_server(server['id'])
                status = model_auth_status(server, profile)
                active = remote_codex_login_flow(server['id'], status['runner']['workspace_id'])
                if active:
                    status['providers']['codex']['flow'] = active
                claude_active = remote_claude_login_flow(server['id'], status['runner']['workspace_id'])
                if claude_active and status['providers']['claude'].get('authenticated') and claude_active['status'] == 'pending':
                    with MODEL_AUTH_LOCK:
                        flow = MODEL_AUTH_FLOWS.get((server['id'], status['runner']['workspace_id'], 'claude'))
                        if flow:
                            flow.update(status='complete', message='Claude Code is connected in this persistent runner.')
                            flow['bridge'].close()
                    claude_active = remote_claude_login_flow(server['id'], status['runner']['workspace_id'])
                if claude_active:
                    status['providers']['claude']['flow'] = claude_active
                self.send_json(status)
                return
            match = re.match(r'^/api/coder-servers/(\d+)/external-auth$', route)
            if match:
                server = coder_server_or_404(int(match.group(1)))
                self.send_json({'providers': coder_external_auth_providers(server)})
                return
            match = re.match(r'^/api/coder-servers/(\d+)/external-auth/([a-z0-9_-]+)/flow$', route)
            if match:
                with AUTH_FLOW_LOCK:
                    self.send_json(dict(AUTH_FLOWS.get((int(match.group(1)), match.group(2))) or {'status': 'idle'}))
                return
            match = re.match(r'^/api/coder-servers/(\d+)/external-auth/([a-z0-9][a-z0-9_-]{0,63})$', route)
            if match:
                server = coder_server_or_404(int(match.group(1)))
                self.send_json(coder_external_auth_status(server, match.group(2)))
                return
            match = re.match(r"^/api/tasks/(\d+)$", route)
            if match:
                task_id = int(match.group(1))
                # Read task/run together before slow availability checks. Otherwise
                # a concurrent completion can pair an old task with a finished run.
                with DB_LOCK:
                    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
                    task_run = one('SELECT * FROM runs WHERE task_id=? ORDER BY rowid DESC LIMIT 1', (task_id,))
                if not task:
                    raise ValueError("Task not found")
                selected, selection = choose_harness(task)
                blockers = execution_blockers(project_or_404(task['project_id']), task)
                if task_run and task_run.get('status') == 'blocked' and task_run.get('message'):
                    blockers.append(task_run['message'])
                if selected:
                    problem = permission_blocker(selected['key'], permission_snapshot(task)[selected['key']])
                    if problem:
                        blockers.append(problem)
                self.send_json({"task": task, "next_harness": {'key': selected['key'], 'label': selected['label'], 'model': selected['model'], 'selection': selection} if selected else None,
                    "harness_models": model_snapshot(task),
                    "run": task_run,
                    "connection_action": run_connection_action(task_run),
                    "lease": execution_lease(task_run['id']) if task_run else None,
                    "pull_requests": task_pull_requests(task_id),
                    "blockers": blockers,
                    "messages": conversation_messages(task_id),
                    "sessions": sessions_for_task(task_id),
                    "attachments": task_attachment_details(task_id),
                    "result_screenshots": {item['id']: attempt_screenshots(item['id']) for item in rows("SELECT id FROM attempts WHERE task_id=?", (task_id,))},
                    "attempts": [present_attempt(a) for a in rows("SELECT * FROM attempts WHERE task_id=? ORDER BY id", (task_id,))]})
                return
            match = re.match(r"^/api/attempts/(\d+)/screenshots/(\d+)$", route)
            if match:
                record = one('SELECT * FROM attempt_result_screenshots WHERE id=? AND attempt_id=?',
                             (int(match.group(2)), int(match.group(1))))
                path = Path((record or {}).get('stored_path') or '')
                if not record or not path.is_file():
                    raise ValueError('That result screenshot is no longer stored on this machine.')
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(path.stat().st_size))
                self.send_header('Content-Disposition', 'inline; filename="after.png"')
                self.end_headers()
                with path.open('rb') as handle:
                    shutil.copyfileobj(handle, self.wfile)
                return
            match = re.match(r"^/api/tasks/(\d+)/attachments/(\d+)$", route)
            if match:
                record = task_attachment_or_404(int(match.group(1)), int(match.group(2)))
                path = Path(record['stored_path'])
                if not path.is_file():
                    raise ValueError('That attachment is no longer stored on this machine.')
                self.send_response(200)
                self.send_header('Content-Type', record['media_type'])
                self.send_header('Content-Length', str(path.stat().st_size))
                # Inline so the dashboard can preview an image; the filename is
                # the sanitized one, never the browser's original text.
                self.send_header('Content-Disposition', 'inline; filename="%s"' % record['filename'])
                self.end_headers()
                with path.open('rb') as handle:
                    shutil.copyfileobj(handle, self.wfile)
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
            match = re.match(r"^/api/attempts/(\d+)/files$", route)
            if match:
                attempt = one("SELECT * FROM attempts WHERE id=?", (int(match.group(1)),))
                if not attempt:
                    raise ValueError("Attempt not found")
                listing = attempt_files(attempt)
                wanted = parse_qs(urlparse(self.path).query).get('path', [None])[0]
                if wanted is None:
                    self.send_json(listing)
                    return
                chosen = next((item for item in listing['files'] if item['path'] == wanted and item['exists']), None)
                if not chosen:
                    raise ValueError("That file is not part of this attempt's changes.")
                path = worktrees.resolve_within(Path(attempt['worktree_path']), wanted)
                self.send_response(200)
                self.send_header('Content-Type', chosen['media_type'])
                self.send_header('Content-Length', str(path.stat().st_size))
                self.send_header('Content-Disposition', 'inline; filename="%s"' % attachment_store.safe_name(wanted))
                self.end_headers()
                with path.open('rb') as handle:
                    shutil.copyfileobj(handle, self.wfile)
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
            # An unmatched /api path is a bug or a stale server, not a file
            # request.  Answering in JSON keeps the client's error readable
            # instead of handing it the index page with an HTML content type.
            if route.startswith("/api/"):
                self.send_json({"error": "Unknown API route: %s. If you just updated Aludra, restart the Python server." % route}, 404)
                return
            # The React product has one canonical surface at /.  Preserve old
            # bookmarks without letting /ui/index.html serve the retired UI.
            self.path = "/index.html" if route == '/' or route == '/legacy' or route == '/ui' or route.startswith('/ui/') else route
            return super().do_GET()
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        try:
            if not self.is_dashboard_origin():
                self.send_json({"error": "Requests must come from this dashboard"}, 403)
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise ValueError("Expected a JSON request")
            payload = self.body()
            if route == "/api/memories/claude/global":
                self.send_json(memory_store.claude_set_global(
                    bool(payload.get("enabled")), payload.get("directory")))
                return
            if route == "/api/memories/codex/enable":
                self.send_json(memory_store.codex_enable())
                return
            match = re.fullmatch(r"/api/memories/(\w+)/notes", route)
            if match:
                memory_harness(match.group(1))
                self.send_json(memory_store.codex_add_note(str(payload.get("text", ""))))
                return
            match = re.fullmatch(r"/api/memories/(\w+)", route)
            if match:
                self.send_json(memory_store.save(
                    memory_harness(match.group(1)),
                    memory_scope(str(payload.get("scope", "global"))), payload))
                return
            match = re.fullmatch(r'/api/projects/(\d+)/board-views', route)
            if match:
                project_id = int(match[1])
                project_or_404(project_id)
                name = str(payload.get('name', '')).strip()[:80]
                config = payload.get('config', {})
                if not name or not isinstance(config, dict):
                    raise ValueError('A view name and configuration are required')
                stages = config.get('stages', ['planned', 'running', 'review', 'done'])
                if not isinstance(stages, list) or not stages or any(s not in ('planned', 'running', 'review', 'done') for s in stages):
                    raise ValueError('Choose at least one valid workflow stage')
                view_id = payload.get('id')
                if view_id:
                    if not one('SELECT id FROM board_views WHERE id=? AND project_id=?', (view_id, project_id)):
                        raise ValueError('View not found')
                    execute('UPDATE board_views SET name=?,config_json=? WHERE id=?', (name, json.dumps(config), view_id))
                else:
                    view_id = execute('INSERT INTO board_views(project_id,name,config_json,created_at) VALUES(?,?,?,?)', (project_id, name, json.dumps(config), now()))
                self.send_json({'id': view_id}); return
            match = re.fullmatch(r'/api/projects/(\d+)/board', route)
            if match:
                project_id = int(match[1])
                project_or_404(project_id)
                items = payload.get('tasks')
                expected = {t['id'] for t in rows('SELECT id FROM tasks WHERE project_id=?', (project_id,))}
                if not isinstance(items, list) or any(not isinstance(t, dict) for t in items) or len(items) != len(expected) or {t.get('id') for t in items} != expected:
                    raise ValueError('Board must contain every project task exactly once; reload and retry')
                if any(t.get('workflow_stage') not in ('planned', 'running', 'review', 'done') for t in items):
                    raise ValueError('Unknown workflow stage')
                with DB_LOCK, db() as conn:
                    for position, item in enumerate(items):
                        conn.execute('UPDATE tasks SET workflow_stage=?,board_position=? WHERE id=? AND project_id=?', (item['workflow_stage'], position, item['id'], project_id))
                self.send_json({'ok': True}); return
            match = re.match(r'^/api/coder-servers/(\d+)/runner$', route)
            if match:
                server = coder_server_or_404(int(match.group(1)))
                token, owner, _ = coder_runner_context(server)
                name = str(payload.get('workspace_name') or '')
                if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9-]{0,63}', name):
                    raise ValueError('Choose an existing private Coder workspace.')
                workspace = coder_json(server['base_url'], '/api/v2/users/me/workspace/' + quote(name, safe=''), token)
                runner = refresh_runner_capacity(save_coder_runner(server, owner, workspace),
                                                 dict(os.environ, CODER_URL=server['base_url'], CODER_SESSION_TOKEN=token,
                                                      CODER_ORGANIZATION=server['organization']), force=True)
                self.send_json({'runner': runner})
                return
            match = re.match(r'^/api/coder-servers/(\d+)/runner/migrate$', route)
            if match:
                server = coder_server_or_404(int(match.group(1)))
                project_id = int(payload.get('project_id') or 0)
                profile = project_coder_profile(project_id)
                if not profile or profile.get('coder_server_id') != server['id']:
                    raise ValueError('Choose this Coder server for the project before migrating its runner.')
                self.send_json(migrate_coder_runner(server, profile))
                return
            match = re.match(r'^/api/coder-servers/(\d+)/model-auth/codex/connect$', route)
            if match:
                server = coder_server_or_404(int(match.group(1)))
                self.send_json(start_remote_codex_login(server, coder_profile_for_server(server['id'])))
                return
            match = re.match(r'^/api/coder-servers/(\d+)/model-auth/claude/connect$', route)
            if match:
                server = coder_server_or_404(int(match.group(1)))
                self.send_json(start_remote_claude_login(server, coder_profile_for_server(server['id'])))
                return
            match = re.match(r'^/api/coder-servers/(\d+)/model-auth/claude/input$', route)
            if match:
                server = coder_server_or_404(int(match.group(1)))
                profile = coder_profile_for_server(server['id'])
                runner, _ = ensure_coder_runner(server, profile)
                self.send_json(send_remote_claude_login_input(server['id'], runner['workspace_id'], str(payload.get('input') or '')))
                return
            match = re.match(r'^/api/coder-servers/(\d+)/external-auth/([a-z0-9_-]+)/connect$', route)
            if match:
                self.send_json(start_device_flow(coder_server_or_404(int(match.group(1))), match.group(2)))
                return
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
                scope, scope_id = harness_order_scope(str(payload.get('scope', 'global')))
                if scope == 'global':
                    with DB_LOCK, db() as conn:
                        for position, key in enumerate(order):
                            conn.execute('UPDATE harnesses SET chain_position=?,updated_at=? WHERE key=?', (position, now(), key))
                else:
                    set_harness_order(scope, scope_id, order)
                self.send_json({'ok': True, 'scope': scope})
                return
            match = re.match(r'^/api/(projects|tasks)/(\d+)/harness-models$', route)
            if match:
                scope = 'project' if match.group(1) == 'projects' else 'task'
                owner_id = int(match.group(2))
                key = str(payload.get('harness') or '')
                if key not in ADAPTERS:
                    raise ValueError('Unknown harness')
                model = requested_model(payload.get('model'))
                if scope == 'project':
                    project_or_404(owner_id)
                    save_scoped_model(scope, owner_id, key, model)
                else:
                    task = one('SELECT * FROM tasks WHERE id=?', (owner_id,))
                    if not task:
                        raise ValueError('Task not found')
                    save_scoped_model(scope, owner_id, key, model)
                    if task.get('preferred_harness') == key:
                        # Keep the legacy pin and the scoped choice from disagreeing.
                        execute('UPDATE tasks SET preferred_model=? WHERE id=?', (model, owner_id))
                self.send_json({'harness_models': model_snapshot(
                    one('SELECT * FROM tasks WHERE id=?', (owner_id,)) if scope == 'task' else None,
                    project_id=owner_id if scope == 'project' else None)})
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
                auth_provider_id = str(payload.get('auth_provider_id') or 'github').strip().lower()
                if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', auth_provider_id):
                    raise ValueError('Enter a valid Coder connection ID, such as github or primary-gitlab.')
                template_name = str(payload.get('template_name') or '').strip().lower() or coder_template_name(server['name'], project['name'])
                if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', template_name):
                    raise ValueError('Enter a valid Coder template name.')
                execute("""INSERT INTO project_coder_profiles(project_id,coder_server_id,setup_profile,repo_url,base_ref,auth_provider_id,template_name,enabled,default_target,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,1,?,?,?) ON CONFLICT(project_id) DO UPDATE SET coder_server_id=excluded.coder_server_id,
                    setup_profile=excluded.setup_profile,repo_url=excluded.repo_url,base_ref=excluded.base_ref,auth_provider_id=excluded.auth_provider_id,template_name=excluded.template_name,
                    enabled=1,default_target=excluded.default_target,updated_at=excluded.updated_at""",
                    (project_id, server['id'], setup_profile, repo_url, base_ref, auth_provider_id, template_name, default_target, now(), now()))
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
                memory_mode = payload.get('memory_mode', 'inherit')
                if memory_mode not in ('inherit', 'read_write', 'read_only', 'off'):
                    raise ValueError('Unknown task memory mode')
                execution_target = payload.get('execution_target', 'project')
                if execution_target not in ('project', 'local', 'coder'):
                    raise ValueError('Execution target must be project default, local, or Coder.')
                # Decode before creating anything: a rejected file should not
                # leave a half-described task on the board.
                incoming = payload.get('attachments') or []
                if not isinstance(incoming, list) or len(incoming) > MAX_NEW_TASK_ATTACHMENTS:
                    raise ValueError('Attach at most %d files when creating a task.' % MAX_NEW_TASK_ATTACHMENTS)
                pending = [(str(item.get('filename') or ''), attachment_store.decode(item.get('data')))
                           for item in incoming]
                for filename, _ in pending:
                    attachment_store.classify(filename)
                with DB_LOCK:
                    order = one("SELECT COALESCE(MAX(task_order),-1)+1 AS n FROM tasks WHERE project_id=?", (project_id,))["n"]
                    task_id = execute("INSERT INTO tasks(project_id,text,task_order,status,created_at) VALUES(?,?,?,'pending',?)",
                        (project_id, task_text.splitlines()[0][:120], order, now()))
                    execute("UPDATE tasks SET board_position=? WHERE id=?", (order, task_id))
                    execute('UPDATE tasks SET tool_permissions=?,memory_mode=?,execution_target=? WHERE id=?',
                            (permissions, memory_mode, execution_target, task_id))
                    session_id = execute("INSERT INTO task_sessions(task_id,session_number,status,opened_at) VALUES(?,1,'active',?)", (task_id, now()))
                    message_id = execute("INSERT INTO task_messages(task_id,session_id,role,content,created_at) VALUES(?,?,'user',?,?)", (task_id, session_id, task_text, now()))
                for filename, data in pending:
                    store_task_attachment(task_id, session_id, filename, data, message_id)
                # Attachments are stored before the run is requested, so a task
                # started on creation still reaches its harness with them.
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
            match = re.match(r"^/api/tasks/(\d+)/attachments$", route)
            if match:
                task_id = int(match.group(1))
                task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
                if not task:
                    raise ValueError("Task not found")
                attachment_id = store_task_attachment(
                    task_id, active_session(task_id)['id'], str(payload.get('filename') or ''),
                    attachment_store.decode(payload.get('data')))
                self.send_json({'attachment': one("SELECT * FROM task_attachments WHERE id=?", (attachment_id,))}, 201)
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
                session = active_session(task_id)
                message_id = execute("INSERT INTO task_messages(task_id,session_id,role,content,created_at) VALUES(?,?,'user',?,?)", (task_id, session['id'], content, now()))
                # Attachments are uploaded before the message they belong to;
                # sending the message is what ties them to a point in the thread.
                execute("UPDATE task_attachments SET message_id=?,session_id=? WHERE task_id=? AND message_id IS NULL",
                        (message_id, session['id'], task_id))
                execute("UPDATE tasks SET status='pending' WHERE id=?", (task_id,))
                submission = request_run(task['project_id'], task_id) if payload.get('start') else None
                self.send_json({"ok": True, 'submission': submission}, 201)
                return
            match = re.match(r"^/api/tasks/(\d+)/sessions/rotate$", route)
            if match:
                task_id = int(match.group(1))
                task = one('SELECT * FROM tasks WHERE id=?', (task_id,))
                if not task:
                    raise ValueError('Task not found')
                if current_run(task['project_id']):
                    raise ValueError('Wait for the active run to finish before starting a fresh conversation session.')
                project = project_or_404(task['project_id'])
                backend, _ = effective_execution_backend(project, task)
                if backend == 'coder':
                    session = rotate_session(task, 'manual session rotation', None, saved_remote_workspace_snapshot(project, task))
                else:
                    prior = one('SELECT * FROM attempts WHERE task_id=? ORDER BY id DESC LIMIT 1', (task_id,))
                    root = Path(prior['worktree_path']) if prior and prior.get('worktree_path') else Path(project['repo_path'])
                    session = rotate_session(task, 'manual session rotation', root)
                self.send_json({'session': sessions_for_task(task_id)[-1], 'next_session_id': session['id']})
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
                    session = active_session(run['task_id'])
                    execute("INSERT INTO task_messages(task_id,session_id,role,content,created_at) VALUES(?,?,'system',?,?)",(run['task_id'],session['id'],'You requested a retry with Claude automatic permission review for this run only.',now()))
                threading.Thread(target=run_attempt,args=(run['id'],run['project_id'],run['task_id'],attempt['id'],True),daemon=True).start()
                self.send_json({'ok':True});return
            match = re.match(r"^/api/runs/([\w-]+)/(?:approve-commit|complete)$", route)
            if match:
                run = one("SELECT * FROM runs WHERE id=?", (match.group(1),))
                if not run or run["status"] not in ('awaiting_commit', 'awaiting_review'): raise ValueError("Run is not awaiting review")
                claim_run(run['id'], run['status'], 'committing', 'Finishing reviewed run.')
                threading.Thread(target=finish_commit, args=(run["id"], run["project_id"], run["task_id"], run["attempt_id"]), daemon=True).start()
                self.send_json({"ok": True}); return
            match = re.match(r"^/api/runs/([\w-]+)/retry-delivery$", route)
            if match:
                retry_remote_delivery(match.group(1))
                self.send_json({'ok': True}); return
            match = re.match(r"^/api/runs/([\w-]+)/resume$", route)
            if match:
                run = one("SELECT * FROM runs WHERE id=?", (match.group(1),))
                if not run or run["status"] not in ('awaiting_resume', 'paused_cooldown'): raise ValueError("Run is not awaiting a resume decision")
                claim_run(run['id'], run['status'], 'queued', 'Resuming this task on the next available harness.')
                threading.Thread(target=run_attempt, args=(run["id"], run["project_id"], run["task_id"], run["attempt_id"]), daemon=True).start()
                self.send_json({"ok": True}); return
            match = re.match(r"^/api/runs/([\w-]+)/resume-after-auth$", route)
            if match:
                run = one("SELECT * FROM runs WHERE id=?", (match.group(1),))
                if not run or run['status'] != 'awaiting_external_auth':
                    raise ValueError('Run is not awaiting Coder account authorization')
                profile = project_coder_profile(run['project_id'])
                if not profile or not profile.get('coder_server_id'):
                    raise ValueError('This project no longer has a Coder server configured')
                claim_run(run['id'], run['status'], 'queued', 'Retrying repository checkout in the persistent Coder runner.')
                threading.Thread(target=run_attempt, args=(run['id'], run['project_id'], run['task_id']), daemon=True).start()
                self.send_json({'ok': True}); return
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
                fields = {name: payload[name] for name in ("text", "mode_override", "preferred_harness", "preferred_model", "force_gate", "degradable", "tool_permissions", "memory_mode", "execution_target", "session_budget_chars") if name in payload}
                if 'tool_permissions' in fields and fields['tool_permissions'] not in ('inherit','standard','auto','ask'):
                    raise ValueError('Unknown task permission mode')
                if 'memory_mode' in fields and fields['memory_mode'] not in ('inherit', 'read_write', 'read_only', 'off'):
                    raise ValueError('Unknown task memory mode')
                if not fields: raise ValueError("No task updates supplied")
                if "text" in fields:
                    fields["text"] = str(fields["text"]).strip()[:120]
                    if not fields["text"]:
                        raise ValueError("Task name cannot be empty")
                if fields.get("mode_override") not in (None, "", "supervised", "unattended"):
                    raise ValueError("Mode must be supervised or unattended")
                if fields.get('execution_target') not in (None, 'project', 'local', 'coder'):
                    raise ValueError('Execution target must be project default, local, or Coder.')
                if 'session_budget_chars' in fields:
                    try:
                        fields['session_budget_chars'] = int(fields['session_budget_chars'])
                    except (TypeError, ValueError):
                        raise ValueError('Session context budget must be a whole number of characters.')
                    if not 0 <= fields['session_budget_chars'] <= 500000:
                        raise ValueError('Session context budget must be between 0 and 500,000 characters; use 0 to disable automatic rollover.')
                assignments = ", ".join(f"{name}=?" for name in fields)
                execute(f"UPDATE tasks SET {assignments} WHERE id=?", tuple(fields.values()) + (task_id,))
                if 'preferred_model' in fields or 'preferred_harness' in fields:
                    saved = one("SELECT preferred_harness,preferred_model FROM tasks WHERE id=?", (task_id,))
                    if saved['preferred_harness'] in ADAPTERS:
                        save_scoped_model('task', task_id, saved['preferred_harness'],
                                          requested_model(saved['preferred_model']))
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
            if not self.is_dashboard_origin():
                self.send_json({"error": "Requests must come from this dashboard"}, 403)
                return
            if route == "/api/harness-order":
                scope, scope_id = harness_order_scope(
                    parse_qs(urlparse(self.path).query).get("scope", ["global"])[0])
                if scope == "global":
                    raise ValueError("The global order is the root; it has nothing to inherit")
                clear_harness_order(scope, scope_id)
                self.send_json({"ok": True})
                return
            match = re.fullmatch(r"/api/memories/(\w+)/([a-z0-9-]+)", route)
            if match:
                scope = parse_qs(urlparse(self.path).query).get("scope", ["global"])[0]
                self.send_json(memory_store.delete(
                    memory_harness(match.group(1)), memory_scope(scope), match.group(2)))
                return
            match = re.match(r"^/api/tasks/(\d+)/attachments/(\d+)$", route)
            if match:
                record = task_attachment_or_404(int(match.group(1)), int(match.group(2)))
                attachment_store.remove(record)
                execute("DELETE FROM task_attachments WHERE id=?", (record['id'],))
                self.send_json({"ok": True})
                return
            match = re.match(r"^/api/tasks/(\d+)$", route)
            if not match:
                raise ValueError("Unknown API route")
            task_id = int(match.group(1))
            task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
            if not task:
                raise ValueError("Task not found")
            active = current_run(task["project_id"])
            worker_statuses = ('running', 'verifying', 'queued', 'rotating', 'committing')
            if active and active["task_id"] == task_id and active['status'] in worker_statuses:
                raise ValueError("Finish or close this task's active run before deleting it")
            attachment_store.remove_task(DATA_ROOT, task_id)
            with DB_LOCK, db() as conn:
                # A remote task checkout is deliberately preserved on the runner,
                # but its local record must not keep a discarded task alive.
                conn.execute("DELETE FROM coder_task_worktrees WHERE task_id=?", (task_id,))
                conn.execute("DELETE FROM task_attachments WHERE task_id=?", (task_id,))
                conn.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
                conn.execute("DELETE FROM attempts WHERE task_id=?", (task_id,))
                conn.execute("DELETE FROM runs WHERE task_id=?", (task_id,))
                conn.execute("DELETE FROM harness_orders WHERE scope='task' AND scope_id=?", (task_id,))
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
    recover_capacity_waiters()
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
