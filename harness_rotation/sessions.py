"""Durable task-session lifecycle and workspace handoff integrity."""

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


Rows = Callable[[str, Tuple[Any, ...]], List[Dict[str, Any]]]
One = Callable[[str, Tuple[Any, ...]], Optional[Dict[str, Any]]]
Execute = Callable[[str, Tuple[Any, ...]], int]


class SessionService:
    """Own session rotation without depending on the HTTP or process layers."""

    def __init__(
        self,
        rows: Rows,
        one: One,
        execute: Execute,
        clock: Callable[[], str],
        git: Callable[..., Any],
        read_events: Callable[[Optional[str]], List[Dict[str, Any]]],
    ) -> None:
        self.rows = rows
        self.one = one
        self.execute = execute
        self.clock = clock
        self.git = git
        self.read_events = read_events

    def for_task(self, task_id: int) -> List[Dict[str, Any]]:
        sessions = self.rows(
            "SELECT * FROM task_sessions WHERE task_id=? ORDER BY session_number", (task_id,)
        )
        for session in sessions:
            session["handoff"] = json.loads(session["handoff_json"]) if session.get("handoff_json") else None
            session["baseline"] = json.loads(session["baseline_json"]) if session.get("baseline_json") else None
            session.pop("handoff_json", None)
            session.pop("baseline_json", None)
        return sessions

    def active(self, task_id: int) -> Dict[str, Any]:
        session = self.one(
            "SELECT * FROM task_sessions WHERE task_id=? AND status='active' "
            "ORDER BY session_number DESC LIMIT 1",
            (task_id,),
        )
        if session:
            return session
        latest = self.one(
            "SELECT COALESCE(MAX(session_number),0) AS n FROM task_sessions WHERE task_id=?",
            (task_id,),
        )
        session_id = self.execute(
            "INSERT INTO task_sessions(task_id,session_number,status,opened_at) "
            "VALUES(?,?,'active',?)",
            (task_id, latest["n"] + 1, self.clock()),
        )
        return self.one("SELECT * FROM task_sessions WHERE id=?", (session_id,))

    def message_chars(self, session_id: int) -> int:
        value = self.one(
            "SELECT COALESCE(SUM(LENGTH(content)),0) AS n FROM task_messages WHERE session_id=?",
            (session_id,),
        )
        return int(value["n"])

    def workspace_snapshot(self, root: Path) -> Dict[str, Any]:
        if not root.is_dir():
            return {"available": False, "reason": "The preserved working folder is unavailable."}
        git_dir = self.git(["rev-parse", "--git-dir"], root, check=False)
        if git_dir.returncode:
            return {"available": False, "reason": "This working folder is not a Git repository."}
        baseline = self.git(["rev-parse", "--verify", "HEAD"], root, check=False).stdout.strip() or None
        status = self.git(["status", "--porcelain=v1"], root, check=False).stdout
        diff = self.git(["diff", "HEAD", "--binary", "--"], root, check=False).stdout
        untracked = self.git(["ls-files", "--others", "--exclude-standard"], root, check=False).stdout
        payload = (status + "\n" + diff + "\nUNTRACKED\n" + untracked).encode()
        return {
            "available": True,
            "base_sha": baseline,
            "status": status[-12000:],
            "diff_hash": hashlib.sha256(payload).hexdigest(),
            "changed_files": [line[3:] for line in status.splitlines() if len(line) > 3][:200],
        }

    def attempt_summary(self, session_id: int) -> List[Dict[str, Any]]:
        result = []
        for attempt in self.rows(
            "SELECT * FROM attempts WHERE session_id=? ORDER BY id", (session_id,)
        ):
            events = self.read_events(attempt.get("log_path"))
            result.append({
                "id": attempt["id"],
                "harness": attempt.get("harness_key"),
                "model": attempt.get("model"),
                "label": "%s attempt #%s" % (attempt.get("harness_key") or "Harness", attempt["id"]),
                "status": attempt["status"],
                "verification": (attempt.get("verify_output") or "")[-2000:],
                "error": attempt.get("error"),
                "progress": [event["text"] for event in events if event["kind"] == "message"][-4:],
            })
        return result

    def handoff_pack(
        self,
        task: Dict[str, Any],
        session: Dict[str, Any],
        root: Optional[Path],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        messages = self.rows(
            "SELECT role,content FROM task_messages WHERE session_id=? "
            "AND role IN ('user','assistant') ORDER BY id",
            (session["id"],),
        )
        workspace = snapshot if snapshot is not None else (
            self.workspace_snapshot(root)
            if root
            else {"available": False, "reason": "No workspace snapshot was available."}
        )
        return {
            "task": task["text"],
            "source_session": session["session_number"],
            "harness": session.get("harness_key"),
            "model": session.get("model"),
            "messages": [
                {"role": item["role"], "content": item["content"][-3000:]}
                for item in messages[-12:]
            ],
            "attempts": self.attempt_summary(session["id"]),
            "workspace": workspace,
            "next_action": "Inspect the workspace and current diff before making further changes.",
        }

    def seal(
        self,
        task: Dict[str, Any],
        session: Dict[str, Any],
        reason: str,
        root: Optional[Path],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        pack = self.handoff_pack(task, session, root, snapshot)
        self.execute(
            "UPDATE task_sessions SET status='sealed',sealed_at=?,close_reason=?,"
            "handoff_json=?,baseline_json=? WHERE id=?",
            (
                self.clock(), reason, json.dumps(pack, separators=(",", ":")),
                json.dumps(pack["workspace"], separators=(",", ":")), session["id"],
            ),
        )
        return pack

    def rotate(
        self,
        task: Dict[str, Any],
        reason: str,
        root: Optional[Path],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        session = self.active(task["id"])
        if self.message_chars(session["id"]) == 0 and not self.rows(
            "SELECT id FROM attempts WHERE session_id=?", (session["id"],)
        ):
            return session
        pack = self.seal(task, session, reason, root, snapshot)
        session_id = self.execute(
            """INSERT INTO task_sessions(
                task_id,session_number,harness_key,model,status,opened_at,integrity_status
            ) VALUES(?,?,?,?, 'active', ?, ?)""",
            (
                task["id"], session["session_number"] + 1, session.get("harness_key"),
                session.get("model"), self.clock(),
                "not_checked" if pack["workspace"].get("available") else "unavailable",
            ),
        )
        return self.one("SELECT * FROM task_sessions WHERE id=?", (session_id,))

    def latest_handoff(self, task_id: int) -> Optional[Dict[str, Any]]:
        session = self.one(
            "SELECT * FROM task_sessions WHERE task_id=? AND status='sealed' "
            "ORDER BY session_number DESC LIMIT 1",
            (task_id,),
        )
        return json.loads(session["handoff_json"]) if session and session.get("handoff_json") else None

    def reconcile_snapshot(
        self, task: Dict[str, Any], session: Dict[str, Any], actual: Dict[str, Any]
    ) -> Tuple[bool, str]:
        handoff = self.latest_handoff(task["id"])
        if not handoff:
            detail = "Initial session; no predecessor to reconcile."
            self.execute(
                "UPDATE task_sessions SET integrity_status='passed',integrity_detail=? WHERE id=?",
                (detail, session["id"]),
            )
            return True, "Initial session reconciled."
        expected = handoff.get("workspace") or {}
        if not expected.get("available") or not actual.get("available"):
            detail = "Git workspace comparison is unavailable for this session."
            self.execute(
                "UPDATE task_sessions SET integrity_status='unavailable',integrity_detail=? WHERE id=?",
                (detail, session["id"]),
            )
            return True, "Git workspace comparison is unavailable; the harness must inspect the folder."
        matches = (
            expected.get("base_sha") == actual.get("base_sha")
            and expected.get("diff_hash") == actual.get("diff_hash")
        )
        detail = (
            "Workspace baseline and diff match the sealed handoff."
            if matches
            else "Workspace differs from the sealed handoff; review is required before continuing."
        )
        self.execute(
            "UPDATE task_sessions SET integrity_status=?,integrity_detail=? WHERE id=?",
            ("passed" if matches else "mismatch", detail, session["id"]),
        )
        return matches, detail

    def reconcile(
        self, task: Dict[str, Any], session: Dict[str, Any], root: Path
    ) -> Tuple[bool, str]:
        return self.reconcile_snapshot(task, session, self.workspace_snapshot(root))

