"""Scoped harness ordering: a task order overrides a project order, which
overrides the global chain, and failover walks whichever order applies."""
import functools
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class HarnessOrderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-order-")
        root = Path(self.temp.name)
        app.DATA_ROOT = root / "state"
        app.DB_PATH = app.DATA_ROOT / "state.sqlite3"
        app.init_db()
        repo = root / "Example app"
        repo.mkdir()
        app.execute("INSERT INTO projects (name,repo_path,verify_command,created_at) VALUES (?,?,?,?)",
                    ("Example app", str(repo), "true", app.now()))
        self.project_id = app.one("SELECT id FROM projects")["id"]
        app.execute("INSERT INTO tasks (project_id,text,task_order,created_at) VALUES (?,?,0,?)",
                    (self.project_id, "Ship it", app.now()))
        self.task_id = app.one("SELECT id FROM tasks")["id"]
        self.task = app.one("SELECT * FROM tasks WHERE id=?", (self.task_id,))
        handler = functools.partial(app.API, directory=str(app.STATIC_ROOT))
        self.server = app.ThreadingHTTPServer((app.HOST, 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://%s:%d" % (app.HOST, self.server.server_address[1])
        self.everything = app.global_harness_order()

    def tearDown(self):
        self.server.shutdown()
        self.temp.cleanup()

    def call(self, path, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={} if data is None else {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as problem:
            return problem.code, json.loads(problem.read())

    def reversed_order(self):
        return list(reversed(self.everything))

    # -- resolution -------------------------------------------------------

    def test_everything_inherits_the_global_chain_by_default(self):
        order, source = app.resolve_harness_order(self.task)
        self.assertEqual(order, self.everything)
        self.assertEqual(source, "global")

    def test_project_order_overrides_global(self):
        app.set_harness_order("project", self.project_id, self.reversed_order())
        order, source = app.resolve_harness_order(self.task)
        self.assertEqual(order, self.reversed_order())
        self.assertEqual(source, "project")

    def test_task_order_overrides_project(self):
        app.set_harness_order("project", self.project_id, self.reversed_order())
        rotated = self.everything[1:] + self.everything[:1]
        app.set_harness_order("task", self.task_id, rotated)
        order, source = app.resolve_harness_order(self.task)
        self.assertEqual(order, rotated)
        self.assertEqual(source, "task")

    def test_clearing_a_task_order_falls_back_to_the_project(self):
        app.set_harness_order("project", self.project_id, self.reversed_order())
        app.set_harness_order("task", self.task_id, self.everything)
        app.clear_harness_order("task", self.task_id)
        order, source = app.resolve_harness_order(self.task)
        self.assertEqual(order, self.reversed_order())
        self.assertEqual(source, "project")

    def test_a_harness_missing_from_a_stored_order_is_appended(self):
        """An override saved before a new harness existed must not drop it."""
        partial = self.everything[:2]
        with app.DB_LOCK, app.db() as conn:
            for position, key in enumerate(partial):
                conn.execute("INSERT INTO harness_orders(scope,scope_id,harness_key,position,updated_at)"
                             " VALUES('project',?,?,?,?)", (self.project_id, key, position, app.now()))
        order, _ = app.resolve_harness_order(self.task)
        self.assertEqual(order[:2], partial)
        self.assertEqual(sorted(order), sorted(self.everything), "every harness still appears exactly once")

    def test_eligible_harnesses_follows_the_scoped_order(self):
        app.execute("UPDATE harnesses SET enabled=1,installed=1,status='ready',billing_confirmed=1")
        app.set_harness_order("project", self.project_id, self.reversed_order())
        eligible = [item["key"] for item in app.eligible_harnesses(self.task)]
        baseline = [item["key"] for item in app.eligible_harnesses(None)]
        self.assertTrue(eligible, "the test needs at least one eligible harness")
        self.assertEqual(eligible, [key for key in self.reversed_order() if key in eligible])
        self.assertNotEqual(eligible, baseline, "the scoped order must differ from global here")

    # -- routes -----------------------------------------------------------

    def test_get_reports_where_the_order_came_from(self):
        status, payload = self.call("/api/harness-order?scope=project:%d" % self.project_id)
        self.assertEqual(status, 200)
        self.assertFalse(payload["overridden"])
        self.assertEqual(payload["source"], "inherited")
        self.assertEqual(payload["order"], self.everything)

    def test_post_and_reset_a_project_order(self):
        status, _ = self.call("/api/harness-order", "POST",
                              {"scope": "project:%d" % self.project_id, "order": self.reversed_order()})
        self.assertEqual(status, 200)
        status, payload = self.call("/api/harness-order?scope=project:%d" % self.project_id)
        self.assertTrue(payload["overridden"])
        self.assertEqual(payload["order"], self.reversed_order())
        status, _ = self.call("/api/harness-order?scope=project:%d" % self.project_id, "DELETE")
        self.assertEqual(status, 200)
        status, payload = self.call("/api/harness-order?scope=project:%d" % self.project_id)
        self.assertFalse(payload["overridden"])
        self.assertEqual(payload["order"], self.everything)

    def test_a_task_reports_the_project_order_as_its_inheritance(self):
        self.call("/api/harness-order", "POST",
                  {"scope": "project:%d" % self.project_id, "order": self.reversed_order()})
        status, payload = self.call("/api/harness-order?scope=task:%d" % self.task_id)
        self.assertEqual(payload["inherited"], self.reversed_order())
        self.assertFalse(payload["overridden"])

    def test_global_post_still_writes_chain_position(self):
        status, _ = self.call("/api/harness-order", "POST", {"order": self.reversed_order()})
        self.assertEqual(status, 200)
        self.assertEqual(app.global_harness_order(), self.reversed_order())

    def test_global_cannot_be_reset(self):
        status, _ = self.call("/api/harness-order?scope=global", "DELETE")
        self.assertEqual(status, 400)

    def test_partial_order_is_rejected(self):
        status, _ = self.call("/api/harness-order", "POST",
                              {"scope": "project:%d" % self.project_id, "order": self.everything[:2]})
        self.assertEqual(status, 400)

    def test_unknown_scope_is_rejected(self):
        for scope in ("project:99999", "task:99999", "nonsense"):
            status, _ = self.call("/api/harness-order?scope=" + scope)
            self.assertEqual(status, 400, scope)

    def test_deleting_a_task_removes_its_override(self):
        app.set_harness_order("task", self.task_id, self.reversed_order())
        status, _ = self.call("/api/tasks/%d" % self.task_id, "DELETE")
        self.assertEqual(status, 200)
        left = app.rows("SELECT * FROM harness_orders WHERE scope='task' AND scope_id=?", (self.task_id,))
        self.assertEqual(left, [], "a deleted task must not leave its order behind")


if __name__ == "__main__":
    unittest.main()
