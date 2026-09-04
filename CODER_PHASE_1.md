# Coder Community Phase 1: Remote, recoverable fallback

## Decision

Use self-hosted Coder Community as the **execution substrate** for Harness Rotation.
Harness Rotation remains the task control plane: it owns task state, attempt history,
harness selection, fallback, verification, review gates, and recovery decisions.

This is deliberately not a migration to Coder Agents or a dependency on a paid Coder
plan. Coder provisions and connects to an isolated remote workspace. The existing
headless Codex, Claude Code, Droid, and OpenCode CLIs continue to be launched by the
harness inside that workspace.

## Product outcome

A task can run in an isolated Coder workspace and survive a quota handoff without
losing its working files, task context, attempt history, or verification evidence.
The result reaches **Needs review** only after the project verification command
passes. No task merges, deploys, or publishes automatically.

## Generated-template model

Coder templates are generated from a **reviewed Harness blueprint**, not handwritten
per task and not from arbitrary repository code. A blueprint fixes the security and
infrastructure boundary: provisioner type, base image, persistent storage, Coder
agent, allowed resource presets, checkout convention, and credentials policy.

Each registered Coder server advertises its own capability record. The dispatcher
uses that record to compile a versioned template for a project on that server:

```text
server capability record + approved blueprint + project Coder profile
  → harness-<server>-<project> template version
  → task workspace / execution lease
```

A project Coder profile supplies the repository source, base branch, setup profile
(`auto`, `python`, or `node` initially), and its preferred execution target. A task
can inherit that preference or explicitly choose **Local** or **Coder**. Local is the
default, and an existing local folder is never uploaded or moved automatically.

The first registration of a Coder server is intentionally explicit: the user selects
or discovers its URL, supplies an API token, and verifies access. The token belongs
in the operating-system keychain, never in SQLite, source control, a template, or a
task record. Localhost discovery may suggest `127.0.0.1:3000`; broad subnet scanning
is not part of the default flow. A Tailscale/MagicDNS address can be registered
directly later.

## Responsibilities

| Component | Owns |
| --- | --- |
| Harness Rotation | Task contract, ordered fallback chain, run state, attempt record, prompt handoff, verification, review state, checkpoints, and recovery policy. |
| Coder Community | Self-hosted workspace lifecycle, Terraform template, remote agent connection, network boundary, and remote command transport. |
| Workspace template | Repository checkout convention, runtime dependencies, installed headless CLIs, allowed outbound network, and task working-directory layout. |
| Git | Base revision, checkpoint commits or patch artifacts, diff, and eventual review/PR handoff. |

## Phase 1 run contract

Every remote run receives an immutable execution lease:

```text
task ID
run ID
Coder workspace ID + workspace name
template version / template revision
repository identity + base SHA
task worktree path
fallback-chain snapshot
latest checkpoint reference
```

The execution lease is created before the first harness starts and remains attached
to that task run. A quota handoff creates another **attempt**, not another task or
workspace. The next harness starts in the same task worktree and receives the
existing conversation plus the prior attempt's visible progress, verification output,
and checkpoint reference.

### Lifecycle

```text
Queued
  → Provisioning workspace
  → Restoring checkpoint (if any)
  → Running attempt
       → quota: checkpoint → next eligible harness → Running attempt
       → failed: checkpoint → Stopped
       → success: verify
  → Needs review
  → Done / Closed
```

`Needs input` remains a separate state for a missing decision or requirement. It is
not reported as an active agent run.

## Checkpoint and recovery rules

1. Before a harness handoff, the harness writes its stream log and records the
   current verification output.
2. The dispatcher creates a durable checkpoint. For a Git workspace this is a
   task-owned commit on the task branch; if a commit is unavailable, it stores an
   explicit patch artifact and the base SHA.
3. The next harness resumes in the same workspace and task worktree.
4. If that workspace is unavailable, the dispatcher provisions a replacement from
   the recorded template, checks out the base SHA, restores the checkpoint, and only
   then starts the next attempt.
5. A checkpoint never overwrites the user's base branch or merges task work.

This is stronger than the current local-only behavior, where files are preserved but
a harness-server restart marks an active attempt interrupted.

## Free-plan implementation approach

The dispatcher uses Coder Community's normal workspace management and remote-command
transport. It does not require Coder's paid native background-agent API.

1. Ensure a task workspace exists from an approved Coder template.
2. Wait until its workspace agent is healthy.
3. Start the configured headless harness with Coder's remote command mechanism.
4. Stream normalized output to the existing attempt log and task conversation.
5. Apply the existing quota/cooldown and verification rules.
6. Stop or retain the workspace according to the task outcome and recovery window.

The working directory must be an actual persistent workspace volume for the task's
whole run. A fresh disposable clone per harness attempt would break the present
same-folder handoff behavior.

## Required Coder configuration before live dispatch

These values must be intentionally supplied; none are stored in source control.

- Coder deployment URL.
- A least-privilege Coder token for the dispatcher.
- Coder organization and a working provisioner/provider (Docker locally first).
- An approved Harness blueprint from which per-project template versions are
  generated for that specific server.
- Template parameters for repository identity, task/run identifier, setup profile,
  and allowed resource preset.
- Workspace startup contract: checkout location, persistent task volume, Git identity,
  dependencies, and installed headless harness CLIs.
- Credential strategy for each model provider. The initial design must not bake
  long-lived provider keys into source or task records.
- Workspace retention/autostop and recovery period.

GitHub access is per Coder user. Before provisioning a private-repository workspace,
Harness checks Coder's GitHub external-auth state. If authorization is missing, the
task stays active as `awaiting_external_auth`; the dashboard presents **Connect
GitHub** and **I've connected** actions, then resumes that same run after Coder
confirms the connection. Provider tokens are never returned to the browser or stored
by Harness.

This removes terminal setup from the normal flow, but it does not silently grant
repository access. Each new Coder user approves GitHub once under their own account.
A future multi-user Harness deployment must scope Coder API tokens and task ownership
per Harness user instead of sharing one dispatcher account.

## Minimum acceptance tests

Phase 1 is complete only when all of the following are demonstrated against a
disposable repository and Coder workspace:

1. The board launches a task into Coder rather than a local folder.
2. The task view exposes the workspace, active harness/model, attempts, and current
   lifecycle state.
3. A deterministic quota result causes a second harness to continue in the same
   task worktree with the prior file edits present.
4. Killing the remote workspace after a checkpoint results in a replacement workspace
   that restores the work and can continue the task.
5. The configured verification command runs remotely and must pass before the run
   enters review.
6. The user can inspect the diff and explicitly finish or close the run; no automatic
   merge or deployment occurs.

## Pull-request delivery state

A pull request is a task-level delivery artifact, not an attempt-level artifact.
Attempts may create or update the same branch, while the task retains the stable link
to its PR and the delivery decision it represents.

The task board should display a linked pull request with these derived states:

```text
Not linked → Draft → Open / review pending → Approved | Changes requested → Merged | Closed
```

For the first slice, a user links a GitHub pull-request URL to a task. The harness
uses the authenticated `gh` CLI in the project repository to read its state, review
decision, branch, head SHA, and merge time. Sync is read-only: the harness does not
create, merge, close, or approve a PR. A missing GitHub CLI or authorization failure
is shown as a sync error while preserving the task's existing PR link.

When the Coder backend is active, this same task-level PR record will be updated from
the remote task workspace. The board therefore keeps the same PR state whether work
ran locally, in a Coder workspace, or across several fallback attempts.

## Non-goals for Phase 1

- Coder Agents or Coder AI Gateway integration.
- Automatic model scoring or silent mid-attempt model switches.
- Multiple concurrent attempts editing the same task worktree.
- Tailscale/mobile access. That is Phase 2 after remote state and recovery are proven.
- Public internet exposure of the harness or workspaces.

## Next implementation slice

The registry and provisioning slices now record Coder servers, verify credentials,
publish the Docker blueprint, pass repository parameters, provision a workspace,
validate its remote Git HEAD, and bind it to the execution lease. The next slice
installs and authenticates approved headless harness CLIs, streams their remote
process output, and applies checkpoint/fallback behavior. Existing local and
Git-worktree modes retain their current behavior.
