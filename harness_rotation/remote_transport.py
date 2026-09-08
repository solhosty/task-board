"""Validated transport to task helpers running in persistent Coder workspaces."""

import base64
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any, Dict, List, Tuple

# Helper programs that execute *inside* the Coder runner rather than here. They
# are sent over SSH as stdin, so they must stay stdlib-only and must never
# import from this package. `infra/runner/` is their one canonical location.
RUNNER_PAYLOADS = Path("infra") / "runner"


def payload(app_root: Path, name: str) -> str:
    """Read a runner payload program for transmission over Coder SSH."""
    return (app_root / RUNNER_PAYLOADS / name).read_text()



def agent_request(harness: Dict[str, Any], worktree: Dict[str, Any], prompt: str,
                  project: Dict[str, Any], permission_mode: str,
                  attachments: List[Dict[str, Any]] = None) -> str:
    request = {
        "harness": harness["key"], "model": harness.get("model") or "default",
        "prompt": prompt, "permission_mode": permission_mode,
        "worktree_path": worktree["worktree_path"], "base_sha": worktree["base_sha"],
        "verify_command": project.get("verify_command") or "",
        "reply_path": worktree["worktree_path"] + "/.harness-last-message",
        "attachments": attachments or [],
    }
    encoded = base64.urlsafe_b64encode(json.dumps(request, separators=(",", ":")).encode()).decode()
    return shlex.join(["python3", "-", encoded])


def stage_attachments(runner: Dict[str, Any], environment: Dict[str, str], task_key: str,
                      attachments: List[Dict[str, Any]], app_root: Path) -> List[Dict[str, Any]]:
    """Copy verified local files to the runner's non-Git attachment store."""
    if not attachments:
        return []
    metadata, transfer = [], []
    for item in attachments:
        path = Path(item.get("stored_path") or "")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != item.get("sha256") or len(data) != item.get("byte_size"):
            raise RuntimeError("An attachment changed or is missing locally. Remove it or add it again before retrying.")
        details = {key: item[key] for key in ("id", "filename", "media_type", "kind", "byte_size", "sha256")}
        metadata.append(details)
        transfer.append({"sha256": digest, "data": base64.b64encode(data).decode()})
    request = base64.urlsafe_b64encode(json.dumps({"task_key": task_key, "attachments": metadata}, separators=(",", ":")).encode()).decode()
    command = shlex.join(["python3", "-c", payload(app_root, "remote_attachment_stager.py"), request])
    try:
        checked = subprocess.run(
            ["coder", "ssh", "--wait", "yes", runner["workspace_name"], "--", command],
            input=json.dumps(transfer, separators=(",", ":")), env=environment,
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Attachment transfer to the Coder runner failed; retry this task.") from exc
    if checked.returncode:
        raise RuntimeError("Attachment transfer to the Coder runner failed; retry this task.")
    try:
        staged = json.loads(checked.stdout)
    except ValueError as exc:
        raise RuntimeError("The Coder runner returned an unreadable attachment transfer result; retry this task.") from exc
    if not isinstance(staged, list) or len(staged) != len(metadata):
        raise RuntimeError("The Coder runner did not confirm every attachment; retry this task.")
    expected = {item["id"]: item for item in metadata}
    if len(expected) != len(metadata) or {item.get("id") for item in staged} != set(expected):
        raise RuntimeError("The Coder runner did not confirm every attachment; retry this task.")
    for item in staged:
        original = expected.get(item.get("id"))
        remote_path = str(item.get("stored_path") or "")
        expected_path = "/home/coder/.harness-runner/attachments/%s/%s-%s" % (
            task_key, original["sha256"][:12], original["filename"])
        if (not original or item.get("sha256") != original["sha256"] or
                remote_path != expected_path):
            raise RuntimeError("The Coder runner returned an invalid attachment location; retry this task.")
    return staged


def delivery_request(worktree: Dict[str, Any], task: Dict[str, Any], profile: Dict[str, Any]) -> str:
    request = {
        "worktree_path": worktree["worktree_path"], "branch_name": worktree["branch_name"],
        "base_sha": worktree["base_sha"], "repo_url": profile["repo_url"],
        "base_ref": profile["base_ref"],
        "auth_provider_id": profile.get("auth_provider_id") or "github", "title": task["text"],
    }
    encoded = base64.urlsafe_b64encode(json.dumps(request, separators=(",", ":")).encode()).decode()
    return shlex.join(["python3", "-", encoded])


def pr_status_request(pull_request: Dict[str, Any], profile: Dict[str, Any]) -> str:
    request = {
        "action": "status", "pr_url": pull_request["url"],
        "auth_provider_id": profile.get("auth_provider_id") or "github",
    }
    encoded = base64.urlsafe_b64encode(json.dumps(request, separators=(",", ":")).encode()).decode()
    return shlex.join(["python3", "-", encoded])


def workspace_snapshot(runner: Dict[str, Any], environment: Dict[str, str], worktree_path: str,
                       app_root: Path) -> Dict[str, Any]:
    """Read Git state in a Coder task worktree without modifying it."""
    if not worktree_path:
        return {"available": False, "reason": "The remote task worktree is unavailable."}
    command = shlex.join(["python3", "-", worktree_path])
    try:
        checked = subprocess.run(
            ["coder", "ssh", "--wait", "yes", runner["workspace_name"], "--", command],
            input=payload(app_root, "remote_workspace_snapshot.py"), env=environment,
            capture_output=True, text=True, timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "reason": "Could not inspect the remote working folder."}
    if checked.returncode:
        return {"available": False, "reason": "Could not inspect the remote working folder."}
    try:
        snapshot = json.loads(checked.stdout)
    except json.JSONDecodeError:
        return {"available": False, "reason": "Remote workspace inspection returned an invalid response."}
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("available"), bool):
        return {"available": False, "reason": "Remote workspace inspection returned an invalid response."}
    if snapshot["available"]:
        if not re.fullmatch(r"[a-f0-9]{40,64}", str(snapshot.get("base_sha") or "")) or not re.fullmatch(r"[a-f0-9]{64}", str(snapshot.get("diff_hash") or "")):
            return {"available": False, "reason": "Remote workspace inspection returned an invalid Git snapshot."}
        snapshot["status"] = str(snapshot.get("status") or "")[-12000:]
        snapshot["changed_files"] = [
            str(path)[:500] for path in snapshot.get("changed_files", []) if isinstance(path, str)
        ][:200]
    else:
        snapshot["reason"] = str(snapshot.get("reason") or "Remote workspace inspection is unavailable.")[:500]
    return snapshot


def transfer_manifest(runner: Dict[str, Any], environment: Dict[str, str], worktree_path: str,
                      app_root: Path) -> Dict[str, Any]:
    """Bounded preflight for a later cross-runner copy; never reads credentials."""
    command = shlex.join(["python3", "-", worktree_path])
    try:
        checked = subprocess.run(["coder", "ssh", "--wait", "yes", runner["workspace_name"], "--", command],
                                 input=payload(app_root, "remote_transfer_manifest.py"), env=environment,
                                 capture_output=True, text=True, timeout=90)
        manifest = json.loads(checked.stdout) if not checked.returncode else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        manifest = None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("available"), bool):
        return {"available": False, "reason": "Could not prepare the remote transfer manifest."}
    return manifest


def run_agent(runner: Dict[str, Any], environment: Dict[str, str], command: str,
              output_file: Path, app_root: Path, result_marker: str) -> Tuple[Dict[str, Any], str]:
    """Run the bounded remote helper and retain its transcript locally for review."""
    process = subprocess.Popen(
        ["coder", "ssh", "--wait", "yes", runner["workspace_name"], "--", command],
        env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    assert process.stdin and process.stdout
    process.stdin.write(payload(app_root, "remote_agent_runner.py"))
    process.stdin.close()
    captured = []
    with output_file.open("w", encoding="utf-8") as handle:
        for line in process.stdout:
            captured.append(line)
            handle.write(line)
    code = process.wait()
    transcript = "".join(captured)
    marker_at = transcript.rfind(result_marker)
    if code or marker_at < 0:
        raise RuntimeError("The Coder runner did not return a complete agent result. Its task worktree was preserved.")
    raw = transcript[marker_at + len(result_marker):].strip().splitlines()[0]
    try:
        result = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("The Coder runner returned an unreadable agent result.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("The Coder runner returned an invalid agent result.")
    return result, transcript[:marker_at]


def _run_delivery_helper(runner: Dict[str, Any], environment: Dict[str, str], command: str,
                         app_root: Path, marker: str, action: str) -> Dict[str, Any]:
    process = subprocess.Popen(
        ["coder", "ssh", "--wait", "yes", runner["workspace_name"], "--", command],
        env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    assert process.stdin and process.stdout
    process.stdin.write(payload(app_root, "remote_delivery_runner.py"))
    process.stdin.close()
    transcript = process.stdout.read()
    code = process.wait()
    marker_at = transcript.rfind(marker)
    if code or marker_at < 0:
        raise RuntimeError("The Coder runner did not return a complete %s result. The remote worktree was preserved." % action)
    try:
        result = json.loads(transcript[marker_at + len(marker):].strip().splitlines()[0])
    except ValueError:
        raise RuntimeError("The Coder runner returned an unreadable %s result." % action) from None
    if not isinstance(result, dict) or result.get("error"):
        raise RuntimeError((result or {}).get("error") or "The Coder runner could not %s." % action)
    return result


def run_delivery(runner: Dict[str, Any], environment: Dict[str, str], command: str,
                 app_root: Path, marker: str) -> Dict[str, Any]:
    result = _run_delivery_helper(runner, environment, command, app_root, marker, "delivery")
    if not result.get("commit_sha") or not result.get("pr_url"):
        raise RuntimeError("The Coder runner returned an incomplete delivery result.")
    return result


def run_pr_status(runner: Dict[str, Any], environment: Dict[str, str], command: str,
                  app_root: Path, marker: str) -> Dict[str, Any]:
    result = _run_delivery_helper(runner, environment, command, app_root, marker, "pull-request status")
    if not result.get("url"):
        raise RuntimeError("The Coder runner could not read the pull-request status.")
    return result
