"""Stage task attachments in a persistent Coder runner without touching Git."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path("/home/coder/.harness-runner/attachments").resolve()


def main(encoded):
    request = json.loads(base64.urlsafe_b64decode(encoded.encode()).decode())
    task_key = str(request.get("task_key") or "")
    if not task_key.startswith("task-") or any(ch not in "0123456789abcdef-" for ch in task_key[5:]):
        raise ValueError("Invalid attachment task key.")
    destination = (ROOT / task_key).resolve()
    if ROOT not in destination.parents:
        raise ValueError("Invalid attachment destination.")
    destination.mkdir(parents=True, exist_ok=True)
    received = json.loads(sys.stdin.read())
    if not isinstance(received, list) or len(received) != len(request.get("attachments") or []):
        raise ValueError("Attachment transfer was incomplete.")
    staged = []
    for expected, item in zip(request["attachments"], received):
        if not isinstance(item, dict) or item.get("sha256") != expected.get("sha256"):
            raise ValueError("Attachment transfer metadata did not match.")
        data = base64.b64decode(item.get("data") or "", validate=True)
        if len(data) != expected.get("byte_size") or hashlib.sha256(data).hexdigest() != expected.get("sha256"):
            raise ValueError("Attachment checksum verification failed.")
        name = str(expected.get("filename") or "attachment")
        target = (destination / (expected["sha256"][:12] + "-" + name)).resolve()
        if target.parent != destination:
            raise ValueError("Invalid attachment filename.")
        if target.is_file() and target.stat().st_size == len(data):
            if hashlib.sha256(target.read_bytes()).hexdigest() == expected["sha256"]:
                staged.append({"id": expected.get("id"), "stored_path": str(target), **expected})
                continue
        fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=str(destination))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        staged.append({"id": expected.get("id"), "stored_path": str(target), **expected})
    print(json.dumps(staged, separators=(",", ":")))


if __name__ == "__main__":
    main(sys.argv[1])
