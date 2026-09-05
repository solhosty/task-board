"""SQLite connection, schema, and migration ownership.

This module deliberately knows nothing about HTTP handlers or execution services.
The top-level app passes its configurable database path and lock so existing local
and test entry points retain their behavior while persistence has one clear home.
"""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple


BASE_SCHEMA = """
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
  session_budget_chars INTEGER NOT NULL DEFAULT 24000,
  last_attempt_id INTEGER, created_at TEXT NOT NULL,
  UNIQUE(project_id, task_order)
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id), harness_key TEXT,
  model TEXT, selection TEXT, status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
  worktree_path TEXT, branch_name TEXT, base_sha TEXT, commit_sha TEXT, diff_stat TEXT,
  diff_output TEXT, verify_output TEXT, error TEXT, log_path TEXT, run_id TEXT
);
CREATE TABLE IF NOT EXISTS task_sessions (
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  session_number INTEGER NOT NULL, harness_key TEXT, model TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','sealed','needs_review')),
  opened_at TEXT NOT NULL, sealed_at TEXT, close_reason TEXT,
  handoff_json TEXT, integrity_status TEXT NOT NULL DEFAULT 'not_checked'
  CHECK(integrity_status IN ('not_checked','passed','mismatch','unavailable')),
  integrity_detail TEXT, baseline_json TEXT,
  UNIQUE(task_id, session_number)
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
  repo_url TEXT, base_ref TEXT NOT NULL DEFAULT 'main', auth_provider_id TEXT NOT NULL DEFAULT 'github', template_name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0, default_target TEXT NOT NULL DEFAULT 'local'
  CHECK(default_target IN ('local','coder')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coder_runners (
  id INTEGER PRIMARY KEY, coder_server_id INTEGER NOT NULL REFERENCES coder_servers(id),
  deployment_url TEXT NOT NULL, organization TEXT NOT NULL, owner_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL, workspace_name TEXT NOT NULL, workspace_url TEXT NOT NULL,
  template_name TEXT NOT NULL, max_tasks INTEGER NOT NULL DEFAULT 1 CHECK(max_tasks BETWEEN 1 AND 8),
  detected_cpu_count INTEGER, detected_memory_bytes INTEGER, detected_max_tasks INTEGER,
  capacity_checked_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(coder_server_id, deployment_url, organization, owner_id)
);
CREATE TABLE IF NOT EXISTS coder_task_worktrees (
  task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
  runner_id INTEGER NOT NULL REFERENCES coder_runners(id),
  task_key TEXT NOT NULL UNIQUE, repo_url TEXT NOT NULL,
  worktree_path TEXT, base_sha TEXT, branch_name TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY, applied_at TEXT NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=20, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def fetch_all(path: Path, lock: RLock, sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    with lock, closing(connect(path)) as connection:
        return [dict(item) for item in connection.execute(sql, params).fetchall()]


def fetch_one(path: Path, lock: RLock, sql: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
    result = fetch_all(path, lock, sql, params)
    return result[0] if result else None


def execute(path: Path, lock: RLock, sql: str, params: Tuple[Any, ...] = ()) -> int:
    with lock, connect(path) as connection:
        cursor = connection.execute(sql, params)
        return cursor.lastrowid


def _columns(connection: sqlite3.Connection, table: str) -> set:
    return {item[1] for item in connection.execute("PRAGMA table_info(" + table + ")")}


def _add_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _columns(connection, table):
        connection.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition))


@dataclass(frozen=True)
class Migration:
    version: str
    apply: Callable[[sqlite3.Connection], None]


def _runner_link(connection: sqlite3.Connection) -> None:
    _add_column(connection, "execution_leases", "runner_id", "INTEGER REFERENCES coder_runners(id)")


def _runner_capacity(connection: sqlite3.Connection) -> None:
    for column, definition in (
        ("detected_cpu_count", "INTEGER"), ("detected_memory_bytes", "INTEGER"),
        ("detected_max_tasks", "INTEGER"), ("capacity_checked_at", "TEXT"),
    ):
        _add_column(connection, "coder_runners", column, definition)


def _attempt_diff(connection: sqlite3.Connection) -> None:
    _add_column(connection, "attempts", "diff_output", "TEXT")


def _harness_catalog(connection: sqlite3.Connection) -> None:
    _add_column(connection, "harnesses", "model_catalog", "TEXT")
    _add_column(connection, "harnesses", "model_source", "TEXT")


def _harness_pool(connection: sqlite3.Connection) -> None:
    if "pool_migrated" not in _columns(connection, "harnesses"):
        connection.execute("ALTER TABLE harnesses ADD COLUMN pool_migrated INTEGER DEFAULT 1")
        connection.execute("UPDATE harnesses SET enabled=installed")


def _project_execution(connection: sqlite3.Connection) -> None:
    _add_column(connection, "projects", "execution_mode", "TEXT NOT NULL DEFAULT 'local'")
    _add_column(connection, "projects", "auto_failover", "INTEGER NOT NULL DEFAULT 1")


def _message_attempt(connection: sqlite3.Connection) -> None:
    _add_column(connection, "task_messages", "attempt_id", "INTEGER REFERENCES attempts(id)")


def _permission_policy(connection: sqlite3.Connection) -> None:
    _add_column(connection, "harnesses", "tool_permissions", "TEXT NOT NULL DEFAULT 'standard'")
    _add_column(connection, "runs", "permission_override", "TEXT")
    _add_column(connection, "runs", "permissions_json", "TEXT")
    _add_column(connection, "tasks", "tool_permissions", "TEXT NOT NULL DEFAULT 'inherit'")
    _add_column(connection, "attempts", "tool_permissions", "TEXT")


def _execution_targets(connection: sqlite3.Connection) -> None:
    _add_column(connection, "tasks", "execution_target", "TEXT NOT NULL DEFAULT 'project'")
    _add_column(connection, "project_coder_profiles", "default_target", "TEXT NOT NULL DEFAULT 'local'")
    _add_column(connection, "project_coder_profiles", "auth_provider_id", "TEXT NOT NULL DEFAULT 'github'")


def _sessions(connection: sqlite3.Connection) -> None:
    _add_column(connection, "tasks", "session_budget_chars", "INTEGER NOT NULL DEFAULT 24000")
    _add_column(connection, "attempts", "session_id", "INTEGER REFERENCES task_sessions(id)")
    _add_column(connection, "task_messages", "session_id", "INTEGER REFERENCES task_sessions(id)")


def _aludra_board(connection: sqlite3.Connection) -> None:
    _add_column(connection, "tasks", "workflow_stage", "TEXT")
    _add_column(connection, "tasks", "board_position", "REAL NOT NULL DEFAULT 0")
    connection.execute("UPDATE tasks SET board_position=task_order")
    connection.execute("""CREATE TABLE IF NOT EXISTS board_views (
        id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name TEXT NOT NULL, config_json TEXT NOT NULL, created_at TEXT NOT NULL)""")


def _scoped_harness_order(connection: sqlite3.Connection) -> None:
    """A project or task may carry its own complete harness ordering.  Rows
    exist only where a scope overrides what it inherits, so an absent scope
    falls through to its parent rather than storing a copy of it."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS harness_orders (
          id INTEGER PRIMARY KEY,
          scope TEXT NOT NULL CHECK(scope IN ('project','task')),
          scope_id INTEGER NOT NULL,
          harness_key TEXT NOT NULL,
          position INTEGER NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(scope, scope_id, harness_key)
        )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_harness_orders_scope ON harness_orders(scope, scope_id, position)")


def _task_memory_mode(connection: sqlite3.Connection) -> None:
    _add_column(connection, "tasks", "memory_mode", "TEXT NOT NULL DEFAULT 'inherit'")


def _scoped_harness_models(connection: sqlite3.Connection) -> None:
    """Per-harness model choices a project, and then a task, may narrow.

    The harnesses table keeps the global choice, so a missing row here means the
    wider scope still decides and no model is copied forward on save.
    """
    connection.execute("""CREATE TABLE IF NOT EXISTS project_harness_models (
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        harness_key TEXT NOT NULL, model TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(project_id, harness_key))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS task_harness_models (
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        harness_key TEXT NOT NULL, model TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(task_id, harness_key))""")


def _task_attachments(connection: sqlite3.Connection) -> None:
    """Files and images a user adds to a task conversation.

    The bytes live on disk beside the logs rather than in SQLite, so a large
    image never inflates the state database or a task query; the row records
    where they are and which message introduced them.
    """
    connection.execute("""CREATE TABLE IF NOT EXISTS task_attachments (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        session_id INTEGER REFERENCES task_sessions(id),
        message_id INTEGER REFERENCES task_messages(id),
        filename TEXT NOT NULL, media_type TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('image','file')),
        byte_size INTEGER NOT NULL, sha256 TEXT NOT NULL,
        stored_path TEXT NOT NULL, created_at TEXT NOT NULL)""")


MIGRATIONS = (
    Migration("001_execution_lease_runner", _runner_link),
    Migration("002_runner_capacity", _runner_capacity),
    Migration("003_attempt_diff", _attempt_diff),
    Migration("004_harness_catalog", _harness_catalog),
    Migration("005_harness_pool", _harness_pool),
    Migration("006_project_execution", _project_execution),
    Migration("007_message_attempt", _message_attempt),
    Migration("008_permission_policy", _permission_policy),
    Migration("009_execution_targets", _execution_targets),
    Migration("010_task_sessions", _sessions),
    Migration("011_aludra_board", _aludra_board),
    Migration("012_task_memory_mode", _task_memory_mode),
    Migration("013_scoped_harness_models", _scoped_harness_models),
    Migration("014_task_attachments", _task_attachments),
    Migration("015_scoped_harness_order", _scoped_harness_order),
)


def _migrate_legacy_sessions(connection: sqlite3.Connection, timestamp: Callable[[], str]) -> None:
    """Give pre-session tasks one durable initial session without losing history."""
    task_ids = [row["id"] for row in connection.execute(
        "SELECT id FROM tasks WHERE NOT EXISTS "
        "(SELECT 1 FROM task_sessions WHERE task_sessions.task_id=tasks.id)"
    )]
    for task_id in task_ids:
        session_id = connection.execute(
            "INSERT INTO task_sessions(task_id,session_number,status,opened_at) VALUES(?,1,'active',?)",
            (task_id, timestamp()),
        ).lastrowid
        connection.execute(
            "UPDATE task_messages SET session_id=? WHERE task_id=? AND session_id IS NULL",
            (session_id, task_id),
        )
        connection.execute(
            "UPDATE attempts SET session_id=? WHERE task_id=? AND session_id IS NULL",
            (session_id, task_id),
        )


def initialize(
    data_root: Path,
    path: Path,
    adapters: Mapping[str, Mapping[str, Any]],
    timestamp: Callable[[], str],
) -> None:
    """Create the current schema and safely upgrade databases from prior releases."""
    data_root.mkdir(exist_ok=True)
    with connect(path) as connection:
        connection.executescript(BASE_SCHEMA)
        for position, (key, adapter) in enumerate(adapters.items()):
            connection.execute(
                """INSERT INTO harnesses(key,label,binary,chain_position,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(key) DO NOTHING""",
                (key, adapter["label"], adapter["binary"], position, timestamp()),
            )
        for migration in MIGRATIONS:
            # Migration operations are intentionally idempotent. Running the guard
            # each startup also repairs old development databases that were edited
            # manually before migration history existed.
            migration.apply(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?) "
                "ON CONFLICT(version) DO NOTHING",
                (migration.version, timestamp()),
            )
        _migrate_legacy_sessions(connection, timestamp)
