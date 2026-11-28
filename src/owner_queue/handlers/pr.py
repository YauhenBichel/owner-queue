"""PostToolUse on Bash: a pull request was opened.

Recorded: the pull request's URL, repository, number and the title given on the
command line. The command itself and its output are not stored.
"""

from __future__ import annotations

import re

from .. import config, text
from ..store import Queue
from . import Context

_CREATE = re.compile(r"\bgh\b[^|;&\n]*?\bpr\s+(?:create|new)\b")
# Title forms: \"escaped\" (the command sits inside bash -c "..." or ssh "..."), "double", 'single', bare.
_TITLE = re.compile(
    r"(?:--title|-t)(?:=|\s+)"
    r"(?:\\\"((?:[^\"\\]|\\(?!\"))*)\\\"|\"((?:[^\"\\]|\\.)*)\"|'([^']*)'|(\S+))"
)
_ANNOUNCED = "Creating pull request for"
_MAX_OUTPUT = 200_000


def run(queue: Queue, event: dict) -> "tuple | None":
    """Queue the pull request this Bash call created. Returns (item id, newly queued) or None."""
    if event.get("tool_name") != "Bash":
        return None
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    command = command if isinstance(command, str) else ""
    output = _output(event.get("tool_response"))
    created = _created(command, output)
    if created is None:
        return None
    url, repo, number = created
    data = {"kind": "pr", "key": text.pr_key(url), "url": url, "urls": [url], "repo": repo, "number": number}
    title = _title(command)
    if title:
        data["title"] = title
    data.update(Context(event).fields())
    added = queue.add(data, wait=config.HOOK_LOCK_WAIT, limit=True)
    return added if added[0] else None


def _created(command: str, output: str):
    """The pull request this call created, or None. Links that are only mentioned do not count."""
    if _CREATE.search(command):
        links = text.PR_URL.findall(output)
        if links:  # gh prints the new pull request's URL last
            host, owner, repo, number = links[-1]
            return text.pull_request(f"https://{host}/{owner}/{repo}/pull/{number}")
        return None
    at = output.find(_ANNOUNCED)   # gh ran inside a script
    if at >= 0:
        return text.pull_request(output[at:])
    return None


def _title(command: str) -> str:
    if not _CREATE.search(command):
        return ""
    match = _TITLE.search(command)
    if not match:
        return ""
    title = next((g for g in match.groups() if g), "")
    if match.group(2):
        title = re.sub(r"\\(.)", r"\1", title)
    return text.field(title, config.MAX_TITLE)


def _output(response, depth: int = 0) -> str:
    """The text of a tool result, whatever its shape (the schema depends on the tool)."""
    if isinstance(response, str):
        return response[:_MAX_OUTPUT]
    if depth > 3:
        return ""
    if isinstance(response, dict):
        parts = [_output(v, depth + 1) for v in response.values()]
    elif isinstance(response, list):
        parts = [_output(v, depth + 1) for v in response[:50]]
    else:
        return ""
    return "\n".join(p for p in parts if p)[:_MAX_OUTPUT]
