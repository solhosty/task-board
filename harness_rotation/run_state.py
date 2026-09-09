"""Persistence-backed run admission and execution-lease state."""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid


REMOTE_SLOT_STATUSES = (
    "awaiting_dispatch", "queued", "running", "verifying", "rotating", "committing"
)

Rows = Callable[[str, Tuple[Any, ...]], List[Dict[str, Any]]]
One = Callable[[str, Tuple[Any, ...]], Optional[Dict[str, Any]]]
Execute = Callable[[str, Tuple[Any, ...]], int]


class RunStateStore:
    """Own database queries and mutations for active runs and their leases."""

    def __init__(
        self,
        rows: Rows,
        one: One,
        execute: Execute,
        clock: Callable[[], str],
        project_profile: Callable[[int], Optional[Dict[str, Any]]],
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self.rows = rows
        self.one = one
        self.execute = execute
        self.clock = clock
        self.project_profile = project_profile
        self.uuid_factory = uuid_factory

    def current(self, project_id: int) -> Optional[Dict[str, Any]]:
        return (self.active(project_id) or [None])[0]

    def active(self, project_id: int) -> List[Dict[str, Any]]:
        return self.rows(
            "SELECT * FROM runs WHERE project_id=? "
            "AND status NOT IN ('complete','stopped','discarded','blocked') "
            "AND rowid=(SELECT MAX(newer.rowid) FROM runs newer WHERE newer.task_id=runs.task_id) "
            "ORDER BY rowid DESC",
            (project_id,),
        )

    def remote_capacity(self, profile: Optional[Dict[str, Any]]) -> int:
        """Sum slots across the configured isolated runner pool."""
        if not profile or not profile.get("coder_server_id"):
            return 1
        bindings = self.rows(
            """SELECT r.detected_max_tasks FROM runner_account_bindings b
               JOIN coder_runners r ON r.id=b.runner_id
               WHERE b.enabled=1 AND r.coder_server_id=?""",
            (profile["coder_server_id"],),
        )
        if bindings:
            return sum(max(1, int(item["detected_max_tasks"] or 1)) for item in bindings)
        runner = self.one(
            "SELECT detected_max_tasks FROM coder_runners WHERE coder_server_id=? ORDER BY rowid DESC LIMIT 1",
            (profile["coder_server_id"],),
        )
        return max(1, int(runner["detected_max_tasks"] or 1)) if runner else 1

    def runner_slots(self, coder_server_id: int) -> List[Dict[str, Any]]:
        """Runs admitted to a runner, whether or not SSH has started yet."""
        placeholders = ",".join("?" for _ in REMOTE_SLOT_STATUSES)
        return self.rows(
            """SELECT r.* FROM runs r JOIN execution_leases l ON l.run_id=r.id
            LEFT JOIN project_coder_profiles p ON p.project_id=r.project_id
            WHERE l.backend='coder' AND r.status IN (%s) AND p.coder_server_id=?
            ORDER BY r.created_at, r.rowid""" % placeholders,
            (*REMOTE_SLOT_STATUSES, coder_server_id),
        )

    def lease(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self.one("SELECT * FROM execution_leases WHERE run_id=?", (run_id,))

    def backend(self, run_id: str) -> Optional[str]:
        lease = self.lease(run_id)
        return lease.get("backend") if lease else None

    def connection_action(self, run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Return structured UI routing without parsing a human-readable URL."""
        if not run or run["status"] != "awaiting_external_auth":
            return None
        profile = self.project_profile(run["project_id"])
        if not profile or not profile.get("coder_server_id"):
            return None
        return {
            "server_id": profile["coder_server_id"],
            "provider": profile.get("auth_provider_id") or "github",
        }

    def create_lease(
        self, run_id: str, task_id: int, backend: str = "local"
    ) -> Dict[str, Any]:
        if backend not in ("local", "coder"):
            raise ValueError("Unknown execution backend")
        lease = self.lease(run_id)
        if lease:
            return lease
        lease_id = str(self.uuid_factory())
        timestamp = self.clock()
        self.execute(
            """INSERT INTO execution_leases(
                id,run_id,task_id,backend,state,created_at,updated_at
            ) VALUES(?,?,?,?,'planned',?,?)""",
            (lease_id, run_id, task_id, backend, timestamp, timestamp),
        )
        lease = self.lease(run_id)
        if not lease:
            raise RuntimeError("Execution lease was not created for run %s" % run_id)
        return lease

    def bind_local(self, run_id: str, worktree: Path, base_sha: Optional[str]) -> None:
        lease = self.lease(run_id)
        if not lease:
            raise RuntimeError("Run %s has no execution lease" % run_id)
        if lease["backend"] != "local":
            return
        self.execute(
            """UPDATE execution_leases SET state='ready', worktree_path=?,
            base_sha=?, updated_at=? WHERE run_id=?""",
            (str(worktree), base_sha, self.clock(), run_id),
        )
