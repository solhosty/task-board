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
