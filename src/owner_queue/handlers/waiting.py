"""A session is waiting for the owner, and the moment it moves again.

Notification       -> a "waiting" item (permission prompt, idle prompt, MCP dialog)
PermissionRequest  -> nothing is queued; the tool call is remembered by fingerprint so
                      the notification that follows can be tied to it
PostToolUse, PostToolUseFailure, SubagentStop, UserPromptSubmit, Stop, SessionEnd
                   -> the session moved: its "waiting" item closes

A session can run several subagents at once, and a prompt for one of them is
not answered because another one called a tool. So a waiting item closes when
the same agent moves, or when the very tool call it was waiting for completes.
"""

from __future__ import annotations

from .. import config, text
from ..store import Queue
from . import Context, tool_digest

# notification_type values that mean "the owner has to do something in that session"
WAITING_TYPES = ("permission_prompt", "idle_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input")


def permission_request(queue: Queue, event: dict) -> None:
    ctx = Context(event)
    tool = text.plain(event.get("tool_name"), config.MAX_ID)
    if ctx.session and tool:
        queue.note(ctx.session, ctx.agent, tool, tool_digest(event))


def notification(queue: Queue, event: dict, idle: bool = True) -> "tuple | None":
    ntype = event.get("notification_type")
    if ntype not in WAITING_TYPES:
        return None
    ctx = Context(event)
    if not ctx.session:
        return None
    if ntype == "idle_prompt" and not idle:
        return None
    state = queue.load()
    if ntype == "idle_prompt" and any(
        i.session == ctx.session and i.kind in ("review", "waiting") for i in state.open_items()
    ):
        return None   # that session already has an item saying it needs the owner
    data = {"kind": "waiting", "ntype": ntype}
    message = text.field(event.get("message"), config.MAX_MESSAGE)
    if message:
        data["text"] = message
    data.update(ctx.fields())
    note = state.notes.get(ctx.session)
    if ntype == "permission_prompt" and note:
        # The request this prompt is about: which agent asked, and for which call.
        if not ctx.agent and note["agent"]:
            data["agent"] = note["agent"]
        if note["tool"]:
            data["tool"] = note["tool"]
        if note["digest"]:
            data["digest"] = note["digest"]
    data["key"] = "waiting:" + ctx.session + ":" + data.get("agent", "")
    added = queue.add(data, wait=config.HOOK_LOCK_WAIT, limit=True)
    return added if added[0] else None


def tool_finished(queue: Queue, event: dict) -> int:
    """PostToolUse / PostToolUseFailure: this agent moved, or the awaited call completed."""
    ctx = Context(event)
    if not ctx.session:
        return 0
    tool = text.plain(event.get("tool_name"), config.MAX_ID)
    digest = None   # computed only when something could match it

    def same_call(expected_tool: str, expected_digest: str) -> bool:
        nonlocal digest
        if not expected_tool or expected_tool != tool or not expected_digest:
            return False
        if digest is None:
            digest = tool_digest(event)
        return digest == expected_digest

    def match(item) -> bool:
        return (
            item.kind == "waiting"
            and item.session == ctx.session
            and (item.agent == ctx.agent or same_call(item.tool, item.digest))
        )

    # Read without the lock first: almost every tool call finds nothing to close.
    state = queue.load()
    closed = 0
    if any(match(i) for i in state.open_items()):
        closed = queue.close_where(match, "moved")
    note = state.notes.get(ctx.session)
    if note and same_call(note["tool"], note["digest"]):
        queue.note(ctx.session, "", "", "")   # answered: forget the request
    return closed


def agent_moved(queue: Queue, event: dict, every_agent: bool = False, kinds=("waiting",)) -> int:
    """Close this session's items of `kinds` that belong to the agent the event came from."""
    ctx = Context(event)
    if not ctx.session:
        return 0

    def match(item) -> bool:
        if item.session != ctx.session or item.kind not in kinds:
            return False
        return item.kind != "waiting" or every_agent or item.agent == ctx.agent

    if not any(match(i) for i in queue.load().open_items()):
        return 0
    return queue.close_where(match, "moved")
