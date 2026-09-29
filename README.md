# Harness Rotation

Harness Rotation is a local-first task board for coordinating coding harnesses against real project folders. A task keeps its goal, conversation, attempts, verification output, diff, and review state together while work moves between supported CLIs.

It is experimental desktop tooling for a single trusted user, not a hosted service or a security sandbox.

## What it does

- Organizes local code folders into projects and persistent tasks.
- Runs Codex, Claude Code, Droid, or OpenCode when those CLIs are installed and authenticated locally.
- Preserves progress, logs, diffs, and verification results across attempts and controlled fallback.
- Supports explicit dispatch and review gates before delivery actions.
- Optionally connects to a self-hosted Coder deployment for persistent remote runners and task worktrees.

Read [DOMAIN.md](DOMAIN.md) for the product model and [the persistent-runner decision](docs/adr/0001-persistent-coder-runners.md) for the remote-workspace boundary.

## Requirements

- Python 3.9+
- Node.js 20+
- npm
- Git, for Git-backed projects and isolated worktrees

The optional Coder flow is intended for a self-hosted Coder deployment. Coder credentials are stored in the operating-system keychain when available; do not commit them or the local .harness/ state directory.

## Quick start

Install the root and frontend dependencies, then build the frontend bundle:

    npm ci
    npm --prefix frontend ci
    npm run build
    python3 app.py

Open the localhost URL printed by the server. State is written to .harness/ by default; pass --data-dir PATH to isolate it elsewhere.

For frontend development, use the combined development script after installing dependencies:

    npm run dev

It starts the Python API on port 4173 and Vite with API proxying. Press Ctrl-C to stop both processes.

## Safety boundaries

- Local runs edit the selected project folder and may include its existing uncommitted work.
- A working directory and a Git worktree are not operating-system security boundaries. Do not use this tool with untrusted prompts or sensitive repositories unless you have independently constrained the execution environment.
- Harness Rotation does not automatically commit, push, merge, or create a pull request from local work.
- Remote Coder tasks stop for review before delivery. Credentials remain in the runner or keychain; they are not stored in Harness state.
- Provider usage, account limits, and CLI permissions remain controlled by the installed harnesses. This project does not guarantee subscription billing or provider availability.

## Verification

Run the full local test suite:

    npm test

For narrower checks:

    npm run test:python
    npm --prefix frontend test
    npm --prefix frontend run test:e2e
    npm run format:check

Some live tests intentionally consume provider usage and are opt-in:

    python3 -B tests/real_adapter_smoke.py codex
    python3 -B tests/real_adapter_smoke.py claude
    python3 -B tests/real_adapter_smoke.py droid
    python3 -B tests/live_handoff.py

## Architecture

app.py is the dependency-light HTTP application and compatibility API. The harness_rotation/ package holds the backend boundaries, frontend/ contains the React/Vite interface, and infra/runner/ contains standard-library-only programs sent to persistent Coder runners.

The committed static/ bundle is intentionally absent: generate it with npm run build. It is ignored as build output.

## Contributing

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Security reports belong in the process described in [SECURITY.md](SECURITY.md), not in public issues.
