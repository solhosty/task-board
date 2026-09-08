"""Local Git worktree and harness-process operations."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


Git = Callable[..., subprocess.CompletedProcess]


def stream_process(command: List[str], cwd: Path, output_file: Path,
                   children: Set[subprocess.Popen], timeout_seconds: int = 600) -> Tuple[int, str]:
    captured: List[str] = []
    with output_file.open("w", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(command) + "\n\n")
        handle.flush()
        process = subprocess.Popen(command, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
        children.add(process)

        def timed_out() -> None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)

        timer = threading.Timer(timeout_seconds, timed_out)
        timer.daemon = True
        timer.start()
        assert process.stdout
        with process.stdout:
            for line in process.stdout:
                captured.append(line)
                handle.write(line)
                handle.flush()
        returncode = process.wait()
        timer.cancel()
        children.discard(process)
        if returncode == -signal.SIGTERM:
            captured.append("\nHarness exceeded the 10-minute execution timeout. Files were preserved. Retry or split this task into a smaller step.\n")
    return returncode, "".join(captured)


def make_worktree(project: Dict[str, Any], run_id: str, worktree_root: Path, git: Git) -> Tuple[Path, str, str]:
    repo = Path(project["repo_path"])
    base = git(["rev-parse", "HEAD"], repo).stdout.strip()
    branch = f"harness/{run_id[:8]}"
    destination = worktree_root / f"project-{project['id']}" / run_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = git(["worktree", "add", "-b", branch, str(destination), base], repo, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Could not create Git worktree")
    return destination, branch, base


def worktree_diff(attempt: Dict[str, Any], git: Git) -> str:
    root = Path(attempt['worktree_path'])
    if not root.exists():
        return attempt.get('diff_output') or 'Worktree is no longer available.'
    if not attempt['base_sha']:
        return 'This folder has no Git baseline. Changes are in the project folder; no commit was created.'
    diff = git(['diff', attempt['base_sha'], '--'], root, check=False).stdout
    untracked = git(['ls-files', '--others', '--exclude-standard', '-z'], root).stdout.split('\0')
    for path in filter(None, untracked):
        diff += git(['diff', '--no-index', '--', '/dev/null', path], root, check=False).stdout
    return diff


def changed_files(attempt: Dict[str, Any], git: Git) -> List[Dict[str, Any]]:
    """Every file this attempt added or changed in its worktree.

    This is the diff expressed as files rather than as text, so the dashboard
    can show what a harness produced -- including the binary results a textual
    diff can only describe as "binary files differ".
    """
    root = Path(attempt.get("worktree_path") or "")
    if not root.is_dir() or not attempt.get("base_sha"):
        return []
    listed: List[Dict[str, Any]] = []
    status = git(["diff", "--name-status", "-z", attempt["base_sha"], "--"], root, check=False).stdout
    fields = [field for field in status.split("\0") if field]
    index = 0
    while index < len(fields):
        code = fields[index]
        # A rename carries both names; the new path is the one on disk now.
        step = 3 if code[:1] in ("R", "C") else 2
        path = fields[index + step - 1] if index + step - 1 < len(fields) else None
        if path:
            listed.append({"path": path, "status": _CHANGE_NAMES.get(code[:1], "changed")})
        index += step
    for path in filter(None, git(["ls-files", "--others", "--exclude-standard", "-z"], root).stdout.split("\0")):
        listed.append({"path": path, "status": "added"})
    for item in listed:
        file = root / item["path"]
        item["exists"] = file.is_file()
        item["byte_size"] = file.stat().st_size if item["exists"] else 0
    return sorted(listed, key=lambda item: item["path"])


_CHANGE_NAMES = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied", "T": "changed"}


def resolve_within(root: Path, relative: str) -> Path:
    """One file inside a worktree, or a refusal.

    Attempt paths come back to us over HTTP, and a worktree can hold symlinks a
    harness created, so resolve both sides before comparing them.
    """
    base = root.resolve()
    target = (base / relative).resolve()
    if not target.is_file() or base not in target.parents:
        raise ValueError("That file is not part of this attempt's workspace.")
    return target


def cleanup_worktree(repo: Path, worktree: Path, git: Git) -> None:
    if repo.resolve() != worktree.resolve():
        git(["worktree", "remove", "--force", str(worktree)], repo, check=False)


def mark_task_complete(repo: Path, task: Dict[str, Any], git: Git) -> None:
    if task['source_line'] is None:
        return
    file = repo / "TASKS.md"
    content = file.read_text(encoding="utf-8").splitlines(keepends=True)
    preferred_line = (task["source_line"] or 1) - 1
    locations = [preferred_line] + [i for i in range(len(content)) if i != preferred_line]
    for index in locations:
        if 0 <= index < len(content) and re.match(r"^\s*[-*]\s+\[ \]\s+" + re.escape(task["text"]) + r"\s*$", content[index].rstrip("\n")):
            content[index] = re.sub(r"(\[) (\])", r"\1x\2", content[index], count=1)
            file.write_text("".join(content), encoding="utf-8")
            git(["add", "TASKS.md"], repo)
            result = git(["commit", "-m", "chore: complete task"], repo, check=False)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "Could not commit TASKS.md")
            return
    raise RuntimeError("Merged work, but could not locate the unchecked task in TASKS.md")


def commit_and_merge(project: Dict[str, Any], task: Dict[str, Any], attempt: Dict[str, Any], git: Git) -> Tuple[Optional[str], str, str]:
    repo, worktree = Path(project["repo_path"]), Path(attempt["worktree_path"])
    if git(['status', '--porcelain'], repo).stdout.strip() or git(['rev-parse', 'HEAD'], repo).stdout.strip() != attempt['base_sha']:
        raise RuntimeError('The main project changed during execution. Verified work is preserved in the worktree for review.')
    git(["add", "-A"], worktree)
    status = git(["status", "--porcelain"], worktree).stdout.strip()
    if status:
        result = git(["commit", "-m", "harness: " + task["text"][:68]], worktree, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Could not commit worktree changes")
    head = git(["rev-parse", "HEAD"], worktree).stdout.strip()
    merged = git(["merge", "--ff-only", attempt["branch_name"]], repo, check=False)
    if merged.returncode:
        raise RuntimeError("Main repository changed during this run; worktree kept. " + merged.stderr.strip())
    mark_task_complete(repo, task, git)
    return head, git(["diff", "--stat", attempt["base_sha"], "HEAD"], repo).stdout.strip(), git(["diff", attempt["base_sha"], "HEAD", "--"], repo).stdout


def rebase_onto_current_project(project: Dict[str, Any], attempt: Dict[str, Any], git: Git) -> Tuple[bool, str, str]:
    """Rebase an isolated task branch onto the project's current checked-out commit.

    This intentionally operates only in the task worktree.  A conflict is left
    in place for a harness (or person) to resolve; the project checkout and the
    task's original branch are never reset or overwritten.
    """
    repo, worktree = Path(project["repo_path"]), Path(attempt["worktree_path"])
    if repo.resolve() == worktree.resolve():
        raise RuntimeError('Local project-folder tasks do not have an isolated branch to update.')
    target = git(["rev-parse", "HEAD"], repo).stdout.strip()
    if not target or target == attempt.get("base_sha"):
        return True, target, ''
    result = git(["rebase", "--onto", target, str(attempt["base_sha"])], worktree, check=False)
    detail = (result.stderr or result.stdout).strip()
    return result.returncode == 0, target, detail[-4000:]
