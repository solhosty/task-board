"""Commit, push, and open a GitHub PR from a verified Coder task worktree.

This helper runs only in the persistent runner. It obtains a short-lived GitHub
token from Coder external auth and never writes that token to stdout or disk.
"""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

MARKER = "__HARNESS_REMOTE_DELIVERY_RESULT__"
ROOT = Path("/home/coder/.harness-runner/tasks").resolve()


def run(command, cwd, check=True, env=None):
    result = subprocess.run(command, cwd=str(cwd), text=True, capture_output=True, timeout=120, env=env)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")
        operation = " ".join(command[1:3]) or "operation"
        raise RuntimeError("Remote Git " + operation + " failed" + (": " + detail[:500] if detail else "."))
    return result


def github_repository(source):
    parsed = urlparse(source)
    host, path = parsed.hostname, parsed.path
    scp = re.fullmatch(r"[^@]+@([^:]+):(.+)", source)
    if scp:
        host, path = scp.group(1), "/" + scp.group(2)
    if (host or "").lower() not in ("github.com", "www.github.com"):
        raise ValueError("Remote PR creation supports github.com repository URLs only.")
    parts = path.strip("/").removesuffix(".git").split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise ValueError("Could not determine the GitHub repository from the configured source URL.")
    return "/".join(parts)


def github_token(provider):
    result = subprocess.run(["coder", "external-auth", "access-token", provider], text=True,
                            capture_output=True, timeout=45)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Coder did not provide a usable GitHub repository credential. Connect the external-auth provider and install its GitHub App with repository access, then try again.")
    return result.stdout.strip()


def github_pr_url(url):
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if parsed.scheme != "https" or parsed.hostname not in ("github.com", "www.github.com") or len(parts) != 4 or parts[2] != "pull" or not parts[3].isdigit():
        raise ValueError("Invalid GitHub pull-request URL.")
    return "/".join(parts[:2]), parts[3]


def github_identity(token):
    account = github_json("GET", "https://api.github.com/user", token)
    account_id, login = str(account.get("id") or ""), str(account.get("login") or "")
    if not account_id.isdigit() or not re.fullmatch(r"[A-Za-z0-9-]+", login):
        raise RuntimeError("GitHub did not provide a usable commit identity.")
    return account.get("name") or login, account_id + "+" + login + "@users.noreply.github.com"


def commit_environment(name, email):
    environment = os.environ.copy()
    environment.update({"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
    return environment


def github_json(method, url, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json", "Authorization": "Bearer " + token,
        "User-Agent": "Harness-Rotation", "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read())
    except (HTTPError, URLError, ValueError):
        raise RuntimeError("GitHub could not create or read the pull request. Check Coder GitHub authorization and repository permissions.") from None


def main(encoded):
    request = json.loads(base64.urlsafe_b64decode(encoded.encode()).decode())
    if request.get("action") == "status":
        repository, number = github_pr_url(str(request["pr_url"]))
        pull = github_json("GET", "https://api.github.com/repos/" + repository + "/pulls/" + number,
                           github_token(str(request["auth_provider_id"])))
        print(MARKER + json.dumps({"url": pull.get("html_url"), "number": str(pull.get("number") or "") or None,
                                   "branch_name": pull.get("head", {}).get("ref"), "head_sha": pull.get("head", {}).get("sha"),
                                   "state": pull.get("state"), "merged_at": pull.get("merged_at")}, separators=(",", ":")))
        return
    worktree = Path(request["worktree_path"]).resolve()
    if ROOT not in worktree.parents or not worktree.is_dir():
        raise ValueError("Invalid task worktree.")
    branch = str(request["branch_name"])
    if not re.fullmatch(r"harness/task-[a-f0-9-]+", branch):
        raise ValueError("Invalid task branch.")
    source = str(request["repo_url"])
    repository = github_repository(source)
    base = str(request["base_ref"])
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", base):
        raise ValueError("Invalid base branch.")
    token = github_token(str(request["auth_provider_id"]))
    commit_name, commit_email = github_identity(token)
    identity_environment = commit_environment(commit_name, commit_email)
    title = str(request["title"]).strip()[:120] or "Harness task"
    run(["git", "add", "-A"], worktree)
    staged = run(["git", "diff", "--cached", "--quiet"], worktree, check=False)
    if staged.returncode == 0:
        head_sha = run(["git", "rev-parse", "HEAD"], worktree).stdout.strip()
        if head_sha == request["base_sha"]:
            raise RuntimeError("The reviewed remote worktree has no changes to commit.")
        existing_emails = run(["git", "show", "-s", "--format=%ae%n%ce", "HEAD"], worktree).stdout.splitlines()
        if any(email != commit_email for email in existing_emails[:2]):
            run(["git", "commit", "--amend", "--no-edit", "--reset-author"], worktree, env=identity_environment)
            head_sha = run(["git", "rev-parse", "HEAD"], worktree).stdout.strip()
    elif staged.returncode == 1:
        run(["git", "commit", "-m", "harness: " + title[:68]], worktree, env=identity_environment)
        head_sha = run(["git", "rev-parse", "HEAD"], worktree).stdout.strip()
    else:
        raise RuntimeError("The remote worktree could not be inspected before delivery.")
    # Another delivery attempt can have updated this task's remote branch while
    # this preserved worktree was waiting for review.  Reconcile that branch
    # here instead of reporting a generic non-fast-forward push failure.
    # A semantic conflict remains in this isolated worktree for task recovery.
    fetch = run(["git", "fetch", "origin", branch], worktree, check=False)
    remote_branch = "origin/" + branch
    if fetch.returncode == 0 and run(["git", "rev-parse", "--verify", remote_branch], worktree, check=False).returncode == 0:
        ancestor = run(["git", "merge-base", "--is-ancestor", remote_branch, "HEAD"], worktree, check=False)
        if ancestor.returncode != 0:
            run(["git", "rebase", remote_branch], worktree)
            head_sha = run(["git", "rev-parse", "HEAD"], worktree).stdout.strip()
    # Coder injects the workspace's fresh external-auth credential through
    # GIT_ASKPASS. Do not replace it with a cached or desktop credential.
    # A delivery retry may have amended the task's previous commit (for
    # example, after the harness resolves a rebase conflict). This is the
    # task's private branch, so update it only if the remote still matches the
    # ref we fetched; never overwrite a branch that changed underneath us.
    run(["git", "push", "--force-with-lease", "--set-upstream", "origin", branch], worktree)
    head = repository.split("/", 1)[0] + ":" + branch
    query = urlencode({"state": "open", "head": head, "base": base, "per_page": "1"})
    existing = github_json("GET", "https://api.github.com/repos/" + repository + "/pulls?" + query, token)
    body = str(request.get("body") or "Created by Harness Rotation after remote verification.")[:4000]
    pull = existing[0] if isinstance(existing, list) and existing else github_json(
        "POST", "https://api.github.com/repos/" + repository + "/pulls", token,
        {"title": title, "head": branch, "base": base, "body": body})
    diff = run(["git", "diff", request["base_sha"], "HEAD", "--"], worktree).stdout
    stat = run(["git", "diff", "--stat", request["base_sha"], "HEAD"], worktree).stdout.strip()
    print(MARKER + json.dumps({"commit_sha": head_sha, "head_sha": head_sha, "branch_name": branch,
                               "diff": diff[-250000:], "diff_stat": stat, "pr_url": pull.get("html_url"),
                               "pr_number": str(pull.get("number") or "") or None}, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except Exception as exc:
        print(MARKER + json.dumps({"error": str(exc)}, separators=(",", ":")))
