"""Files and images a user attaches to a task conversation.

Attachments are kept outside the task worktree, under the application data
directory, for two reasons: a harness that reads one can never turn it into a
repository change, because the merge step's `git add -A` only ever sees the
harness's own work; and the same stored file survives worktree cleanup and
session rotation.

Each CLI then receives the set in the best form it supports.  Codex takes
images on its own `--image` flag and OpenCode takes any file on `--file`, so
those two get real model input.  Claude Code and Droid have no attachment flag,
so they receive the same absolute paths in the prompt and open them with their
own file tools; Claude additionally needs the folder allowed with `--add-dir`
because it sits outside the working directory.  Every harness therefore sees
every attachment -- what differs is only whether the CLI carries the bytes or
the agent opens the path itself.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

# Image types every image-capable harness accepts.  SVG is deliberately absent:
# it is markup rather than a raster image, and travels better as a file.
IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}
FILE_TYPES = {
    ".pdf": "application/pdf", ".txt": "text/plain", ".log": "text/plain",
    ".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv",
    ".json": "application/json", ".yml": "text/yaml", ".yaml": "text/yaml",
    ".toml": "text/plain", ".ini": "text/plain", ".env": "text/plain",
    ".html": "text/html", ".xml": "text/xml", ".svg": "text/xml",
    ".diff": "text/x-diff", ".patch": "text/x-diff", ".sql": "text/plain",
    ".py": "text/x-python", ".js": "text/javascript", ".jsx": "text/javascript",
    ".ts": "text/typescript", ".tsx": "text/typescript", ".css": "text/css",
    ".sh": "text/x-shellscript", ".rs": "text/plain", ".go": "text/plain",
}

MAX_BYTES = 10 * 1024 * 1024
MAX_TASK_BYTES = 64 * 1024 * 1024
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def classify(filename: str) -> Tuple[str, str]:
    """The kind and media type of a supported attachment name."""
    suffix = Path(filename or "").suffix.lower()
    if suffix in IMAGE_TYPES:
        return "image", IMAGE_TYPES[suffix]
    if suffix in FILE_TYPES:
        return "file", FILE_TYPES[suffix]
    supported = ", ".join(sorted(IMAGE_TYPES) + sorted(FILE_TYPES))
    raise ValueError("%s is not a supported attachment type. Supported: %s" %
                     (suffix or "That file", supported))


def safe_name(filename: str) -> str:
    """A stored name a shell, a path, and a prompt can all carry unchanged."""
    stem = _UNSAFE.sub("-", Path(filename or "").name).strip("-.")
    return stem[:80] or "attachment"


def decode(payload: Any) -> bytes:
    """Bytes from the browser's base64, with or without a data-URL prefix."""
    text = payload if isinstance(payload, str) else ""
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    try:
        return base64.b64decode(re.sub(r"\s+", "", text), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("The attachment could not be read. Try adding it again.") from None


def directory(data_root: Path, task_id: int) -> Path:
    return data_root / "attachments" / ("task-%d" % task_id)


def readable_size(size: int) -> str:
    if size >= 1024 * 1024:
        return "%.1f MB" % (size / (1024 * 1024))
    return "%.0f KB" % max(size / 1024, 1)


def save(data_root: Path, task_id: int, filename: str, data: bytes) -> Dict[str, Any]:
    """Store one attachment and describe it for the task record."""
    kind, media = classify(filename)
    if not data:
        raise ValueError("That file is empty.")
    if len(data) > MAX_BYTES:
        raise ValueError("Attachments are limited to %s." % readable_size(MAX_BYTES))
    folder = directory(data_root, task_id)
    folder.mkdir(parents=True, exist_ok=True)
    stored_total = sum(item.stat().st_size for item in folder.iterdir() if item.is_file())
    if stored_total + len(data) > MAX_TASK_BYTES:
        raise ValueError("This task already holds %s of attachments. Remove some first."
                         % readable_size(stored_total))
    digest = hashlib.sha256(data).hexdigest()
    stored = folder / ("%s-%s" % (digest[:12], safe_name(filename)))
    stored.write_bytes(data)
    return {
        "filename": safe_name(filename), "media_type": media, "kind": kind,
        "byte_size": len(data), "sha256": digest, "stored_path": str(stored),
    }


def remove(record: Dict[str, Any]) -> None:
    """Delete one stored file, tolerating a store a user already cleaned up."""
    path = Path(record.get("stored_path") or "")
    if path.is_file():
        path.unlink()


def remove_task(data_root: Path, task_id: int) -> None:
    folder = directory(data_root, task_id)
    for item in folder.iterdir() if folder.is_dir() else ():
        if item.is_file():
            item.unlink()
    if folder.is_dir():
        folder.rmdir()


def existing(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Only the attachments whose stored file is still on disk.

    A record whose file was removed underneath us would otherwise become a
    broken path in a prompt or an argument the CLI rejects outright.
    """
    return [item for item in records if Path(item.get("stored_path") or "").is_file()]


def describe(records: List[Dict[str, Any]]) -> str:
    """The prompt section naming every attachment by absolute path."""
    if not records:
        return ""
    lines = [
        "- %s \"%s\" (%s, %s): %s" % (item["kind"], item["filename"], item["media_type"],
                                      readable_size(item["byte_size"]), item["stored_path"])
        for item in records
    ]
    return ("Attachments the user added to this conversation:\n" + "\n".join(lines) +
            "\n\nRead each one before acting on it; some are already attached directly to this "
            "prompt by the CLI. They live outside the working directory: do not copy, move, or "
            "commit them into the project unless the conversation asks for that.")


def command_arguments(harness_key: str, records: List[Dict[str, Any]]) -> List[str]:
    """CLI arguments that hand this harness the attachments it can carry."""
    if not records:
        return []
    if harness_key == "codex":
        return [part for item in records if item["kind"] == "image"
                for part in ("--image", item["stored_path"])]
    if harness_key == "opencode":
        return [part for item in records for part in ("--file", item["stored_path"])]
    if harness_key == "claude":
        # Claude reads the paths from the prompt, but only inside directories it
        # was allowed; the attachment store is outside the project.
        folders = sorted({str(Path(item["stored_path"]).parent) for item in records})
        return [part for folder in folders for part in ("--add-dir", folder)]
    return []
