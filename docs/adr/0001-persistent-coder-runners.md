# Persistent runners, separate task worktrees

Reuse a private Coder runner per registered deployment, organization, and authenticated Coder user, with a separate persistent worktree per task. This replaces new containers per task so native Codex and Claude Code logins can remain in their own persistent home without copying subscription tokens. Concurrency is a configurable admission limit, not an intrinsic one-task restriction of the runner; worktrees share an OS user, credentials, ports, and resources and are not security isolation boundaries.

Remote process scheduling and model sign-in are separate implementation slices. The current scheduler remains serial until simultaneous native CLI sessions, refresh, resource allocation, and cancellation are tested; no shared credential broker is introduced.
