"""Private protocol bridges to model CLIs inside a persistent Coder runner."""

import base64
import json
from pathlib import Path
from queue import Empty, Queue
import re
import shlex
import subprocess
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse


REMOTE_CODEX_BIN = "/home/coder/.codex/packages/standalone/current/bin/codex"


class CoderExternalAuthRequired(RuntimeError):
    """A Coder user must finish a provider login before provisioning can continue."""

    def __init__(self, provider_id: str, display_name: str, login_url: str):
        self.provider_id = provider_id
        self.display_name = display_name
        self.login_url = login_url
        super().__init__("%s authorization is required. Open %s" % (display_name, login_url))


class RemoteCodexAppServer:
    """A short-lived, private stdio bridge to Codex in one Coder runner.

    The bridge transports JSON-RPC only. It never receives, persists, or logs
    OpenAI credentials: Codex owns its managed login in the runner's home.
    """

    def __init__(self, workspace_name: str, environment: Dict[str, str]):
        self.process = subprocess.Popen(
            ["coder", "ssh", "--wait", "yes", workspace_name, "--", REMOTE_CODEX_BIN, "app-server"],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.responses: Dict[int, Queue] = {}
        self.notifications: Queue = Queue()
        self.next_id = 1
        self.lock = threading.RLock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.request(
            "initialize",
            {"clientInfo": {"name": "harness_rotation", "title": "Harness Rotation", "version": "1"}},
        )
        self.notify("initialized", {})

    def _read(self) -> None:
        assert self.process.stdout
        for line in self.process.stdout:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("id"), int):
                queue = self.responses.get(payload["id"])
                if queue:
                    queue.put(payload)
            elif isinstance(payload, dict):
                self.notifications.put(payload)

    def send(self, payload: Dict[str, Any]) -> None:
        if self.process.poll() is not None or not self.process.stdin:
            raise RuntimeError("The remote Codex connection closed. Start the connection again.")
        self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def request(
        self, method: str, params: Optional[Dict[str, Any]] = None, timeout: int = 20
    ) -> Dict[str, Any]:
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
            reply: Queue = Queue(maxsize=1)
            self.responses[request_id] = reply
            self.send({"method": method, "id": request_id, "params": params or {}})
        try:
            result = reply.get(timeout=timeout)
        except Empty:
            raise RuntimeError(
                "Codex did not answer in time. Check that the runner is online and retry."
            ) from None
        finally:
            self.responses.pop(request_id, None)
        if result.get("error"):
            raise RuntimeError(str(result["error"].get("message") or "Remote Codex request failed."))
        return result.get("result") or {}

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self.send({"method": method, "params": params})

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()


class RemoteClaudeLogin:
    """A temporary PTY bridge for Claude Code's own interactive login flow.

    Its screen and user input are held only in memory. The bridge never reads or
    persists the credential Claude writes in the runner home.
    """

    def __init__(
        self,
        workspace_name: str,
        environment: Dict[str, str],
        login_script: Optional[Path] = None,
    ):
        script = login_script or Path(__file__).resolve().parents[1] / "infra" / "runner" / "remote_claude_login.py"
        source = base64.b64encode(script.read_bytes()).decode()
        launcher = (
            "import base64;exec(compile(base64.b64decode("
            + repr(source)
            + "),\"<remote-claude-login>\",\"exec\"))"
        )
        command = shlex.join(["python3", "-c", launcher])
        self.process = subprocess.Popen(
            ["coder", "ssh", "--wait", "yes", workspace_name, "--", command],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        self.screen = ""
        self.login_url_requested = False
        self.lock = threading.RLock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout
        while True:
            chunk = self.process.stdout.read(1024)
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace")
            with self.lock:
                self.screen = (self.screen + text)[-24000:]
                # In SSH/container sessions Claude can ask the user to press c to
                # reveal/copy its browser URL. Do that non-sensitive step here.
                if not self.login_url_requested and re.search(
                    r"press\s*c\b|pressc\b", self.screen, re.I
                ):
                    self.login_url_requested = True
                    self._write("c\n")

    def _write(self, value: str) -> None:
        if self.process.poll() is None and self.process.stdin:
            self.process.stdin.write(value.encode("utf-8"))
            self.process.stdin.flush()

    def send(self, value: str) -> None:
        if self.process.poll() is not None or not self.process.stdin:
            raise RuntimeError("The Claude login session closed. Start it again.")
        if not isinstance(value, str) or not value or len(value) > 8192 or "\x00" in value:
            raise ValueError("Enter a valid Claude login response.")
        # Claude Code's current remote-SSH OAuth widget drops a pasted buffer.
        # The dashboard receives one pasted value, but the bridge replays it as
        # ordinary keystrokes and then submits the line. This keeps the callback
        # inside the runner while avoiding the upstream terminal-widget bug.
        for character in value:
            self._write(character)
            time.sleep(0.012)
        self._write("\r")

    def accept_default(self) -> None:
        """Confirm a native terminal menu's currently selected option."""
        if self.process.poll() is None:
            self._write("\r")

    def snapshot(self) -> str:
        with self.lock:
            cleaned = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", self.screen)
            cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", cleaned)
            return cleaned.replace("\r", "")[-16000:]

    def verification_url(self) -> Optional[str]:
        """Extract only Claude's browser continuation URL from wrapped PTY output."""
        with self.lock:
            screen = self.screen
        # Claude's PTY output includes OSC-8 hyperlink metadata around the URL.
        # Strip terminal control sequences before parsing or opening it in a browser.
        screen = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", screen)
        screen = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", screen)
        for start in [match.start() for match in re.finditer(r"https://", screen)]:
            candidate = re.split(r"\n\s*\n", screen[start:], maxsplit=1)[0]
            candidate = re.split(
                r"Paste\s*code\s*here", candidate, maxsplit=1, flags=re.I
            )[0]
            candidate = re.sub(r"\s+", "", candidate)
            parsed = urlparse(candidate)
            if parsed.scheme == "https" and parsed.hostname in {
                "claude.com", "claude.ai", "platform.claude.com", "auth.anthropic.com"
            }:
                return candidate
        return None

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
