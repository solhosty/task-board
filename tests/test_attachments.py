"""Attachment storage, delivery to each harness, and the HTTP round trip.

No harness runs here: the checks are that a stored attachment reaches every
adapter's command line in the form that adapter supports, and that the same
file survives the API path a user takes in the dashboard.
"""
import base64
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
from harness_rotation import attachments

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


class AttachmentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="attachment-store-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_supported_types_are_classified_by_extension(self):
        self.assertEqual(attachments.classify("shot.PNG"), ("image", "image/png"))
        self.assertEqual(attachments.classify("notes.md"), ("file", "text/markdown"))
        with self.assertRaises(ValueError):
            attachments.classify("payload.exe")

    def test_a_stored_name_cannot_escape_its_task_folder(self):
        record = attachments.save(self.root, 7, "../../etc/pass wd.txt", b"hello")
        stored = Path(record["stored_path"])
        self.assertEqual(stored.parent, attachments.directory(self.root, 7))
        self.assertEqual(stored.read_bytes(), b"hello")
        self.assertNotIn("/", record["filename"])

    def test_oversized_files_are_refused_before_they_are_written(self):
        with self.assertRaises(ValueError):
            attachments.save(self.root, 7, "big.txt", b"x" * (attachments.MAX_BYTES + 1))
        self.assertFalse(attachments.directory(self.root, 7).exists()
                         and any(attachments.directory(self.root, 7).iterdir()))

    def test_data_urls_and_plain_base64_both_decode(self):
        encoded = base64.b64encode(PNG).decode()
        self.assertEqual(attachments.decode(encoded), PNG)
        self.assertEqual(attachments.decode("data:image/png;base64," + encoded), PNG)
        with self.assertRaises(ValueError):
            attachments.decode("not base64 at all!!")

    def test_a_record_whose_file_vanished_is_dropped(self):
        record = attachments.save(self.root, 7, "notes.txt", b"hello")
        self.assertEqual(attachments.existing([record]), [record])
        Path(record["stored_path"]).unlink()
        self.assertEqual(attachments.existing([record]), [])


class AttachmentDeliveryTests(unittest.TestCase):
    """Every harness receives every attachment; only the form differs."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="attachment-delivery-")
        root = Path(self.temp.name)
        self.image = attachments.save(root, 1, "screen.png", PNG)
        self.notes = attachments.save(root, 1, "notes.md", b"# spec")
        self.records = [self.image, self.notes]
        self.folder = str(attachments.directory(root, 1))

    def tearDown(self):
        self.temp.cleanup()

    def command(self, key):
        return app.configured_command({"key": key, "model": "default"}, Path("/tmp/work"),
                                      "Do the thing", "standard", None, self.records)

    def test_codex_attaches_images_on_its_own_flag(self):
        command = self.command("codex")
        self.assertIn("--image", command)
        self.assertIn(self.image["stored_path"], command)
        self.assertNotIn(self.notes["stored_path"], command,
                         "Codex --image takes images only; other files travel as prompt paths")

    def test_opencode_attaches_every_file(self):
        command = self.command("opencode")
        self.assertEqual([command[command.index(path) - 1] for path in
                          (self.image["stored_path"], self.notes["stored_path"])], ["--file", "--file"])

    def test_claude_is_allowed_to_read_the_attachment_folder(self):
        command = self.command("claude")
        self.assertEqual(command[command.index("--add-dir") + 1], self.folder)

    def test_droid_relies_on_the_prompt_paths_alone(self):
        self.assertEqual(self.command("droid"), app.configured_command(
            {"key": "droid", "model": "default"}, Path("/tmp/work"), "Do the thing", "standard"))

    def test_every_adapter_keeps_the_prompt_as_its_final_argument(self):
        for key in ("codex", "claude", "droid", "opencode"):
            self.assertEqual(self.command(key)[-1], "Do the thing", key)

    def test_the_prompt_section_names_each_absolute_path(self):
        described = attachments.describe(self.records)
        self.assertIn(self.image["stored_path"], described)
        self.assertIn(self.notes["stored_path"], described)
        self.assertEqual(attachments.describe([]), "")


class AttachmentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="attachment-api-")
        root = Path(self.temp.name)
        self.original = (app.DATA_ROOT, app.DB_PATH)
        app.DATA_ROOT = root / "state"
        app.DB_PATH = app.DATA_ROOT / "state.sqlite3"
        app.init_db()
        self.repo = root / "repo"
        self.repo.mkdir()
        app.execute("INSERT INTO projects (name,repo_path,verify_command,created_at) VALUES (?,?,?,?)",
                    ("Example", str(self.repo), "true", app.now()))
        project_id = app.one("SELECT id FROM projects")["id"]
        handler = functools.partial(app.API, directory=str(app.STATIC_ROOT))
        self.server = app.ThreadingHTTPServer((app.HOST, 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://%s:%d" % (app.HOST, self.server.server_address[1])
        status, created = self.call("/api/projects/%d/tasks" % project_id, "POST", {"text": "Match the mockup"})
        self.assertEqual(status, 201, created)
        self.task_id = created["id"]

    def tearDown(self):
        self.server.shutdown()
        app.DATA_ROOT, app.DB_PATH = self.original
        self.temp.cleanup()

    def call(self, path, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={} if data is None else {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as problem:
            return problem.code, json.loads(problem.read())

    def upload(self, filename, data):
        return self.call("/api/tasks/%d/attachments" % self.task_id, "POST",
                         {"filename": filename, "data": base64.b64encode(data).decode()})

    def test_upload_is_listed_readable_and_removable(self):
        status, payload = self.upload("screen.png", PNG)
        self.assertEqual(status, 201, payload)
        attachment = payload["attachment"]
        self.assertEqual(attachment["kind"], "image")
        self.assertEqual(attachment["byte_size"], len(PNG))

        _, detail = self.call("/api/tasks/%d" % self.task_id)
        self.assertEqual([item["id"] for item in detail["attachments"]], [attachment["id"]])

        with urllib.request.urlopen("%s/api/tasks/%d/attachments/%d"
                                    % (self.base, self.task_id, attachment["id"])) as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.read(), PNG)

        status, _ = self.call("/api/tasks/%d/attachments/%d" % (self.task_id, attachment["id"]), "DELETE")
        self.assertEqual(status, 200)
        self.assertFalse(Path(attachment["stored_path"]).exists())
        _, detail = self.call("/api/tasks/%d" % self.task_id)
        self.assertEqual(detail["attachments"], [])

    def test_result_screenshot_is_listed_and_served_separately_from_prompt_attachments(self):
        attempt_id = app.execute("""INSERT INTO attempts(task_id,harness_key,status,started_at)
                                  VALUES(?,'codex','completed',?)""", (self.task_id, app.now()))
        captured = Path(self.temp.name) / 'after.png'
        captured.write_bytes(PNG)
        saved = app.save_attempt_screenshot(attempt_id, captured)
        self.assertIsNotNone(saved)
        _, detail = self.call("/api/tasks/%d" % self.task_id)
        self.assertEqual(detail['result_screenshots'][str(attempt_id)][0]['filename'], 'after.png')
        with urllib.request.urlopen("%s/api/attempts/%d/screenshots/%d" % (self.base, attempt_id, saved['id'])) as response:
            self.assertEqual(response.headers['Content-Type'], 'image/png')
            self.assertEqual(response.read(), PNG)
        self.assertEqual(detail['attachments'], [], 'result images must not be re-sent as task input')

    def test_an_unsupported_type_is_refused_with_a_readable_reason(self):
        status, payload = self.upload("installer.exe", b"MZ")
        self.assertEqual(status, 400)
        self.assertIn("supported attachment type", payload["error"])

    def test_sending_a_message_ties_pending_attachments_to_it(self):
        _, payload = self.upload("notes.md", b"# spec")
        self.call("/api/tasks/%d/messages" % self.task_id, "POST", {"content": "Use this spec", "start": False})
        stored = app.one("SELECT * FROM task_attachments WHERE id=?", (payload["attachment"]["id"],))
        message = app.one("SELECT * FROM task_messages WHERE task_id=? ORDER BY id DESC LIMIT 1", (self.task_id,))
        self.assertEqual(stored["message_id"], message["id"])

    def test_the_prompt_carries_the_attachment_to_the_harness(self):
        _, payload = self.upload("screen.png", PNG)
        task = app.one("SELECT * FROM tasks WHERE id=?", (self.task_id,))
        project = app.one("SELECT * FROM projects WHERE id=?", (task["project_id"],))
        self.assertIn(payload["attachment"]["stored_path"], app.task_prompt(task, project))

    def test_a_new_task_can_be_created_with_its_files_already_attached(self):
        project_id = app.one("SELECT id FROM projects")["id"]
        status, created = self.call("/api/projects/%d/tasks" % project_id, "POST", {
            "text": "Match this mockup",
            "attachments": [{"filename": "mockup.png", "data": base64.b64encode(PNG).decode()}]})
        self.assertEqual(status, 201, created)
        _, detail = self.call("/api/tasks/%d" % created["id"])
        self.assertEqual([item["filename"] for item in detail["attachments"]], ["mockup.png"])
        first_message = app.one("SELECT id FROM task_messages WHERE task_id=? ORDER BY id", (created["id"],))
        self.assertEqual(detail["attachments"][0]["message_id"], first_message["id"],
                         "The files belong to the message that opened the task")

    def test_a_rejected_file_leaves_no_task_behind(self):
        project_id = app.one("SELECT id FROM projects")["id"]
        before = len(app.rows("SELECT id FROM tasks"))
        status, payload = self.call("/api/projects/%d/tasks" % project_id, "POST", {
            "text": "Match this mockup",
            "attachments": [{"filename": "installer.exe", "data": base64.b64encode(b"MZ").decode()}]})
        self.assertEqual(status, 400)
        self.assertIn("supported attachment type", payload["error"])
        self.assertEqual(len(app.rows("SELECT id FROM tasks")), before)

    def test_deleting_the_task_removes_its_stored_files(self):
        _, payload = self.upload("screen.png", PNG)
        folder = Path(payload["attachment"]["stored_path"]).parent
        status, _ = self.call("/api/tasks/%d" % self.task_id, "DELETE")
        self.assertEqual(status, 200)
        self.assertFalse(folder.exists())
        self.assertEqual(app.rows("SELECT * FROM task_attachments WHERE task_id=?", (self.task_id,)), [])


class AttemptFileTests(unittest.TestCase):
    """What the harness produced, listed and served back from its worktree."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="attempt-files-")
        root = Path(self.temp.name).resolve()
        self.original = (app.DATA_ROOT, app.DB_PATH)
        app.DATA_ROOT = root / "state"
        app.DB_PATH = app.DATA_ROOT / "state.sqlite3"
        app.init_db()
        self.work = root / "work"
        self.work.mkdir()
        for command in (["init"], ["config", "user.email", "files@example.test"],
                        ["config", "user.name", "Files test"]):
            app.git(command, self.work)
        (self.work / "README.md").write_text("start\n")
        app.git(["add", "README.md"], self.work)
        app.git(["commit", "-m", "fixture"], self.work)
        self.base = app.git(["rev-parse", "HEAD"], self.work).stdout.strip()
        (self.work / "README.md").write_text("changed\n")
        (self.work / "diagram.png").write_bytes(PNG)
        app.execute("INSERT INTO projects (name,repo_path,verify_command,created_at) VALUES (?,?,?,?)",
                    ("Example", str(self.work), "true", app.now()))
        app.execute("INSERT INTO tasks(project_id,text,task_order,status,created_at) VALUES(1,'Draw it',0,'pending',?)",
                    (app.now(),))
        self.attempt_id = app.execute(
            """INSERT INTO attempts(task_id,harness_key,model,selection,status,started_at,worktree_path,base_sha)
               VALUES(1,'codex','default','test','complete',?,?,?)""",
            (app.now(), str(self.work), self.base))
        handler = functools.partial(app.API, directory=str(app.STATIC_ROOT))
        self.server = app.ThreadingHTTPServer((app.HOST, 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://%s:%d" % (app.HOST, self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        app.DATA_ROOT, app.DB_PATH = self.original
        self.temp.cleanup()

    def test_created_and_changed_files_are_both_listed(self):
        attempt = app.one("SELECT * FROM attempts WHERE id=?", (self.attempt_id,))
        listing = app.attempt_files(attempt)
        self.assertTrue(listing["available"])
        by_path = {item["path"]: item for item in listing["files"]}
        self.assertEqual(by_path["diagram.png"]["status"], "added")
        self.assertEqual(by_path["diagram.png"]["kind"], "image")
        self.assertEqual(by_path["README.md"]["status"], "modified")

    def test_a_produced_file_is_served_with_its_own_type(self):
        with urllib.request.urlopen("%s/api/attempts/%d/files?path=diagram.png"
                                    % (self.url, self.attempt_id)) as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.read(), PNG)

    def test_a_path_outside_the_attempt_is_refused(self):
        request = urllib.request.Request(
            "%s/api/attempts/%d/files?path=../../etc/hosts" % (self.url, self.attempt_id))
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request)
        self.assertEqual(refused.exception.code, 400)

    def test_a_missing_workspace_explains_itself(self):
        attempt = dict(app.one("SELECT * FROM attempts WHERE id=?", (self.attempt_id,)),
                       worktree_path="/home/coder/.harness-runner/tasks/gone")
        listing = app.attempt_files(attempt)
        self.assertFalse(listing["available"])
        self.assertIn("Coder runner", listing["reason"])


if __name__ == "__main__":
    unittest.main()
