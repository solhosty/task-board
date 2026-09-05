"""Static definitions for supported command-line harness adapters."""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


CommandBuilder = Callable[[Path, str, str], List[str]]
Adapter = Dict[str, Any]


def _model_args(model: str) -> List[str]:
    return [] if model in ("", "default") else ["--model", model]


def _codex(root: Path, model: str, prompt: str) -> List[str]:
    return [
        "codex", "exec", "--json", "--approve-for-me", "--skip-git-repo-check",
        "--cd", str(root), *_model_args(model), prompt,
    ]


def _claude(root: Path, model: str, prompt: str) -> List[str]:
    del root  # Claude is launched with the project as its process cwd.
    return [
        "claude", "-p", "--permission-mode", "acceptEdits", "--verbose",
        "--output-format", "stream-json", *_model_args(model), prompt,
    ]


def _droid(root: Path, model: str, prompt: str) -> List[str]:
    return [
        "droid", "exec", "--auto", "medium", "--output-format", "stream-json",
        "--cwd", str(root), *_model_args(model), prompt,
    ]


def _opencode(root: Path, model: str, prompt: str) -> List[str]:
    del root  # OpenCode is launched with the project as its process cwd.
    return ["opencode", "run", "--format", "json", *_model_args(model), prompt]


# Commands are argument lists: no shell interpolation and no configurable command
# fragments. Each adapter retains its CLI permission checks; cwd is not an OS
# sandbox. The dictionary remains mutable because tests and local development use
# deterministic command builders in place of real subscription-backed CLIs.
ADAPTERS: Dict[str, Adapter] = {
    "codex": {
        "label": "Codex", "binary": "codex", "runnable": True,
        "models": ["default"], "build": _codex,
        "safety_note": "Uses Codex workspace-write sandbox with automatic approval review.",
    },
    "claude": {
        "label": "Claude Code", "binary": "claude", "runnable": True,
        "models": ["opus", "sonnet", "haiku", "default"], "build": _claude,
        "safety_note": "Uses the installed CLI with its permission checks intact.",
    },
    "droid": {
        "label": "Droid", "binary": "droid", "runnable": True,
        "models": ["default"], "build": _droid,
        "safety_note": "Uses the installed CLI with its permission checks intact.",
    },
    "opencode": {
        "label": "OpenCode", "binary": "opencode", "runnable": True,
        "models": ["default"], "build": _opencode,
        "safety_note": "Uses OpenCode run with configured provider permissions.",
    },
}


def permission_blocker(adapters: Dict[str, Adapter], key: str, mode: str) -> Optional[str]:
    if mode == "ask":
        return (
            "%s: live approval in chat is not supported by this adapter yet. "
            "No command was started. Choose a supported permission mode in task settings."
            % adapters[key]["label"]
        )
    if mode == "auto" and key == "opencode":
        return (
            "OpenCode: automatic review is not supported by this adapter. No command was "
            "started. Use harness defaults with your configured OpenCode policy, or choose "
            "another harness."
        )
    return None


def configured_command(
    adapters: Dict[str, Adapter],
    harness: Dict[str, Any],
    root: Path,
    prompt: str,
    override: Optional[str] = None,
) -> List[str]:
    """Build a command only after validating the effective permission policy."""
    mode = override or harness.get("tool_permissions") or "standard"
    if mode not in ("standard", "auto", "ask"):
        raise ValueError("Unknown permission mode")
    problem = permission_blocker(adapters, harness["key"], mode)
    if problem:
        raise ValueError(problem)
    command = adapters[harness["key"]]["build"](root, harness["model"], prompt)
    if harness["key"] == "claude" and mode == "auto" and "--permission-mode" in command:
        command[command.index("--permission-mode") + 1] = "auto"
    return command
