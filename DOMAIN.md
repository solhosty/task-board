# Product model

A **project** is a named local folder containing code, usually a Git repository. It is an organizational container and its default execution policy, not the objective being delegated. A project can contain many tasks and is presented as a board grouped by task state.

A **task** is a persistent goal. It may be a feature, a fix, or an entire application the user wants built. It is not necessarily a small checklist item. Its objective, conversation, working state, and attempts remain together over time.

An **attempt** is one harness working toward that task's goal. Codex, Claude Code, Droid, and OpenCode can be successive workers on the same task. Rotation changes the worker, not the task's identity.

A **Coder server** is a registered self-hosted execution environment. Its URL, organization, capability status, and a yes/no indication that a credential exists are recorded by Harness. The API token itself is kept in the operating-system keychain and is never stored in project state.

A **project Coder profile** selects one Coder server and describes the project's approved remote environment: repository source, base branch, setup profile, and preferred execution target. Harness derives a versioned template from this profile and a server-specific approved blueprint.

An **execution target** is the place a task runs: the project default, local, or the project’s Coder environment. Each task displays its effective target on the board and may override the project default. Selecting Coder never moves the user's local folder automatically; remote work starts from a reachable repository source and recorded checkpoint.

The task conversation records the user's instructions and progress events. Harness-native conversations are not imported automatically, but provider-emitted reasoning summaries and user-visible progress updates are retained with each run. Private model chain-of-thought is not recorded. Code, diffs, verification, and the progress note accompany a handoff.

Projects can run directly in their existing local folder, with or without Git. Local execution is the default and includes uncommitted code. Rotation does not commit, stash, reset, or delete local project changes. One active or paused run owns a project at a time. A new project can optionally use isolated Git worktrees instead, which require a clean repository and an initial commit.

The rotation is an ordered pool of harnesses. Membership enables selection; there is no separate enablement or billing checkbox. A task preference overrides the order when available. Quota pauses a harness and hands the same task and working directory to the next candidate. CLI authentication and permissions remain owned by each CLI.
