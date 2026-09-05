"""User-facing memory stores for the Claude and Codex harnesses.

Both harnesses keep durable, model-visible memory on disk, but they shape it
differently.  Claude writes one Markdown file per fact into a directory chosen
per project, or into a single directory when `autoMemoryDirectory` is set in
settings.  Codex keeps one user-global store and folds in extra sources from
extension folders, each described by its own `instructions.md`; deleting an
extension resource is its documented signal for forgetting.

This module presents both through one vocabulary: a *scope* -- "global" or a
project -- holding *entries* that can be listed, created, edited and deleted.
Nothing here talks to a harness process; these are the same files the harnesses
read at the start of a session, so edits land without a restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MEMORY_TYPES = ("user", "feedback", "project", "reference")
EXTENSION_NAME = "aludra"
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_UNSAFE = re.compile(r"[^a-zA-Z0-9]")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _slug(value: str, fallback: str = "memory") -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:60]
    return text or fallback


def home() -> Path:
    return Path(os.path.expanduser("~"))


# --------------------------------------------------------------------------
# Shared entry format
# --------------------------------------------------------------------------

def _parse_entry(path: Path) -> Dict[str, Any]:
    """Read one memory file.  Frontmatter is optional so hand-written files and
    files produced by the harnesses both survive a round trip."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta: Dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            block = raw[3:end]
            body = raw[end + 4:].lstrip("\n")
            section = None
            for line in block.splitlines():
                if not line.strip():
                    continue
                indented = line[:1] in (" ", "\t")
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key, value = key.strip(), value.strip()
                if indented and section == "metadata":
                    meta[key] = value
                elif not indented:
                    section = key
                    if value:
                        meta[key] = value
    return {
        "id": path.stem,
        "title": meta.get("title") or _prettify(meta.get("name") or path.stem),
        "description": meta.get("description", ""),
        "type": meta.get("type", "project"),
        "body": body.strip(),
        "updated_at": meta.get("modified") or _stat_time(path),
        "path": str(path),
    }


def _stat_time(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_entry(entry: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> str:
    kind = entry.get("type", "project")
    if kind not in MEMORY_TYPES:
        kind = "project"
    lines = [
        "---",
        "name: %s" % entry["id"],
        "description: %s" % _one_line(entry.get("description", "")),
        "metadata:",
        "  node_type: memory",
        "  type: %s" % kind,
    ]
    title = _one_line(entry.get("title", ""))
    if title and title != entry["id"]:
        lines.append("  title: %s" % title)
    origin = (existing or {}).get("originSessionId")
    if origin:
        lines.append("  originSessionId: %s" % origin)
    lines += ["  modified: %s" % _now(), "---", "", entry.get("body", "").strip(), ""]
    return "\n".join(lines)


def _one_line(value: str) -> str:
    return " ".join((value or "").split())


def _prettify(slug: str) -> str:
    """Claude names a memory with a slug; the index beside it carries the
    readable form.  Recover one from the other when nothing was stored."""
    return re.sub(r"[-_]+", " ", slug).strip().title() or slug


def _entry_id(payload: Dict[str, Any]) -> str:
    raw = str(payload.get("id") or payload.get("title") or "").strip()
    candidate = _slug(raw)
    if not _SAFE_ID.match(candidate):
        raise ValueError("A memory needs a title using letters or numbers")
    return candidate


def _write_entry(directory: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    body = str(payload.get("body", "")).strip()
    if not body:
        raise ValueError("A memory needs a body")
    directory.mkdir(parents=True, exist_ok=True)
    entry_id = _entry_id(payload)
    target = directory / ("%s.md" % entry_id)
    previous = _parse_entry(target) if target.exists() else None
    original = str(payload.get("original_id") or "").strip()
    payload = dict(payload, id=entry_id)
    target.write_text(_render_entry(payload, previous), encoding="utf-8")
    if original and original != entry_id:
        stale = directory / ("%s.md" % original)
        if stale.exists() and _SAFE_ID.match(original):
            stale.unlink()
    return _parse_entry(target)


def _delete_entry(directory: Path, entry_id: str) -> None:
    if not _SAFE_ID.match(entry_id or ""):
        raise ValueError("Unknown memory")
    target = directory / ("%s.md" % entry_id)
    if not target.exists():
        raise ValueError("That memory no longer exists")
    target.unlink()


def _list_entries(directory: Path) -> List[Dict[str, Any]]:
    if not directory.is_dir():
        return []
    found = [
        _parse_entry(item)
        for item in sorted(directory.iterdir())
        if item.is_file() and item.suffix == ".md" and item.name != "MEMORY.md"
    ]
    return sorted(found, key=lambda entry: entry["updated_at"], reverse=True)


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------

def claude_home() -> Path:
    return home() / ".claude"


def claude_settings_path() -> Path:
    return claude_home() / "settings.json"


def _claude_settings() -> Dict[str, Any]:
    path = claude_settings_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8") or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def claude_project_slug(repo_path: str) -> str:
    """Claude keys its per-project directory on the working directory with every
    non-alphanumeric character replaced by a hyphen."""
    return _UNSAFE.sub("-", str(repo_path))


def claude_global_dir() -> Optional[Path]:
    configured = _claude_settings().get("autoMemoryDirectory")
    return Path(os.path.expanduser(str(configured))) if configured else None


def claude_project_dir(repo_path: str) -> Path:
    return claude_home() / "projects" / claude_project_slug(repo_path) / "memory"


def _rewrite_index(directory: Path) -> None:
    """Keep MEMORY.md -- the index Claude loads each session -- in step with the
    files beside it."""
    entries = _list_entries(directory)
    if not entries:
        index = directory / "MEMORY.md"
        if index.exists():
            index.unlink()
        return
    lines = [
        "- [%s](%s.md) — %s" % (entry["title"], entry["id"], _one_line(entry["description"]) or entry["title"])
        for entry in entries
    ]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def claude_set_global(enabled: bool, directory: Optional[str] = None) -> Dict[str, Any]:
    """Point Claude's auto-memory at one directory for every project, or return
    it to the per-project default."""
    settings = _claude_settings()
    if enabled:
        target = Path(os.path.expanduser(directory)) if directory else claude_home() / "memory"
        target.mkdir(parents=True, exist_ok=True)
        settings["autoMemoryDirectory"] = str(target)
    else:
        settings.pop("autoMemoryDirectory", None)
    path = claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copyfile(path, path.with_suffix(".json.bak"))
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return {"global_enabled": enabled, "directory": settings.get("autoMemoryDirectory")}


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------

INSTRUCTIONS = """# Aludra memories

These files are written directly by the user in Aludra, not derived from any
rollout. Treat every file here as authoritative and current.

- `resources/global/*.md` apply everywhere, with no scope restriction.
- `resources/<project-key>/scope.json` gives that directory's `cwd`. Every
  Markdown file beside it applies only when working under that `cwd`.
- A file removed from `resources/` means the user deleted that memory. Remove
  any memory derived only from it.

Cite these as `extensions/aludra/resources/<project-key>/<file>` with
`source=aludra`. They are not rollout summaries, so use
`### extension_resource_files` for provenance and never invent rollout paths or
thread IDs for them.
"""


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else home() / ".codex"


def codex_config_path() -> Path:
    return codex_home() / "config.toml"


def codex_memories_root() -> Path:
    return codex_home() / "memories"


def codex_extension_dir() -> Path:
    return codex_memories_root() / "extensions" / EXTENSION_NAME


def codex_enabled() -> bool:
    """Read `[features] memories` without a TOML parser -- Python 3.9 has none,
    and this is the only key we need."""
    path = codex_config_path()
    if not path.exists():
        return False
    table = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            table = stripped[1:-1].strip()
            continue
        if table == "features" and "=" in stripped:
            key, _, value = stripped.partition("=")
            if key.strip() == "memories":
                return value.strip().lower() == "true"
    return False


def codex_enable() -> Dict[str, Any]:
    """Set `[features] memories = true`, preserving the rest of config.toml."""
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if path.exists():
        shutil.copyfile(path, path.with_suffix(".toml.bak"))
    table, replaced, output = None, False, []
    for line in lines:
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if table == "features" and not replaced:
                output.append("memories = true")
                replaced = True
            table = stripped[1:-1].strip()
        elif table == "features" and "=" in stripped and stripped.partition("=")[0].strip() == "memories":
            output.append("memories = true")
            replaced = True
            continue
        output.append(line)
    if not replaced:
        if table == "features":
            output.append("memories = true")
        else:
            output += ["", "[features]", "memories = true"]
    path.write_text("\n".join(output).strip() + "\n", encoding="utf-8")
    return {"enabled": True}


def codex_project_key(project: Dict[str, Any]) -> str:
    """Stable per-project directory name.  Keyed on the repository path so a
    rename in Aludra does not orphan the memories, with the readable basename
    kept in front for anyone browsing the store by hand."""
    repo = str(project.get("repo_path") or "")
    digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:8]
    return "%s-%s" % (_slug(Path(repo).name or project.get("name") or "project"), digest)


def codex_scope_dir(project: Optional[Dict[str, Any]]) -> Path:
    resources = codex_extension_dir() / "resources"
    return resources / (codex_project_key(project) if project else "global")


def codex_prepare(project: Optional[Dict[str, Any]]) -> Path:
    """Create the extension folder on demand.  `instructions.md` is what makes
    the folder legible to Codex, so it is rewritten whenever it drifts."""
    directory = codex_scope_dir(project)
    directory.mkdir(parents=True, exist_ok=True)
    instructions = codex_extension_dir() / "instructions.md"
    if not instructions.exists() or instructions.read_text(encoding="utf-8") != INSTRUCTIONS:
        instructions.write_text(INSTRUCTIONS, encoding="utf-8")
    if project:
        scope = directory / "scope.json"
        payload = {"cwd": str(project.get("repo_path") or "")}
        if not scope.exists() or json.loads(scope.read_text(encoding="utf-8") or "{}") != payload:
            scope.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return directory


def codex_add_note(text: str) -> Dict[str, Any]:
    """Append an ad-hoc note.  Codex's own instructions forbid editing the
    consolidated memory files directly, so a free-text correction has to arrive
    as a note in the shape its tool enforces."""
    body = (text or "").strip()
    if not body:
        raise ValueError("A note needs some text")
    directory = codex_memories_root() / "extensions" / "ad_hoc" / "notes"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    name = "%s-%s.md" % (stamp, _slug(body.splitlines()[0], "note")[:60].strip("-") or "note")
    (directory / name).write_text(body + "\n", encoding="utf-8")
    return {"created": name}


# --------------------------------------------------------------------------
# One vocabulary over both harnesses
# --------------------------------------------------------------------------

def _claude_dir(project: Optional[Dict[str, Any]]) -> Path:
    if project is None:
        directory = claude_global_dir()
        if directory is None:
            raise ValueError(
                "Claude writes memory per project until a global directory is set. "
                "Turn on global memory to keep one list for every project."
            )
        return directory
    if claude_global_dir() is not None:
        raise ValueError("Claude is using one global memory directory, so it has no per-project list")
    return claude_project_dir(str(project.get("repo_path") or ""))


def _resolve(harness: str, project: Optional[Dict[str, Any]]) -> Path:
    if harness == "claude":
        return _claude_dir(project)
    if harness == "codex":
        if not codex_enabled():
            raise ValueError("Codex memories are turned off. Turn them on to start a store.")
        return codex_prepare(project)
    raise ValueError("That harness does not keep memory Aludra can edit")


def overview(projects: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Everything the Memories tab needs to draw itself, without committing to a
    harness or scope up front."""
    global_dir = claude_global_dir()
    claude_scopes = [{
        "key": "global",
        "label": "Global",
        "available": global_dir is not None,
        "count": len(_list_entries(global_dir)) if global_dir else 0,
        "path": str(global_dir) if global_dir else None,
    }]
    for project in projects:
        directory = claude_project_dir(str(project.get("repo_path") or ""))
        claude_scopes.append({
            "key": "project:%s" % project["id"],
            "label": project["name"],
            "project_id": project["id"],
            "available": global_dir is None,
            "count": len(_list_entries(directory)),
            "path": str(directory),
        })

    enabled = codex_enabled()
    codex_scopes = [{
        "key": "global",
        "label": "Global",
        "available": enabled,
        "count": len(_list_entries(codex_scope_dir(None))) if enabled else 0,
        "path": str(codex_scope_dir(None)),
    }]
    for project in projects:
        codex_scopes.append({
            "key": "project:%s" % project["id"],
            "label": project["name"],
            "project_id": project["id"],
            "available": enabled,
            "count": len(_list_entries(codex_scope_dir(project))) if enabled else 0,
            "path": str(codex_scope_dir(project)),
        })

    return {
        "types": list(MEMORY_TYPES),
        "harnesses": [
            {
                "key": "claude",
                "label": "Claude Code",
                "status": "ready",
                "summary": "One Markdown file per fact, read at the start of every session.",
                "global_enabled": global_dir is not None,
                "supports_notes": False,
                "scopes": claude_scopes,
            },
            {
                "key": "codex",
                "label": "Codex",
                "status": "ready" if enabled else "disabled",
                "summary": (
                    "A user-wide store Aludra extends with its own memory folder."
                    if enabled else
                    "Codex keeps memories off until you turn them on."
                ),
                "global_enabled": True,
                "supports_notes": enabled,
                "scopes": codex_scopes,
            },
        ],
    }


def entries(harness: str, project: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    directory = _resolve(harness, project)
    listed = _list_entries(directory)
    return {
        "entries": listed,
        "path": str(directory),
        "pending": harness == "codex",
    }


def save(harness: str, project: Optional[Dict[str, Any]], payload: Dict[str, Any]) -> Dict[str, Any]:
    directory = _resolve(harness, project)
    entry = _write_entry(directory, payload)
    if harness == "claude":
        _rewrite_index(directory)
    return {"entry": entry}


def delete(harness: str, project: Optional[Dict[str, Any]], entry_id: str) -> Dict[str, Any]:
    directory = _resolve(harness, project)
    _delete_entry(directory, entry_id)
    if harness == "claude":
        _rewrite_index(directory)
    return {"ok": True}
