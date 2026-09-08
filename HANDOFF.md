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
- The template has Codex CLI and Claude Code installed. The active
  `agent-template` version published September 4 installs Codex with its
  official standalone installer; Harness invokes its canonical package path so
  the matching `codex-code-mode-host` is available.
- GitHub repository access was configured through Coder's external-auth flow.
- The dashboard process is user-owned. Do not start, stop, or claim a port for
  it; the user normally serves this checkout at `http://127.0.0.1:4173/`.
- Live validation on September 4: both Claude Code and Codex completed isolated
  one-file tasks in the persistent runner, returned their diffs, passed `git
diff --check`, and stopped at **Needs review**. Harness also recognizes
  structured Codex item errors as failures even if the CLI exits zero.

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
commit, merge, push, or create a pull request. After a user explicitly chooses
**Commit, push & create PR** on a verified remote task, the runner commits the
task branch, pushes it, and creates or reuses a GitHub PR using Coder's
short-lived external-auth token; the token never reaches Harness. On a
recognized remote quota response, an unattended task cools down the limiting
CLI and continues in the same preserved worktree on the other authenticated
remote CLI. Supervised tasks pause for an explicit resume decision instead. PR
status sync for remote tasks also executes in that runner, rather than relying
on a desktop GitHub CLI login. If delivery fails after the remote commit, the
board offers **Retry delivery**; it reuses that exact task commit and does not
rerun the agent.

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
- Coder tasks run concurrently only up to the runner's detected CPU and memory capacity; each
  retains its own worktree. Excess unattended remote tasks wait in a durable FIFO queue and
  start automatically when a slot is released. Local project-folder runs remain serialized.

## Next implementation slices

1. Live-validate remote commit/push/PR creation — complete. PR #1 was created
   from the preserved one-file validation task. The final commit is
   `b65ed922b4be2f03e6ceace0519c02c826cc3a58` on its isolated task branch.
   Delivery uses Coder `GIT_ASKPASS` and the GitHub ID-based `noreply` email.
2. Add controlled parallel task execution — complete. Coder tasks are admitted
   up to their detected CPU/cgroup-memory capacity (2 GiB reserved per agent), while local tasks stay serialized.
3. Live validation complete: three remote tasks ran concurrently at the detected capacity of 3;
   each preserved its worktree and fell back from quota-limited Codex to Claude Code. A fourth
   task was held at capacity and is now recovered into the durable FIFO queue on restart.
4. Add Tailscale access after parallel runner behavior is proven.

## Verification and source locations

- Latest pushed commit: `96febf5` — `Dispatch Coder tasks through persistent runners`
- Automated tests: `PYTHONPYCACHEPREFIX=/private/tmp/harness-pyc python3 -m unittest discover -s tests -p 'test_*.py'`
- Latest result: 65 Python tests and 7 browser-format checks passing,
  including remote commit/push/PR state and PR sync, quota handoff state, and
  alternate-authenticated-CLI selection.
- Remote transport was checked live against the configured runner with an
  intentionally invalid request; it started no agent and modified no checkout.

Relevant implementation files:

- `app.py` — dashboard API, runner lifecycle, dispatch, state transitions.
- `infra/runner/remote_worktree.py` — isolated remote Git checkout creation.
- `infra/runner/remote_agent_runner.py` — bounded in-runner Codex/Claude execution.
- `CODER_PHASE_1.md` — detailed architecture and decisions.
