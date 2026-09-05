"""End-to-end checks for the /api/memories routes over real HTTP.

Each test runs against a disposable HOME so the real Claude and Codex stores on
this machine are never read or written.
"""
import functools
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

NOTE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,79}\.md$")


class MemoryApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mem-api-")
        root = Path(self.temp.name)
        self.home = root / "home"
        self.home.mkdir()
        self._env = {key: os.environ.get(key) for key in ("HOME", "CODEX_HOME")}
        os.environ["HOME"] = str(self.home)
        os.environ["CODEX_HOME"] = str(self.home / ".codex")
        app.DATA_ROOT = root / "state"
        app.DB_PATH = app.DATA_ROOT / "state.sqlite3"
        app.init_db()
        self.repo = root / "Example app"
        self.repo.mkdir()
        app.execute("INSERT INTO projects (name,repo_path,verify_command,created_at) VALUES (?,?,?,?)",
                    ("Example app", str(self.repo), "true", app.now()))
        self.project_id = app.one("SELECT id FROM projects")["id"]
        handler = functools.partial(app.API, directory=str(app.STATIC_ROOT))
        self.server = app.ThreadingHTTPServer((app.HOST, 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://%s:%d" % (app.HOST, self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.temp.cleanup()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

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

    def scope(self):
        return "project:%d" % self.project_id

    # -- overview ---------------------------------------------------------

    def test_overview_lists_both_harnesses(self):
        status, payload = self.call("/api/memories")
        self.assertEqual(status, 200)
        claude, codex = payload["harnesses"]
        self.assertEqual(claude["key"], "claude")
        self.assertFalse(claude["global_enabled"], "Claude starts per-project")
        self.assertTrue(any(item.get("project_id") == self.project_id for item in claude["scopes"]))
        self.assertEqual(codex["status"], "disabled", "Codex memories ship turned off")

    def test_claude_project_slug_matches_the_harness_layout(self):
        from harness_rotation import memories
        self.assertEqual(memories.claude_project_slug("/Users/x/Documents/research-harness"),
                         "-Users-x-Documents-research-harness")

    # -- Claude -----------------------------------------------------------

    def test_claude_entry_round_trip(self):
        status, _ = self.call("/api/memories/claude", "POST", {
            "scope": self.scope(), "title": "Verify command",
            "description": "How to test", "type": "project", "body": "Run pytest."})
        self.assertEqual(status, 200)
        status, payload = self.call("/api/memories/claude?scope=" + self.scope())
        self.assertEqual(len(payload["entries"]), 1)
        entry = payload["entries"][0]
        self.assertEqual(entry["title"], "Verify command", "the typed title survives a round trip")
        self.assertEqual(entry["type"], "project")
        self.assertTrue((Path(payload["path"]) / "MEMORY.md").exists(), "the index Claude reads first")

    def test_claude_delete_updates_the_index(self):
        self.call("/api/memories/claude", "POST", {
            "scope": self.scope(), "title": "Verify command", "description": "How to test",
            "type": "project", "body": "Run pytest."})
        status, _ = self.call("/api/memories/claude/verify-command?scope=" + self.scope(), "DELETE")
        self.assertEqual(status, 200)
        status, payload = self.call("/api/memories/claude?scope=" + self.scope())
        self.assertEqual(payload["entries"], [])
        self.assertFalse((Path(payload["path"]) / "MEMORY.md").exists(),
                         "an empty store leaves no dangling index")

    def test_claude_reads_files_written_by_the_harness(self):
        status, payload = self.call("/api/memories/claude?scope=" + self.scope())
        directory = Path(payload["path"])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "no_async_functions.md").write_text(
            "---\nname: no_async_functions\ndescription: Avoid async\n"
            "metadata:\n  type: feedback\n---\n\nNo async.\n", encoding="utf-8")
        status, payload = self.call("/api/memories/claude?scope=" + self.scope())
        entry = payload["entries"][0]
        self.assertEqual(entry["type"], "feedback")
        self.assertEqual(entry["title"], "No Async Functions", "a slug recovers a readable title")

    def test_claude_global_toggle_switches_scopes(self):
        status, payload = self.call("/api/memories/claude?scope=global")
        self.assertEqual(status, 400, "no global list until one is configured")
        status, payload = self.call("/api/memories/claude/global", "POST", {"enabled": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["global_enabled"])
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertIn("autoMemoryDirectory", settings)
        status, _ = self.call("/api/memories/claude", "POST", {
            "scope": "global", "title": "No async", "description": "Avoid async",
            "type": "feedback", "body": "Do not use async."})
        self.assertEqual(status, 200)
        status, _ = self.call("/api/memories/claude?scope=" + self.scope())
        self.assertEqual(status, 400, "per-project turns off while one global list is in use")
        self.call("/api/memories/claude/global", "POST", {"enabled": False})
        status, _ = self.call("/api/memories/claude?scope=" + self.scope())
        self.assertEqual(status, 200, "turning it back off restores per-project lists")

    def test_claude_global_toggle_preserves_other_settings(self):
        path = self.home / ".claude"
        path.mkdir(parents=True, exist_ok=True)
        (path / "settings.json").write_text(json.dumps({"model": "opus", "theme": "dark"}), encoding="utf-8")
        self.call("/api/memories/claude/global", "POST", {"enabled": True})
        settings = json.loads((path / "settings.json").read_text())
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["theme"], "dark")

    # -- Codex ------------------------------------------------------------

    def enable_codex(self):
        config = self.home / ".codex"
        config.mkdir(parents=True, exist_ok=True)
        (config / "config.toml").write_text(
            'model = "gpt-6-astra"\n\n[features]\njs_repl = false\n', encoding="utf-8")
        return self.call("/api/memories/codex/enable", "POST", {})

    def test_codex_refuses_until_enabled(self):
        status, _ = self.call("/api/memories/codex?scope=global")
        self.assertEqual(status, 400)

    def test_codex_enable_preserves_existing_features(self):
        status, _ = self.enable_codex()
        self.assertEqual(status, 200)
        config = (self.home / ".codex" / "config.toml").read_text()
        self.assertIn("memories = true", config)
        self.assertIn("js_repl = false", config, "other feature flags survive")
        self.assertIn('model = "gpt-6-astra"', config)

    def test_codex_project_entry_carries_a_scope_file(self):
        self.enable_codex()
        status, _ = self.call("/api/memories/codex", "POST", {
            "scope": self.scope(), "title": "Deploy", "description": "How to ship",
            "type": "project", "body": "Use the deploy script."})
        self.assertEqual(status, 200)
        status, payload = self.call("/api/memories/codex?scope=" + self.scope())
        self.assertEqual(len(payload["entries"]), 1)
        self.assertTrue(payload["pending"], "Codex folds entries in on its next consolidation")
        scope_file = Path(payload["path"]) / "scope.json"
        self.assertEqual(json.loads(scope_file.read_text())["cwd"], str(self.repo),
                         "scope.json is how Codex limits these to one project")
        instructions = Path(payload["path"]).parents[1] / "instructions.md"
        self.assertTrue(instructions.exists(), "an extension folder is only legible with instructions")

    def test_codex_note_filename_matches_the_enforced_format(self):
        self.enable_codex()
        status, payload = self.call("/api/memories/codex/notes", "POST",
                                    {"text": "Forget the old deploy steps"})
        self.assertEqual(status, 200)
        self.assertRegex(payload["created"], NOTE_NAME)

    # -- guards -----------------------------------------------------------

    def test_unknown_harness_is_rejected(self):
        status, _ = self.call("/api/memories/droid?scope=global")
        self.assertEqual(status, 400)

    def test_unknown_project_is_rejected(self):
        status, _ = self.call("/api/memories/claude?scope=project:99999")
        self.assertEqual(status, 400)

    def test_empty_body_is_rejected(self):
        status, _ = self.call("/api/memories/claude", "POST",
                              {"scope": self.scope(), "title": "x", "body": "  "})
        self.assertEqual(status, 400)

    # -- per-task memory mode --------------------------------------------

    def test_task_records_its_memory_mode(self):
        status, payload = self.call("/api/projects/%d/tasks" % self.project_id, "POST",
                                    {"text": "Ship the thing", "memory_mode": "read_only"})
        self.assertEqual(status, 201)
        task = app.one("SELECT * FROM tasks WHERE id=?", (payload["id"],))
        self.assertEqual(task["memory_mode"], "read_only")

    def test_task_memory_mode_defaults_to_inherit(self):
        status, payload = self.call("/api/projects/%d/tasks" % self.project_id, "POST",
                                    {"text": "Ship the thing"})
        task = app.one("SELECT * FROM tasks WHERE id=?", (payload["id"],))
        self.assertEqual(task["memory_mode"], "inherit")

    def test_unknown_memory_mode_is_rejected(self):
        status, _ = self.call("/api/projects/%d/tasks" % self.project_id, "POST",
                              {"text": "Ship the thing", "memory_mode": "sometimes"})
        self.assertEqual(status, 400)

    def test_task_memory_mode_can_be_updated(self):
        status, payload = self.call("/api/projects/%d/tasks" % self.project_id, "POST",
                                    {"text": "Ship the thing"})
        status, _ = self.call("/api/tasks/%d" % payload["id"], "POST", {"memory_mode": "off"})
        self.assertEqual(status, 200)
        task = app.one("SELECT * FROM tasks WHERE id=?", (payload["id"],))
        self.assertEqual(task["memory_mode"], "off")

    def test_unknown_api_route_answers_json(self):
        status, payload = self.call("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("restart", payload["error"].lower(),
                      "a stale server should say so rather than serve the index page")


class MemoryOverrideTest(unittest.TestCase):
    """The `-c` flags are per-invocation, so a task's choice never persists into
    the user's config.toml or the next run."""

    def setUp(self):
        from harness_rotation import adapters
        self.adapters = adapters
        self.codex = {"key": "codex", "model": "default", "tool_permissions": "standard"}
        self.claude = {"key": "claude", "model": "default", "tool_permissions": "standard"}

    def build(self, harness, memory):
        return self.adapters.configured_command(
            self.adapters.ADAPTERS, harness, Path("/repo"), "do it", None, memory)

    def test_inherit_adds_nothing(self):
        for mode in (None, "inherit"):
            self.assertNotIn("-c", self.build(self.codex, mode))

    def test_read_only_reads_without_contributing(self):
        command = self.build(self.codex, "read_only")
        self.assertIn("memories.use_memories=true", command)
        self.assertIn("memories.generate_memories=false", command)

    def test_off_disables_both_directions(self):
        command = self.build(self.codex, "off")
        self.assertIn("memories.use_memories=false", command)
        self.assertIn("memories.generate_memories=false", command)

    def test_flags_sit_after_the_subcommand_and_keep_the_prompt_last(self):
        command = self.build(self.codex, "off")
        self.assertEqual(command[:2], ["codex", "exec"], "-c must follow the subcommand")
        self.assertEqual(command[-1], "do it", "the prompt stays the final argument")

    def test_other_harnesses_are_untouched(self):
        self.assertEqual(self.build(self.claude, "off"), self.build(self.claude, "inherit"),
                         "memories.* is a Codex switch and must not leak elsewhere")

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            self.build(self.codex, "sometimes")


if __name__ == "__main__":
    unittest.main()
