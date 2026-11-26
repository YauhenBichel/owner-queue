"""Stop: the session finished its turn and left links or a question for the owner.

Conservative on purpose. A session has at most one open "review" item; a newer
one replaces it in place, and it closes when the owner next prompts that
session. Only a short, redacted excerpt and the links are stored.
"""

from __future__ import annotations

import json
import os
import re

from .. import config, text
from ..store import Queue
from . import Context

_ASK = re.compile(
    r"(?i)\b(?:"
    r"please (?:review|merge|approve|confirm|decide|choose|check|verify|open|run|advise|tell me|let me know)"
    r"|let me know|do you want|would you like|should i\b|shall i\b|want me to"
    r"|which (?:option|one|approach)"
    r"|your (?:decision|call|approval|review|input|sign-off|confirmation)"
    r"|wait(?:ing)? (?:for|on) (?:you|your)"
    r"|ready (?:for|to) (?:your )?(?:review|merge|be merged)"
    r"|needs? (?:your|you to|a decision|approval)"
    r"|you(?:'ll| will)? need to|up to you|awaiting (?:your|approval|review)"
    r"|before i (?:continue|proceed)"
    r")"
)
_ASK_WINDOW = 800      # characters at the end of the message where a request is looked for
_QUESTION_LINES = 4


def run(queue: Queue, event: dict) -> "tuple | None":
    if event.get("agent_id"):
        return None   # a subagent reports to its parent session, not to the owner
    ctx = Context(event)
    if not ctx.session:
        return None
    message = event.get("last_assistant_message")
    if not isinstance(message, str) or not message.strip():
        message = last_assistant_text(event.get("transcript_path"))
    if not message.strip():
        return None
    links = text.urls(message, config.MAX_URL, config.MAX_URLS)
    asks = asks_owner(message)
    if not links and not asks:
        return None
    if links and not asks:
        # Links to pull requests that are already queued are not a second item.
        state = queue.load()
        if all(_queued_pr(state, url) for url in links):
            return None
    data = {"kind": "review", "key": "review:" + ctx.session}
    excerpt = text.field(message, config.MAX_TEXT, keep_end=True)
    if excerpt:
        data["text"] = excerpt
    if links:
        data["urls"] = links
    data.update(ctx.fields())
    added = queue.add(data, wait=config.HOOK_LOCK_WAIT, limit=True)
    return added if added[0] else None


def _queued_pr(state, url: str) -> bool:
    found = text.pull_request(url)
    return bool(found) and state.by_key(text.pr_key(found[0]), only_open=False) is not None


def asks_owner(message: str) -> bool:
    """True when the end of the message asks the owner to decide or do something."""
    tail = message[-_ASK_WINDOW:]
    lines = [line.strip().rstrip("*_`) ") for line in tail.splitlines() if line.strip()]
    if any(line.endswith("?") for line in lines[-_QUESTION_LINES:]):
        return True
    return bool(_ASK.search(tail))


def last_assistant_text(path) -> str:
    """The last assistant message in a transcript, reading only the end of the file.

    A fallback: current Claude Code versions pass the message to the hook
    directly, because the transcript can lag behind the conversation.
    """
    if not isinstance(path, str) or not path:
        return ""
    try:
        with open(os.path.expanduser(path), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - config.TRANSCRIPT_TAIL)
            f.seek(start)
            raw = f.read(config.TRANSCRIPT_TAIL)
    except OSError:
        return ""
    lines = raw.split(b"\n")
    if start:
        lines = lines[1:]   # cut in the middle
    for line in reversed(lines):
        if b'"assistant"' not in line:
            continue
        try:
            entry = json.loads(line.decode("utf-8", "replace"))
            if entry.get("type") != "assistant" or entry.get("isSidechain"):
                continue
            content = entry["message"]["content"]
            if isinstance(content, str):
                parts = [content]
            else:
                parts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"]
            joined = "\n".join(p for p in parts if isinstance(p, str) and p.strip())
        except (ValueError, KeyError, TypeError, AttributeError):
            continue   # one unreadable line does not hide the others
        if joined:
            return joined
    return ""
