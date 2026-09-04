# Harness Rotation — Coder handoff

## Purpose

Harness Rotation is a local task board that can run coding tasks either on the
desktop or in self-hosted Coder. Coder supplies the remote workspace; Harness
owns tasks, attempts, logs, verification, review gates, and eventual PR state.

The GitHub repository is `solhosty/alpha-01`, branch `main`.

## Current state

- Local dashboard: `http://127.0.0.1:63645/`
- Coder deployment: local `http://127.0.0.1:3000`
- Project profile: configured for the `agent-template` Coder template.
- Persistent private Coder runner: created and healthy.
- Each remote task gets an isolated Git worktree under the runner rather than a
  disposable workspace. Retry preserves the same worktree and base revision.
- The template has Codex CLI and Claude Code installed.
- GitHub repository access was configured through Coder's external-auth flow.
- The dashboard is restarted and serving the code at commit `96febf5`.

## What a Coder task does now

1. Harness selects the project's Coder target.
2. It reuses the persistent runner and creates or restores the task worktree.
3. It selects an enabled, authenticated remote CLI: Codex first, then Claude
   Code, unless a task specifies a supported preference.
4. The CLI runs only in that worktree. Credentials remain in the Coder runner;
   they are never copied into Harness records or logs.
5. The configured verification command runs remotely in the same worktree.
6. Harness records the transcript, verification output, and Git diff, then puts
   the task in **Needs review**.

Remote execution is intentionally review-first. It does **not** automatically
commit, merge, push, create a pull request, or execute a remote fallback yet.

## First live validation

Before starting a real remote task, connect at least one model in the runner:

1. Open the dashboard and select the project.
2. Open **Manage servers**, select the local Coder server, then **Models**.
3. Connect Codex or select **Connect Claude Code**. The dashboard opens Claude's
   native terminal sign-in in the persistent runner, displays its browser step,
   and accepts a one-time return code if Claude requests one.
4. Confirm the Models screen reports that CLI as authenticated.
5. Create a small, reversible task, select **Coder**, approve dispatch, and
   confirm it reaches **Needs review** with a log, verification result, and diff.

The earlier device-code sign-in cannot survive a dashboard restart because its
in-memory bridge is intentionally short-lived. Start a fresh connection from the
Models screen if Codex is not yet authenticated.

## Safety and boundaries

- Coder API tokens are kept in macOS Keychain, not SQLite or source control.
- Task prompts are base64-encoded across the SSH command boundary; the remote
  helper builds fixed Codex/Claude argument lists and does not evaluate task text
  as shell syntax.
- The remote helper limits an agent run to ten minutes and verification to ten
  minutes. The worktree remains available after any failure.
- Runner migration creates a new workspace and preserves the prior one; it does
  not silently discard task work or model logins.
- Current scheduler behavior remains serial even though runner capacity is stored.

## Next implementation slices

1. Prove one successful Codex task and one Claude Code task end-to-end.
2. Add explicit remote commit/push and PR creation, then synchronize GitHub PR
   status into the task board.
3. Add durable remote quota/failure handoff between supported authenticated CLIs.
4. Add controlled parallel task execution with one isolated worktree per task.
5. Add Tailscale access after remote state/recovery is proven.

## Verification and source locations

- Latest pushed commit: `96febf5` — `Dispatch Coder tasks through persistent runners`
- Automated tests: `PYTHONPYCACHEPREFIX=/private/tmp/harness-pyc python3 -m unittest discover -s tests -p 'test_*.py'`
- Latest result: 60 tests passing.
- Remote transport was checked live against the configured runner with an
  intentionally invalid request; it started no agent and modified no checkout.

Relevant implementation files:

- `app.py` — dashboard API, runner lifecycle, dispatch, state transitions.
- `remote_worktree.py` — isolated remote Git checkout creation.
- `remote_agent_runner.py` — bounded in-runner Codex/Claude execution.
- `CODER_PHASE_1.md` — detailed architecture and decisions.
