"""One handler per hook event.

A handler takes the queue and the hook's event (the JSON Claude Code sends on
stdin) and records or closes items. It prints nothing and decides nothing for
Claude Code: these hooks only observe. They make no network calls and never run
`gh`; whatever goes wrong, the caller swallows it and the hook exits 0.
"""

from __future__ import annotations

import hashlib
import json

from .. import config, text


class Context:
    """Who the event is about: the session, its working directory, and the subagent if any."""

    def __init__(self, event: dict):
        self.cwd = text.plain(event.get("cwd"), config.MAX_PATH)
        session = text.plain(event.get("session_id"), config.MAX_ID)
        # Sessions are told apart by id; without one, by the full working directory
        # (never by its last folder name, which two projects can share).
        self.session = session or ("cwd:" + self.cwd if self.cwd else "")
        self.agent = text.plain(event.get("agent_id"), config.MAX_ID)

    def fields(self) -> dict:
        return {k: v for k, v in (("session", self.session), ("cwd", self.cwd), ("agent", self.agent)) if v}


def tool_digest(event: dict) -> str:
    """A fingerprint of a tool call's input, so the call can be recognised without storing it."""
    try:
        raw = json.dumps(event.get("tool_input"), sort_keys=True, default=str)
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]
