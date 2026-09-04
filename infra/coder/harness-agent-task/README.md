# Harness agent task template

The first live Coder template used by Harness Rotation. It is intentionally a
minimal Docker workspace: persistent `/home/coder`, a stable
`/home/coder/task` execution directory, Git, Python, Node, the official Codex
standalone installation, and Coder Agent metadata. Project checkout, model
credentials, and task dispatch are added by the dispatcher after this scaffold
has been verified in a disposable workspace.

Harness uses the runner's Coder external-auth token only inside the workspace when
the user explicitly commits a reviewed remote task, pushes it, and opens or reuses
a GitHub pull request. The credential is never sent back to Harness.
