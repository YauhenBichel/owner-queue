"""The queue: an append-only event log, a file lock, and state derived by replay.

    <queue dir>/events.jsonl   one JSON object per line: add / done / skip / reopen / note
    <queue dir>/lock           held (flock) while a line is appended or the log is compacted
    <queue dir>/cursor         id of the item `next` last showed

Nothing else is the source of truth. Reading takes no lock: a line is appended
with one write and compaction replaces the file with a rename, so a reader sees
either the old log or the new one. A line that cannot be read is skipped and
counted; it never hides the lines around it.

Files are opened without following symlinks and must be regular files, so a
link or a pipe planted in the queue directory is refused instead of followed.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time

from . import config

LOG = "events.jsonl"
LOCK = "lock"
CURSOR = "cursor"

OPEN, DONE = "open", "done"
KINDS = ("pr", "waiting", "review", "manual")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_STR_FIELDS = ("kind", "key", "text", "cwd", "session", "agent", "url", "repo", "title", "ntype", "tool", "digest")


class QueueError(Exception):
    """The queue could not be read or written; the message says why."""


class Busy(QueueError):
    pass


class Item:
    __slots__ = ("id", "ts", "data", "state", "order", "skips", "by", "done_ts")

    def __init__(self, item_id: str, ts: float, data: dict, order: int):
        self.id = item_id
        self.ts = ts
        self.data = data
        self.state = OPEN
        self.order = order
        self.skips = 0
        self.by = ""
        self.done_ts = 0.0

    def __getattr__(self, name: str):
        # Only reached for names that are not slots: the item's own fields.
        if name == "urls":
            return self.data.get("urls") or []
        if name == "number":
            return self.data.get("number") or 0
        if name in _STR_FIELDS:
            return self.data.get(name) or ""
        raise AttributeError(name)


class State:
    """What the log says, after replaying it."""

    def __init__(self):
        self.items: dict = {}
        self.notes: dict = {}      # session id -> the last permission request seen there
        self.lines = 0
        self.corrupt = 0
        self._seq = 0

    # -- replay ---------------------------------------------------------

    def feed(self, raw: bytes) -> None:
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            self.lines += 1
            if len(line) > config.MAX_LINE * 2 or not self._apply_line(line):
                self.corrupt += 1

    def _apply_line(self, line: bytes) -> bool:
        try:
            event = json.loads(line.decode("utf-8"))
            return self.apply(event)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return False

    def apply(self, event: dict) -> bool:
        if not isinstance(event, dict):
            return False
        op = event.get("op")
        ts = event.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return False
        self._seq += 1
        if op == "note":
            session = event.get("session")
            if not isinstance(session, str) or not session:
                return False
            if not _text(event.get("tool")):
                self.notes.pop(session, None)   # the request was answered
                return True
            self.notes[session] = {
                "ts": float(ts),
                "agent": _text(event.get("agent")),
                "tool": _text(event.get("tool")),
                "digest": _text(event.get("digest")),
            }
            return True
        item_id = event.get("id")
        if not isinstance(item_id, str) or not item_id or len(item_id) > 32:
            return False
        item = self.items.get(item_id)
        if op == "add":
            data = _item_data(event.get("item"))
            if data is None:
                return False
            if item is None:
                item = self.items[item_id] = Item(item_id, float(ts), data, self._seq)
                skips = event.get("skips")
                if isinstance(skips, int) and not isinstance(skips, bool) and 0 <= skips < 10**6:
                    item.skips = skips
            elif item.state == OPEN:
                item.data = data          # a newer version of the same item keeps its place
            return True
        if op not in ("done", "skip", "reopen"):
            return False
        if item is None:
            return True                   # about an item that was compacted away: harmless
        if op == "done" and item.state == OPEN:
            item.state, item.by, item.done_ts = DONE, _text(event.get("by")) or "owner", float(ts)
        elif op == "skip" and item.state == OPEN:
            item.order, item.skips = self._seq, item.skips + 1
        elif op == "reopen" and item.state == DONE:
            item.state, item.by, item.done_ts, item.order = OPEN, "", 0.0, self._seq
        return True

    def expire(self, now: float) -> None:
        """Items a dead session left behind close by themselves; old notes are forgotten."""
        for item in self.items.values():
            ttl = {"waiting": config.WAITING_TTL, "review": config.REVIEW_TTL}.get(item.kind)
            if ttl and item.state == OPEN and now - item.ts > ttl:
                item.state, item.by, item.done_ts = DONE, "expired", item.ts + ttl
        for session in [s for s, n in self.notes.items() if now - n["ts"] > config.NOTE_TTL]:
            del self.notes[session]

    # -- queries --------------------------------------------------------

    def open_items(self) -> list:
        """Open items, oldest first; a skipped item goes to the back."""
        return sorted((i for i in self.items.values() if i.state == OPEN), key=lambda i: i.order)

    def by_key(self, key: str, only_open: bool = True):
        for item in self.items.values():
            if item.key == key and (item.state == OPEN or not only_open):
                return item
        return None

    def find(self, prefix: str):
        """The item with this id, or the only one whose id starts with it."""
        if prefix in self.items:
            return self.items[prefix]
        matches = [i for i in self.items.values() if i.id.startswith(prefix)] if prefix else []
        if len(matches) > 1:
            open_matches = [i for i in matches if i.state == OPEN]
            matches = open_matches if len(open_matches) == 1 else matches
        return matches[0] if len(matches) == 1 else None

    def retained(self, now: float) -> list:
        """Items a compaction keeps: every open one, and recently closed ones."""
        closed = [
            i for i in self.items.values()
            if i.state == DONE and now - i.done_ts <= config.KEEP_DONE_SECONDS
        ]
        closed = sorted(closed, key=lambda i: i.done_ts)[-config.KEEP_DONE:]
        return sorted(self.open_items() + closed, key=lambda i: i.order)


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _item_data(raw) -> "dict | None":
    """Keep only known fields with the expected types; anything else is dropped."""
    if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
        return None
    data = {}
    for name in _STR_FIELDS:
        value = raw.get(name)
        if isinstance(value, str) and value:
            data[name] = value
    urls = raw.get("urls")
    if isinstance(urls, list):
        urls = [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))]
        if urls:
            data["urls"] = urls[: config.MAX_URLS]
    number = raw.get("number")
    if isinstance(number, int) and not isinstance(number, bool) and 0 < number < 10**12:
        data["number"] = number
    return data


class Queue:
    def __init__(self, directory: "str | None" = None, clock=time.time):
        self.directory = directory or config.queue_dir()
        self.clock = clock

    def _path(self, name: str) -> str:
        return os.path.join(self.directory, name)

    # -- reading --------------------------------------------------------

    def load(self) -> State:
        """Replay the log. A missing queue is an empty queue."""
        state = State()
        state.feed(self._read(LOG, config.MAX_LOG_READ))
        state.expire(self.clock())
        return state

    def _read(self, name: str, limit: int) -> bytes:
        try:
            fd = os.open(self._path(name), os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW)
        except FileNotFoundError:
            return b""
        except OSError as exc:
            raise QueueError(f"cannot read {name}: {exc.strerror or exc}") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise QueueError(f"{name} is not a regular file")
            start = max(0, info.st_size - limit)
            if start:
                os.lseek(fd, start, os.SEEK_SET)
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            if start:  # the first line is cut in the middle
                raw = raw[raw.find(b"\n") + 1:]
            return raw
        except OSError as exc:
            raise QueueError(f"cannot read {name}: {exc.strerror or exc}") from None
        finally:
            os.close(fd)

    # -- writing --------------------------------------------------------

    def change(self, decide, wait: float = config.CLI_LOCK_WAIT):
        """Run `decide(state, now)` under the lock and append the events it returns.

        `decide` returns (events, result). The log is replayed inside the lock, so
        two processes deciding at the same moment cannot both add the same item.
        """
        fd = self._lock(wait)
        try:
            raw = self._read(LOG, config.MAX_LOG_READ)
            state = State()
            state.feed(raw)
            now = self.clock()
            state.expire(now)
            events, result = decide(state, now)
            if events:
                lines = []
                for event in events:
                    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    if len(line) > config.MAX_LINE:
                        raise QueueError("item too large")
                    lines.append(line)
                    state.apply(event)
                    state.lines += 1
                # If the last writer died mid-line, start on a fresh line so only that line is lost.
                torn = bool(raw) and not raw.endswith(b"\n")
                self._append((b"\n" if torn else b"") + b"\n".join(lines) + b"\n")
                size = len(raw) + sum(len(line) + 1 for line in lines)
                kept = state.retained(now)
                if size > config.COMPACT_BYTES and state.lines > 2 * len(kept) + 16:
                    self._rewrite(state, kept, now)
            return result
        finally:
            os.close(fd)   # closing the descriptor releases the lock

    def compact(self) -> int:
        """Rewrite the log as one line per kept item. Returns the number of items kept."""
        fd = self._lock(config.CLI_LOCK_WAIT)
        try:
            state = State()
            state.feed(self._read(LOG, config.MAX_LOG_READ))
            now = self.clock()
            state.expire(now)
            kept = state.retained(now)
            self._rewrite(state, kept, now)
            return len(kept)
        finally:
            os.close(fd)

    def _lock(self, wait: float, name: str = LOCK) -> int:
        """An exclusive lock on a file in the queue directory; closing the descriptor releases it."""
        try:
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
            fd = os.open(self._path(name), os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | _NOFOLLOW, 0o600)
        except OSError as exc:
            raise QueueError(f"cannot use {self.directory}: {exc.strerror or exc}") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise QueueError(f"{name} is not a regular file")
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Busy("the queue is busy (another process holds the lock)") from None
                    time.sleep(0.004)
        except BaseException:
            os.close(fd)
            raise

    def _append(self, data: bytes) -> None:
        try:
            fd = os.open(
                self._path(LOG), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | _NOFOLLOW, 0o600
            )
        except OSError as exc:
            raise QueueError(f"cannot write {LOG}: {exc.strerror or exc}") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise QueueError(f"{LOG} is not a regular file")
            _write_all(fd, data)
        except OSError as exc:
            raise QueueError(f"cannot write {LOG}: {exc.strerror or exc}") from None
        finally:
            os.close(fd)

    def _rewrite(self, state: State, kept: list, now: float) -> None:
        lines = []
        for item in kept:
            lines.append({"op": "add", "id": item.id, "ts": item.ts, "item": item.data, "skips": item.skips})
            if item.state == DONE:
                lines.append({"op": "done", "id": item.id, "ts": item.done_ts, "by": item.by})
        for session, note in state.notes.items():
            lines.append(dict(note, op="note", session=session))
        body = b"".join(
            json.dumps(e, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n" for e in lines
        )
        self._replace(LOG, body)

    def _replace(self, name: str, body: bytes) -> None:
        """Write a new file next to `name` and rename it into place."""
        tmp = self._path(f".{name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
            try:
                _write_all(fd, body)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self._path(name))   # replaces a symlink itself, never its target
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise QueueError(f"cannot write {name}: {exc.strerror or exc}") from None

    # Used by the notifier for its own small file and locks in the same directory.
    def read_file(self, name: str, limit: int) -> bytes:
        return self._read(name, limit)

    def replace_file(self, name: str, body: bytes) -> None:
        try:
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise QueueError(f"cannot use {self.directory}: {exc.strerror or exc}") from None
        self._replace(name, body)

    def lock(self, wait: float, name: str) -> int:
        return self._lock(wait, name)

    # -- operations -----------------------------------------------------

    def add(self, data: dict, wait: float = config.CLI_LOCK_WAIT, limit: bool = False):
        """Add an item, or refresh the open item with the same key.

        Returns (id, created). A pull request that was ever queued (open or
        recently closed) is not queued again. With `limit`, nothing is added
        once MAX_OPEN items are open, and the id is "".
        """
        def decide(state: State, now: float):
            key = data.get("key") or ""
            if key:
                existing = state.by_key(key, only_open=data.get("kind") != "pr")
                if existing is not None:
                    # A pull request keeps what its first report recorded.
                    if existing.state == DONE or existing.data == data or data.get("kind") == "pr":
                        return [], (existing.id, False)
                    return [{"op": "add", "id": existing.id, "ts": _stamp(now), "item": data}], (existing.id, False)
            if limit and len(state.open_items()) >= config.MAX_OPEN:
                return [], ("", False)
            item_id = os.urandom(3).hex()
            while item_id in state.items:
                item_id = os.urandom(3).hex()
            return [{"op": "add", "id": item_id, "ts": _stamp(now), "item": data}], (item_id, True)

        return self.change(decide, wait)

    def mark(self, op: str, item_id: str, by: str = "owner") -> bool:
        """Append done / skip / reopen for one item. False when it changes nothing."""
        def decide(state: State, now: float):
            item = state.items.get(item_id)
            if item is None or (item.state == OPEN) == (op == "reopen"):
                return [], False
            event = {"op": op, "id": item_id, "ts": _stamp(now)}
            if op == "done":
                event["by"] = by
            return [event], True

        return self.change(decide)

    def close_where(self, match, by: str, wait: float = config.HOOK_LOCK_WAIT) -> int:
        """Close every open item `match(item)` accepts. Returns how many."""
        def decide(state: State, now: float):
            ids = [i.id for i in state.open_items() if match(i)]
            return [{"op": "done", "id": i, "ts": _stamp(now), "by": by} for i in ids], len(ids)

        return self.change(decide, wait)

    def note(self, session: str, agent: str, tool: str, digest: str) -> None:
        def decide(state: State, now: float):
            event = {"op": "note", "ts": _stamp(now), "session": session, "agent": agent, "tool": tool, "digest": digest}
            return [event], None

        self.change(decide, config.HOOK_LOCK_WAIT)

    # -- the item `next` last showed --------------------------------------

    def shown(self) -> str:
        try:
            value = self._read(CURSOR, 64).decode("ascii", "replace").strip()
        except QueueError:
            return ""
        return value if value.isalnum() else ""

    def set_shown(self, item_id: str) -> None:
        try:
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
            self._replace(CURSOR, item_id.encode("ascii", "replace") + b"\n")
        except (QueueError, OSError):
            pass   # only a convenience: `done` then asks for an id


def _stamp(now: float) -> float:
    return round(now, 3)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]
