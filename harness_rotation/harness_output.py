"""Normalize saved harness output for replies and activity presentation."""

import json
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Optional, Tuple


def decode_result(key: str, output: str) -> Tuple[str, Optional[str]]:
    """Interpret structured CLI output, including failures that exit with code zero."""
    del key  # Kept in the public contract for future provider-specific decoding.
    messages, failure = [], None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            messages.append(item.get("text", ""))
        if event.get("type") == "item.completed" and item.get("type") == "error":
            failure = item.get("message") or item.get("error") or "The harness reported an execution error."
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content", [])
            messages.extend(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        if event.get("type") == "text":
            messages.append(event.get("part", {}).get("text", ""))
        if event.get("type") == "message" and event.get("role") == "assistant":
            messages.append(event.get("text", ""))
        if isinstance(event.get("result"), str):
            messages = [event["result"]]
        if event.get("is_error") or event.get("type") == "error" or event.get("permission_denials"):
            failure = (
                event.get("result")
                or event.get("message")
                or str(event.get("error") or event.get("permission_denials"))
            )
    return "\n\n".join(messages).strip() or output[-16000:], failure


def log_details(
    attempt: Optional[Dict[str, Any]],
    read_events: Callable[[Path], List[Dict[str, Any]]],
) -> Dict[str, Any]:
    path = Path(attempt["log_path"]) if attempt and attempt.get("log_path") else None
    if not path or not path.exists():
        return {"log": "No log yet.", "activity": [], "session_id": None, "resume_command": None}
    with path.open("rb") as handle:
        prefix = handle.read(65536).decode("utf-8", errors="replace")
        handle.seek(max(0, path.stat().st_size - 30000))
        tail = handle.read().decode("utf-8", errors="replace")
    session_id = None
    for line in prefix.splitlines():
        match = re.match(r"^session id:\s*([0-9a-f-]{36})\s*$", line)
        if match:
            session_id = match[1]
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            candidate = event.get("thread_id", "")
            if re.fullmatch(r"[0-9a-f-]{36}", candidate):
                session_id = candidate
    activity = []
    for line in tail.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item") or {}
        if event.get("type") in ("item.started", "item.completed"):
            kind = item.get("type")
            if kind == "command_execution":
                activity.append({"kind": "command", "text": item.get("command", "")[:240], "status": item.get("status", "")})
            elif kind == "file_change":
                activity.append({
                    "kind": "edit",
                    "text": ", ".join(change.get("path", "") for change in item.get("changes", []))[:240],
                    "status": item.get("status", ""),
                })
            elif kind == "agent_message":
                activity.append({"kind": "message", "text": item.get("text", "")[:1500], "status": ""})
        if event.get("type") == "assistant":
            for part in event.get("message", {}).get("content", []):
                if part.get("type") == "text":
                    activity.append({"kind": "message", "text": part.get("text", "")[:1500], "status": ""})
                elif part.get("type") == "tool_use":
                    inputs = part.get("input") or {}
                    activity.append({
                        "kind": "tool",
                        "text": part.get("name", "Tool") + " · " + str(
                            inputs.get("command") or inputs.get("file_path") or inputs.get("path") or ""
                        )[:240],
                        "status": "working",
                    })
        if event.get("type") == "message" and event.get("role") == "assistant":
            activity.append({"kind": "message", "text": event.get("text", "")[:1500], "status": ""})
        if event.get("type") in ("tool_call", "tool_use"):
            activity.append({
                "kind": "tool",
                "text": str(
                    event.get("toolName") or event.get("tool_name") or event.get("name") or "Tool"
                )[:240],
                "status": "working",
            })
        if event.get("type") == "text":
            activity.append({"kind": "message", "text": event.get("part", {}).get("text", "")[:1500], "status": ""})
        if event.get("type") == "tool_use" and isinstance(event.get("part"), dict):
            part = event["part"]
            activity[-1] = {
                "kind": "tool",
                "text": str(part.get("tool") or "Tool")[:240],
                "status": str(part.get("state", {}).get("status", "working")),
            }
    history = read_events(path)
    return {
        "log": tail,
        "activity": activity[-6:],
        "events": history,
        "session_id": session_id,
        "resume_command": (
            "codex resume %s" % session_id
            if session_id and attempt["harness_key"] == "codex"
            else None
        ),
    }

