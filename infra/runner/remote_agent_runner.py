"""Bounded native-agent execution inside a persistent Coder runner.

This program is sent over Coder SSH. It receives a base64 JSON request,
builds supported CLI arguments itself, and writes one final result marker.
Credentials never leave the runner.
"""
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

MARKER = "__HARNESS_REMOTE_RESULT__"
ROOT = Path("/home/coder/.harness-runner/tasks").resolve()
CODEX_BIN = "/home/coder/.codex/packages/standalone/current/bin/codex"


def command_for(request, root):
    key, model, prompt = request["harness"], request.get("model") or "default", request["prompt"]
    attachments = request.get("attachments") or []
    paths = [str(item.get("stored_path") or "") for item in attachments]
    if any(not path.startswith("/home/coder/.harness-runner/attachments/") for path in paths):
        raise ValueError("Invalid remote attachment path.")
    if key == "codex":
        images = [str(item["stored_path"]) for item in attachments if item.get("kind") == "image"]
        flags = [part for image in images for part in ("--image", image)]
        return [CODEX_BIN, "exec", "--json", "--approve-for-me", "--skip-git-repo-check", "--cd", str(root)] + flags + ([] if model == "default" else ["--model", model]) + [prompt]
    if key == "claude":
        mode = "auto" if request.get("permission_mode") == "auto" else "acceptEdits"
        folders = sorted({str(Path(path).parent) for path in paths})
        flags = [part for folder in folders for part in ("--add-dir", folder)]
        return ["/home/coder/.local/bin/claude", "-p", "--permission-mode", mode, "--verbose", "--output-format", "stream-json"] + flags + ([] if model == "default" else ["--model", model]) + [prompt]
    raise ValueError("Unsupported remote harness.")


def bounded(command, cwd, timeout):
    process = subprocess.Popen(command, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
    expired = False
    def stop():
        nonlocal expired
        if process.poll() is None:
            expired = True
            os.killpg(process.pid, signal.SIGTERM)
    timer = threading.Timer(timeout, stop)
    timer.daemon = True
    timer.start()
    try:
        output, _ = process.communicate()
    finally:
        timer.cancel()
    return process.returncode, output, expired


def capture(command, cwd):
    result = subprocess.run(command, cwd=str(cwd), text=True, capture_output=True, timeout=120)
    return result.stdout if result.returncode in (0, 1) else ""


def main(encoded):
    request = json.loads(base64.urlsafe_b64decode(encoded.encode()).decode())
    worktree = Path(request["worktree_path"]).resolve()
    if ROOT not in worktree.parents or not worktree.is_dir():
        raise ValueError("Invalid task worktree.")
    code, output, timed_out = bounded(command_for(request, worktree), worktree, 600)
    verification, verify_code = "", None
    if code == 0:
        verify_code, verification, verify_timed_out = bounded(["/bin/sh", "-lc", request.get("verify_command") or "true"], worktree, 600)
        timed_out = timed_out or verify_timed_out
    diff = capture(["git", "diff", request["base_sha"], "--"], worktree)
    untracked = capture(["git", "ls-files", "--others", "--exclude-standard", "-z"], worktree).split("\0")
    for name in filter(None, untracked):
        diff += capture(["git", "diff", "--no-index", "--", "/dev/null", name], worktree)
    reply = ""
    reply_path = Path(request.get("reply_path") or "")
    if reply_path.parent == worktree and reply_path.is_file():
        reply = reply_path.read_text(encoding="utf-8", errors="replace")[-16000:]
    print(MARKER + json.dumps({"code": code, "output": output[-120000:], "timed_out": timed_out,
                               "verify_code": verify_code, "verification": verification[-12000:],
                               "diff": diff[-250000:], "reply": reply}, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except Exception as exc:
        print(MARKER + json.dumps({"code": -1, "output": "", "verify_code": None,
                                   "verification": "", "diff": "", "reply": "", "error": str(exc)}))
