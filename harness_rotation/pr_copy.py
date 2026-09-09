"""Turn a raw task description into a commit/PR title that reads like a human wrote it.

Prefers the harness's own PR_TITLE/PR_SUMMARY footer (see `app.task_prompt` and
`app.extract_pr_summary`) since the agent that made the change can describe it
far better than the user's original, often conversational, task text.
"""
from typing import Optional, Tuple

DEFAULT_TITLE_LIMIT = 68


def trim_title(text: str, limit: int = DEFAULT_TITLE_LIMIT) -> str:
    """Truncate at a word boundary instead of mid-word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    truncated = text[:limit].rsplit(" ", 1)[0].rstrip(".,;:-")
    return truncated or text[:limit]


def title_and_body(task_text: str, pr_title: Optional[str], pr_summary: Optional[str]) -> Tuple[str, str]:
    title = trim_title((pr_title or "").strip() or task_text)
    body = (pr_summary or "").strip() or ("Created by Harness Rotation from: " + trim_title(task_text, 200))
    return title, body
