"""Tests for backend modules extracted from the compatibility app entry point."""

import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_rotation.adapters import ADAPTERS
from harness_rotation.persistence import MIGRATIONS, connect, initialize
from harness_rotation import credentials


class CredentialTests(unittest.TestCase):
    def test_server_override_wins_without_reading_keychain(self):
        with patch.dict(credentials.os.environ, {
            'HARNESS_CODER_TOKEN_7': 'server-token',
            'HARNESS_CODER_TOKEN': 'fallback-token',
        }, clear=True), patch.object(credentials.subprocess, 'run') as command:
            self.assertEqual(credentials.read_coder_token({'id': 7}), 'server-token')
            self.assertEqual(credentials.read_coder_token({'id': 8}), 'fallback-token')
            command.assert_not_called()

    def test_unconfigured_server_does_not_read_keychain(self):
        with patch.dict(credentials.os.environ, {}, clear=True), \
                patch.object(credentials.subprocess, 'run') as command:
            self.assertIsNone(credentials.read_coder_token({'id': 7, 'token_configured': False}))
            command.assert_not_called()


class AdapterTests(unittest.TestCase):
    def test_builders_preserve_cli_boundaries(self):
        root = Path("/tmp/project with spaces")
        self.assertEqual(
            ADAPTERS["codex"]["build"](root, "custom", "do work"),
            [
                "codex", "exec", "--json", "--approve-for-me", "--skip-git-repo-check",
                "--cd", str(root), "--model", "custom", "do work",
            ],
        )
        self.assertEqual(
            ADAPTERS["opencode"]["build"](root, "default", "do work"),
            ["opencode", "run", "--format", "json", "do work"],
        )


class PersistenceTests(unittest.TestCase):
    def test_schema_history_and_legacy_session_backfill(self):
        with tempfile.TemporaryDirectory(prefix="rotation-persistence-") as directory:
            data_root = Path(directory)
            path = data_root / "state.sqlite3"
            clock = lambda: "2026-09-04T12:00:00+00:00"
            initialize(data_root, path, ADAPTERS, clock)

            with connect(path) as connection:
                versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
                self.assertEqual(versions, {migration.version for migration in MIGRATIONS})
                project_id = connection.execute(
                    "INSERT INTO projects(name,repo_path,verify_command,created_at) VALUES(?,?,?,?)",
                    ("Example", str(data_root), "true", clock()),
                ).lastrowid
                task_id = connection.execute(
                    "INSERT INTO tasks(project_id,text,task_order,created_at) VALUES(?,?,0,?)",
                    (project_id, "Existing task", clock()),
                ).lastrowid
                message_id = connection.execute(
                    "INSERT INTO task_messages(task_id,role,content,created_at) VALUES(?,'user','hello',?)",
                    (task_id, clock()),
                ).lastrowid

            # Reinitialization is safe and backfills records written by releases
            # that predate durable task sessions.
            initialize(data_root, path, ADAPTERS, clock)
            with connect(path) as connection:
                session = connection.execute(
                    "SELECT * FROM task_sessions WHERE task_id=?", (task_id,)
                ).fetchone()
                message = connection.execute(
                    "SELECT * FROM task_messages WHERE id=?", (message_id,)
                ).fetchone()
                self.assertEqual(session["session_number"], 1)
                self.assertEqual(message["session_id"], session["id"])
                columns = {item[1] for item in connection.execute("PRAGMA table_info(tasks)")}
                self.assertTrue({"workflow_stage", "board_position"}.issubset(columns))
                connection.execute(
                    "INSERT INTO board_views(project_id,name,config_json,created_at) VALUES(?,?,?,?)",
                    (project_id, "My view", '{"stages":["planned"]}', clock()),
                )
                view = connection.execute("SELECT name FROM board_views WHERE project_id=?", (project_id,)).fetchone()
                self.assertEqual(view["name"], "My view")


if __name__ == "__main__":
    unittest.main()
