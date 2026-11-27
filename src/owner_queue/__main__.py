"""owner-queue: one queue for what your Claude Code sessions leave for you to do.

    owner-queue next              the oldest open item, one only
    owner-queue list [--all] [--json]
    owner-queue done [id]         close an item (default: the one `next` last showed)
    owner-queue skip [id]         move it to the back of the queue
    owner-queue open [id]         open its link in the browser
    owner-queue reopen <id>       put a closed item back
    owner-queue count             number of open items
    owner-queue add "<text>" [--url URL]   add your own item
    owner-queue sync              ask `gh` which queued pull requests were merged or closed
    owner-queue statusline        "queue 3", or nothing when the queue is empty
    owner-queue notify [--test | --status]   phone notifications through ntfy
    owner-queue compact           rewrite the log without closed history
    owner-queue hook              Claude Code hook (reads the hook's JSON on stdin)
    owner-queue --version

An id can be shortened to its first characters. The queue lives in
~/.claude/owner-queue (or $OWNER_QUEUE_DIR). Hooks never fail the session: any
error ends with no output and exit 0.
"""

from __future__ import annotations

import json
import os
import sys

from . import __version__, config
from .store import DONE, OPEN, Queue, QueueError


# -- hooks ------------------------------------------------------------------

def _hook() -> int:
    try:
        settings = config.load()
        if settings["disable"]:
            return 0
        raw = sys.stdin.buffer.read(config.MAX_STDIN + 1)
        if len(raw) > config.MAX_STDIN:
            return 0
        event = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(event, dict):
            return 0
        created = dispatch(Queue(config.queue_dir(settings)), event, settings)
        if created:
            from .notify import Notifier, spawn_detached
            if Notifier(None, settings).wanted():
                spawn_detached()
    except Exception as exc:  # a hook must never break the session
        try:
            print(f"owner-queue: {type(exc).__name__}", file=sys.stderr)
        except Exception:
            pass
    return 0


def dispatch(queue: Queue, event: dict, settings: dict) -> bool:
    """Handle one hook event. True when a new item was queued."""
    from .handlers import waiting

    name = event.get("hook_event_name")
    if name == "PostToolUse":
        waiting.tool_finished(queue, event)
        if event.get("tool_name") == "Bash":
            from .handlers import pr
            return _is_new(queue, pr.run, event)
    elif name == "PostToolUseFailure":
        waiting.tool_finished(queue, event)
    elif name == "PermissionRequest":
        waiting.permission_request(queue, event)
    elif name == "Notification":
        return _is_new(queue, lambda q, e: waiting.notification(q, e, settings["idle"]), event)
    elif name == "SubagentStop":
        if event.get("agent_id"):
            waiting.agent_moved(queue, event)
    elif name == "UserPromptSubmit":
        waiting.agent_moved(queue, event, kinds=("waiting", "review"))
    elif name == "Stop":
        from .handlers import review
        waiting.agent_moved(queue, event)
        return _is_new(queue, review.run, event)
    elif name == "SessionEnd":
        waiting.agent_moved(queue, event, every_agent=True)
    return False


def _is_new(queue: Queue, handler, event: dict) -> bool:
    added = handler(queue, event)   # (item id, newly queued) or None
    return bool(added and added[1])


# -- commands ---------------------------------------------------------------

def _say(*lines: str) -> None:
    for line in lines:
        print(line)


def _fail(message: str, code: int = 1) -> int:
    print(f"owner-queue: {message}", file=sys.stderr)
    return code


def _pick(queue: Queue, state, args: list, want_open: bool = True):
    """The item a command is about: the id given, or the one `next` last showed."""
    if args:
        item = state.find(args[0])
        if item is None:
            return None, f"no item (or more than one) matches '{args[0][:32]}'"
        return item, ""
    shown = queue.shown()
    item = state.items.get(shown) if shown else None
    if item is None:
        return None, "which item? run `owner-queue next` first, or give an id"
    if want_open and item.state != OPEN:
        return None, f"the item you were shown ({item.id}) is already closed; run `owner-queue next`"
    return item, ""


def _next(queue: Queue, args: list) -> int:
    state = queue.load()
    items = state.open_items()
    if not items:
        _say("The queue is empty.")
        return 0
    from . import render
    queue.set_shown(items[0].id)
    _say(render.card(items[0], 1, len(items), queue.clock()))
    if state.corrupt:
        _say(f"(note: {state.corrupt} unreadable line(s) in the queue log were skipped)")
    return 0


def _list(queue: Queue, args: list) -> int:
    from . import render
    state = queue.load()
    items = state.open_items()
    if "--all" in args:
        items = items + sorted((i for i in state.items.values() if i.state == DONE), key=lambda i: i.done_ts)
    if "--json" in args:
        _say(json.dumps([render.as_json(i) for i in items], ensure_ascii=False, indent=1))
        return 0
    if not items:
        _say("The queue is empty.")
        return 0
    now = queue.clock()
    _say(f"{len(state.open_items())} open, oldest first. " + render.NOTICE)
    for item in items:
        _say(render.row(item, now) + (f"  [closed: {item.by}]" if item.state == DONE else ""))
    return 0


def _mark(queue: Queue, op: str, args: list) -> int:
    state = queue.load()
    item, problem = _pick(queue, state, args, want_open=op != "reopen")
    if item is None:
        return _fail(problem)
    if not queue.mark(op, item.id):
        return _fail(f"{item.id} is already {'open' if op == 'reopen' else 'closed'}")
    left = len(queue.load().open_items())
    word = {"done": "closed", "skip": "moved to the back", "reopen": "reopened"}[op]
    _say(f"{item.id} {word}. {left} open.")
    return 0


def _open(queue: Queue, args: list) -> int:
    import subprocess

    item, problem = _pick(queue, queue.load(), args, want_open=False)
    if item is None:
        return _fail(problem)
    if not item.urls:
        return _fail(f"{item.id} has no link")
    url = item.urls[0]
    opener = os.environ.get("OWNER_QUEUE_OPENER") or ("open" if sys.platform == "darwin" else "xdg-open")
    try:
        done = subprocess.run([opener, url], stdin=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return _fail(f"could not run {opener}: {type(exc).__name__}")
    if done.returncode != 0:
        return _fail(f"{opener} exited with {done.returncode}")
    _say(f"opened {url}")
    for other in item.urls[1:]:
        _say(f"also: {other}")
    return 0


def _add(queue: Queue, args: list) -> int:
    from . import text

    url, words = "", []
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--url" and rest:
            url = rest.pop(0)
        elif arg.startswith("--url="):
            url = arg[6:]
        else:
            words.append(arg)
    note = text.field(" ".join(words), config.MAX_TEXT)
    if not note:
        return _fail('usage: owner-queue add "<text>" [--url URL]', 2)
    data = {"kind": "manual", "text": note}
    if url:
        links = text.urls(url, config.MAX_URL, 1)
        if not links or links[0] != url.strip():
            return _fail("--url takes one http(s) link")
        data["urls"] = links
    item_id, _ = queue.add(data)
    _say(f"added {item_id}")
    return 0


def _sync(queue: Queue, settings: dict) -> int:
    from . import sync

    lines, _ = sync.run(queue, settings)
    _say(*lines)
    return 0


def _notify(queue: Queue, settings: dict, args: list) -> int:
    from .notify import Notifier

    notifier = Notifier(queue, settings)
    if "--status" in args:
        _say(notifier.status())
        return 0
    if "--wait" in args:      # the detached process a hook starts: silent
        notifier.serve()
        return 0
    result = notifier.test() if "--test" in args else notifier.run_once()
    _say(result.message)
    return 0 if result.ok else 1


def _statusline(settings: dict) -> int:
    try:
        count = len(Queue(config.queue_dir(settings)).load().open_items())
        if count:
            print(f"queue {count}")
    except Exception:
        pass   # a status line shows nothing rather than an error
    return 0


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        code = _main(argv)
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        # The reader went away (`owner-queue list | head -1`). End quietly; point stdout at
        # nothing so that Python does not complain when it closes it.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 0 if argv[:1] == ["hook"] else 141


def _main(argv: "list[str]") -> int:
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "hook":
        return _hook()
    if cmd in ("--version", "-V", "version"):
        _say(f"owner-queue {__version__}")
        return 0
    if cmd in ("", "help", "--help", "-h"):
        _say(__doc__.strip())
        return 0 if cmd else 2
    settings = config.load()
    if cmd == "statusline":
        return _statusline(settings)
    queue = Queue(config.queue_dir(settings))
    try:
        if cmd == "next":
            return _next(queue, args)
        if cmd == "list":
            return _list(queue, args)
        if cmd in ("done", "skip", "reopen"):
            return _mark(queue, cmd, args)
        if cmd == "open":
            return _open(queue, args)
        if cmd == "count":
            _say(str(len(queue.load().open_items())))
            return 0
        if cmd == "add":
            return _add(queue, args)
        if cmd == "sync":
            return _sync(queue, settings)
        if cmd == "notify":
            return _notify(queue, settings, args)
        if cmd == "compact":
            _say(f"compacted: {queue.compact()} item(s) kept")
            return 0
    except QueueError as exc:
        return _fail(str(exc))
    return _fail(f"unknown command '{cmd[:32]}'; see owner-queue --help", 2)


if __name__ == "__main__":
    sys.exit(main())
