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

A task can run in its own worktree on a persistent private Coder runner and survive a quota handoff without
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
  → persistent runner (created once) → task worktree / execution lease
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

| Component          | Owns                                                                                                                                            |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Harness Rotation   | Task contract, ordered fallback chain, run state, attempt record, prompt handoff, verification, review state, checkpoints, and recovery policy. |
| Coder Community    | Self-hosted workspace lifecycle, Terraform template, remote agent connection, network boundary, and remote command transport.                   |
| Workspace template | Repository checkout convention, runtime dependencies, installed headless CLIs, allowed outbound network, and task working-directory layout.     |
| Git                | Base revision, checkpoint commits or patch artifacts, diff, and eventual review/PR handoff.                                                     |

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
  → Finding/starting persistent runner
  → Preparing or reusing task worktree
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
4. If the runner is stopped, reconnect to that same workspace. If its storage is
   missing, stop for explicit recovery: never silently replace it and lose model
   logins or task state. Replacement plus checkpoint restoration remains a future
   recovery capability and may require signing in again.
5. A checkpoint never overwrites the user's base branch or merges task work.

This is stronger than the current local-only behavior, where files are preserved but
a harness-server restart marks an active attempt interrupted.

## Free-plan implementation approach

The dispatcher uses Coder Community's normal workspace management and remote-command
transport. It does not require Coder's paid native background-agent API.

1. Ensure the user's private persistent runner exists from an approved Coder template.
2. Wait until its workspace agent is healthy.
3. Start the configured headless harness with Coder's remote command mechanism.
4. Stream normalized output to the existing attempt log and task conversation.
5. Apply the existing quota/cooldown and verification rules.
6. Retain the runner's storage and task worktree after the task ends. Do not stop a
   runner merely because one of its tasks finishes; other tasks may still use it.

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

External-service access is global to a user on one Coder server, rather than belonging
to an individual project. Harness discovers the provider IDs advertised by that
server and presents them under **Connected accounts**. Each project records the
provider ID required for its repository. Before provisioning, Harness checks that
connection; if authorization is missing, the task stays active as
`awaiting_external_auth` and resumes the same run after Coder confirms it. Provider
tokens are never returned to the browser or stored by Harness.

This removes terminal setup from the normal flow, but it does not silently grant
repository access. Each new Coder user approves the required provider once under
their own account.
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
4. Restarting the same runner preserves worktrees and native CLI authentication.
   Missing storage results in an explicit recovery state, not a silent replacement.
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

### In-app model authentication requirement

Users must initiate model connections in Harness, not copy a code from an assistant
message or run a terminal command. Repository authorization remains separate from
model authorization. Do not restart the manual remote-login workflow.

Verified on 2026-09-04:

- Codex 0.149.0's generated protocol schema supports
  `account/login/start` with `type: chatgptDeviceCode` and returns `loginId`,
  `verificationUrl`, and `userCode`.
- A live stdio connection through Coder SSH to the installed remote app-server
  successfully completed `initialize` and `account/read`. It returned no account;
  this probe did not start login, inspect credentials, or invoke a model.
- The documented integration lets the frontend display the provider URL/code and
  observe `account/login/completed`, with cancellation and logout operations.
  Use that protocol instead of scraping CLI output or implementing private OAuth
  endpoints. Successful login must be followed by an account read before the UI
  reports connected. Keep credentials and raw protocol logs out of browser payloads.

The implementation should expose Connect, Cancel, Reconnect, and Disconnect within
Connected accounts, scoped to the user and Coder server. OpenAI still hosts consent;
Harness must never collect the user's OpenAI password. Keep the protocol transport
behind authenticated Coder SSH, not a publicly exposed app-server socket.

The accepted MVP uses a persistent per-user runner with separate task worktrees,
keeping provider-managed credentials in one home. The runner measures its own
CPU quota/CPU set and cgroup memory, then admits the maximum safe number of Coder
tasks (with 2 GiB reserved per agent); this is displayed read-only in Harness,
not configured by the user. Local project-folder tasks remain serialized. Worktrees do not isolate secrets,
ports, processes, or the OS user. Do not copy auth caches into disposable task
containers or share a credential volume across users.

Claude Code support is a required part of this design. Its unmodified native binary
must own sign-in and credentials; do not implement a custom Claude subscription
OAuth exchange or token broker. Anthropic's hosting guidance permits users signing
into hosted, unmodified Claude Code subject to its conditions, but does not establish
a Codex-style structured frontend authentication protocol. In-app access to the
native flow, status checks, expiry and reconnect still need implementation and
end-to-end testing. Do not silently switch subscription users to API billing.

Codex support does not establish equivalent subscription-login support for other
harnesses. Each adapter needs a supported connection mechanism before it can appear
as an available remote fallback. Neither in-app model login nor credential reuse
across task workspaces is implemented by the current GitHub connection UI.

Sources: [Codex app-server authentication](https://learn.chatgpt.com/docs/app-server#authentication),
[Codex credential storage and headless login](https://learn.chatgpt.com/docs/auth),
[Claude Code authentication](https://code.claude.com/docs/en/authentication),
[Claude Code hosting and credential conditions](https://code.claude.com/docs/en/legal-and-compliance).

### Live validation, 2026-09-04

Persistent-runner preparation was validated against the existing
`harness-clean-checkout-0904` workspace using `tests/live_persistent_runner.py` and
a temporary local database. Two tasks used the same workspace ID, had different
worktrees, and retained an uncommitted diagnostic file after repeated preparation.
No workspace was created and no model login or inference was invoked. The two
diagnostic worktrees remain in that test workspace. The dashboard's Runner panel
successfully lists the registered account's private workspaces; no production
runner selection was made by this test.

The suite includes runner account/organization boundaries, stable binding, capacity
admission, database reinitialization, concurrent checkout setup, setup interruption,
missing-worktree detection, and preservation of dirty files and base revisions.
Concurrent checkout setup is not a test of concurrent model execution or token
refresh. The scheduler and model connection/dispatch work remain incomplete.

The registered Coder account now reports GitHub authorized with one app installation.
From `harness-auth-smoke-0904`, a read-only `git ls-remote` and a private clone of
`solhosty/alpha-01` both succeeded. The earlier 403 is resolved. The smoke workspace
contains Node and Python but no Codex, Claude, Droid, or OpenCode CLI. This validates
repository read access, not push permissions or remote agent execution.

A second, fresh workspace (`harness-clean-checkout-0904`) completed the entire
startup script and private checkout without a manual clone. Its checkout was
`dc3950c8d7e414946c437f06512dd6634064163f`. Codex CLI 0.149.0 was then installed
in that disposable workspace under `/home/coder/.local`; `codex login status`
reported not logged in. Device authorization requires the user's consent. No local
model credentials were copied and no API billing credentials were configured.
This installation is a smoke-test preparation, not yet part of every template.

Blocked task connection buttons now use a structured server/provider action from
the project profile and the same Harness-owned connection flow as settings. They
no longer extract a Coder browser login URL from a run message. The template also
passes repository parameters as environment values rather than interpolating
them into shell source. This template hardening still needs a new published
template version before it applies to newly provisioned workspaces.

Harness now owns the device-code exchange using the registered Coder token, instead
of relying on a separate Coder browser session. Tests cover pending, slow-down,
denial, expiry, and duplicate connection requests. The UI distinguishes authorization
from installation and labels an existing installation as Manage repository access.

The registry and provisioning slices now record Coder servers, verify credentials,
select a reviewed template per project, provision or migrate a private persistent
runner, create a task-owned remote Git worktree, and bind it to the execution lease.
Existing local and Git-worktree modes retain their current behavior.

### Model connection slice, 2026-09-04

Harness now exposes **Models** for a Coder server. It resolves the private
persistent runner and reports only public installation/authentication state. For
Codex, the dashboard uses a private Coder-SSH stdio bridge to the official
`codex app-server` device-code protocol: the dashboard can display the OpenAI
verification URL and code, while Codex itself persists and refreshes its managed
login in the runner home. Harness does not receive, log, or persist a ChatGPT
access token.

Claude Code is surfaced in the same screen and is intentionally not represented as
a custom OAuth provider. The dashboard opens Claude's native interactive login in a
short-lived PTY bridge to the runner, displays its browser step, and can forward a
one-time return code. The bridge keeps terminal text only in memory and never reads,
stores, or copies the credential Claude saves in the runner. This gives the user one
durable login per runner rather than one per task.

The runner template now installs Codex and Claude Code. A remote run chooses an
enabled, authenticated native CLI in that runner, invokes it only in the task's
recorded worktree, retains the transcript locally, runs the configured verification
command in the same worktree, and returns a remote Git diff to the board. The remote
helper has a ten-minute bound for the agent and another ten-minute bound for
verification. It accepts a base64-encoded request and builds the fixed native CLI
argument list remotely, so task text never becomes shell syntax.

This is intentionally review-first: every successful remote task enters **Needs
review**. It does not automatically merge or publish a task. After the user
explicitly selects **Commit, push & create PR**, the runner commits and pushes its
isolated task branch and creates or reuses a GitHub PR through Coder external auth.
The short-lived credential remains in Coder; Harness receives only the resulting
PR metadata. On an unattended recognized quota failure, Harness preserves the same
worktree and hands the task to the other authenticated runner CLI. Supervised runs
wait for an explicit resume instead.
