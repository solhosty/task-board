# Harness Rotation

## Run the redesigned Aludra UI

For the production-style dashboard, build the frontend once and start the Python application:

```bash
cd frontend && npm run build
cd .. && python3 app.py
```

Open the printed `http://127.0.0.1:4173` URL. The React application is served at `/`.

For frontend hot reload, leave the Python server running and use a second terminal:

```bash
cd frontend && npm run dev
```

Vite proxies API requests to the Python server on port `4173`.

A local workspace for goals that can continue across coding harnesses.

A project is a named folder containing local code. Each project has many tasks. A task is a persistent goal—a feature, fix, or entire app—with its conversation and harness attempts kept together. See [DOMAIN.md](DOMAIN.md).

The planned self-hosted remote execution path is specified in [CODER_PHASE_1.md](CODER_PHASE_1.md). It keeps Coder Community as the workspace layer while Harness Rotation retains headless fallback, verification, and review control.

Projects open on a task board. A card belongs to Planned, Running, Needs review, or Done and shows whether its effective execution target is local or the project's Coder environment. Local remains the default. The Coder settings can discover/register localhost Coder, store an API token in macOS Keychain, and create a server-specific project profile.

Coder-targeted work reuses a persistent private runner for the authenticated Coder user and creates a separate task worktree with a recorded base SHA. Retries preserve the same task's uncommitted files. Under **Manage servers → Runner**, select an existing private workspace and configure its admission limit; otherwise the first remote task creates a runner from the selected project template. Native CLI credentials stay in the runner's persistent home, never copied from the desktop or into task records. Private-repository authorization remains under per-user **Connected accounts**, where identity authorization and GitHub App installation are distinct. For HTTPS Git operations, Coder injects the workspace’s external-auth credential through `GIT_ASKPASS`; Harness does not replace it with a desktop or cached credential. Codex and Claude Code can now run remotely, return their transcript, verification output, and diff to the board, then always stop at **Needs review**. A user may explicitly commit, push, and create a PR from that review state; remote quota fallback is also supported. Parallel execution remains intentionally out of scope for this increment.

Remote delivery derives the authenticated GitHub account's ID-based `noreply`
commit address at delivery time. It avoids placing a Coder profile email into Git
metadata, so GitHub's private-email push protection remains enabled.

## Run

### Coder connection setup

Connected Accounts separates **Connect to Coder** (device authorization for the
registered Coder account) from **Install for repository access** (GitHub App
installation and repository selection). Both may be required. Installation alone
does not authorize Coder, and authorization alone does not prove repository access.
Harness polls the device exchange server-side, honoring pending and slow-down
responses, and displays expiry or failure. The private device code stays in memory;
only the one-time user code and verification URL reach the browser. Restarting
Harness interrupts pending exchanges; begin a new connection after a restart.


```sh
python3 app.py
```

Open the URL printed in the terminal. The server prefers port 4173 and chooses a free port if that port is occupied. Use `python3 app.py --port 4180` to request another port. Ctrl-C stops the server. State is stored in `.harness/`; `--data-dir PATH` selects another state directory.

## Backend structure

`app.py` remains the dependency-light executable and compatibility API. Backend
responsibilities that need independent evolution live in `harness_rotation/`:

- `adapters.py` owns the supported CLI command contracts.
- `coder_config.py` owns Coder URL validation, naming, and public presentation.
- `credentials.py` owns Coder token lookup and Keychain operations.
- `remote_transport.py` owns the validated SSH protocol used by Coder task helpers.
- `worktrees.py` owns local Git worktree, harness-process, and merge mechanics.
- `run_state.py` owns active-run queries, runner-slot queries, and execution leases.
- `harness_output.py` owns reply decoding and saved-log presentation.
- `coder_bridges.py` owns the private Codex JSON-RPC and Claude login transports
  used inside persistent Coder runners.
- `persistence.py` owns SQLite connections, the current schema, and numbered,
  idempotent migrations.
- `sessions.py` owns durable task-session rotation, handoff construction, and
  workspace-integrity reconciliation.

New backend behavior should enter through one of these boundaries rather than add
another responsibility directly to the HTTP handler. Existing tests and maintenance
scripts may continue to import `app` while modules are extracted incrementally.

## Workspace

- Browse local folders, name a project, and add it to the sidebar. Git and TASKS.md are not required to organize projects.
- Start tasks under a project. Each task holds a goal and an ongoing conversation.
- Create & run and Send & run request execution. Supervised tasks show an explicit dispatch approval before starting. Missing setup is shown in the conversation as a blocked run; creating text alone is no longer presented as running work.
- Open a task to add messages, configure its preferred harness/model, and inspect its attempts.
- Scan tools from the harness panel. Results are visible even before adding a project.
- Add/remove harnesses from rotation and reorder them with the arrows. Membership is the only enablement control. A task's preferred harness takes priority; unavailable preferences fall back to the first eligible harness in the rotation.
- Select discovered models or enter a custom model ID. Codex uses its local cache; Droid uses its CLI catalog; Claude offers aliases; OpenCode uses `opencode models` when installed. Discovery does not guarantee entitlement.
- New projects always start with no tasks, even if the folder already contains TASKS.md. Task conversations are stored in SQLite and existing files are left unchanged. No example projects or goals are seeded.

## Execution status

Chat renders a safe Markdown subset (headings, lists, emphasis, inline code, fenced code). Code blocks and raw logs have bounded scroll areas; raw logs start collapsed. New output follows the bottom until you scroll up; Jump to latest resumes following. All four adapters share the conversation and activity layout, with harness attribution on new replies. Codex uses structured events rather than dumping terminal output into chat. Attempt details expose saved Codex CLI session IDs and resume commands; Rotation does not synchronize these into desktop chat entries. Do not resume a native session separately while Rotation is editing the same folder.

Natural-language progress remains visible in sequence, with expandable tool batches between messages. Provider-emitted **reasoning summaries** are shown inline with each harness run and highlighted while a run is active; these are concise status summaries, not private model chain-of-thought. Saved logs reconstruct history after reload, including interrupted harness progress; up to the latest 500 normalized events per attempt are embedded, with bounded output previews and a full raw transcript download. Older Codex text logs recover explicit assistant sections. Failed Claude attempts retain their natural-language explanations, and recorded tool denials display **Needs permission**, not an undifferentiated failure.

Tool permissions can be chosen before creating a task or in task settings. **Use each harness’s default** inherits the per-harness settings; **Auto** overrides them for this task; **Use adapter defaults** uses the original adapter policies. Every new run snapshots the effective policy for all four harnesses, so later settings changes do not affect that run or its handoffs. Each attempt records its permission mode in history. Project supervised/unattended review gates remain separate from tool permissions.

Auto maps to Codex automatic approval review, Claude native `auto` review (subject to CLI/model/account support), and Droid medium autonomy. Adapter defaults are Codex review, Claude `acceptEdits`, Droid medium autonomy, and OpenCode's configured policy. OpenCode automatic review is unsupported and stops before launch if selected by a task override; no bypass is substituted. Live **Allow / Deny** in chat is not implemented and is marked unavailable in the controls. The API also refuses to execute an `ask` policy. No global CLI configuration is rewritten. The existing Claude permission-retry endpoint is scoped to a run and launches a new attempt, not approval of a still-paused tool call.

Tasks run in the existing project folder by default, including uncommitted/untracked work. Git is not required. Rotation does not commit, stash, reset, merge, or remove files in local mode. Closing a local run preserves its edits. Only one active or paused run may own a project. Avoid running a separate editing harness against that folder simultaneously.

New projects can opt into isolated Git worktrees under Execution settings. That mode requires a clean Git repository and initial commit, and retains the review/merge workflow. Existing projects migrate to local execution; old isolated attempts remain isolated.

Supervised runs pause before dispatch and after verification for review. Local review has a **Finish run** action, not a commit action. Unattended runs finish automatically. A missing Git baseline skips the default `git diff --check`; supply a real project verification command for meaningful validation. The diff for a local Git run includes pre-existing edits and is not an attempt-only change attribution.

Codex uses workspace-write with approval review, Claude uses `acceptEdits`, Droid uses `--auto medium`, and OpenCode uses its configured permissions. No adapter disables all permission checks. A working-directory argument is NOT an OS security boundary; Claude/Droid/OpenCode are not advertised as filesystem sandboxes. Permission failures are surfaced rather than silently called success.

The task header shows the current/next harness, requested model (or CLI default), and working location. Output and attempt history remain on the task. Native transcripts are not imported; conversation plus current code is supplied on each attempt. Quota rotates in the same folder; supervised tasks ask before a handoff. If no harness remains available, the run pauses with Resume and Close controls. Retry now clears a harness cooldown. Cooldowns parse simple relative resets and otherwise conservatively assume four hours; there is no automatic wake-up scheduler yet.

There is no billing checkbox. Rotation uses existing CLI credentials, blocks detected Codex/Claude API environment overrides, and does not guarantee subscription billing for every provider/configuration. It does not change your credentials or buy extra usage. Login failures are shown from the CLI. Runs currently have a ten-minute execution timeout, and server shutdown terminates tracked harness processes. Restart marks interrupted active runs stopped rather than leaving them falsely running.

Verified locally: actual Codex and Claude file edits in non-Git folders; actual Droid usage-limit response followed by a successful Claude edit through the HTTP runner. OpenCode's command follows its [CLI documentation](https://opencode.ai/docs/cli/), but it is not installed here, so live OpenCode execution remains unverified. Droid file-writing remains unverified while its quota is exhausted.

## Verification

```sh
python3 -B tests/smoke.py
python3 -B tests/test_execution.py
```

The integration test exercises folder browsing, project creation without Git, multiple goals, conversation persistence, project isolation, duplicate handling, and real harness/model discovery using disposable state. Its server binds an ephemeral port and closes in a finally block. Add `--serve` for a temporary browser test window (ten-minute maximum, Ctrl-C to stop earlier).

Execution tests replace only the AI command with deterministic subprocess workers. They cover local dirty and non-Git folders, preservation of existing changes, local close, same-folder quota handoff, review gates, isolated merge, visible verification/permission failures, project ownership, and retry.

Opt-in live tests consume a small amount of account usage:

```sh
python3 -B tests/real_adapter_smoke.py codex
python3 -B tests/real_adapter_smoke.py claude
python3 -B tests/real_adapter_smoke.py droid
python3 -B tests/live_handoff.py
```

The last test expects Droid's account to be quota-exhausted and verifies a real Droid → Claude handoff. All use disposable state/folders; temporary servers are closed after testing. Browser verification also covered blank slate → add project → create task → approve dispatch → review local result → finish run.
