"""Phone notifications through ntfy: off unless a topic file exists.

The topic is the only secret: anyone who knows it can read the pushes. It is
read from a file, used in one request URL and never printed, logged or stored
anywhere else; error messages are scrubbed of it.

Hooks never come here. A hook that queued a new item starts `owner-queue
notify --wait` as a detached process and returns; that process does the
waiting and the one HTTP request.

Not flooding the phone:
  * one push at most every `ntfy_interval` seconds, as a digest of what is new;
  * a "waiting" item is pushed only if it is still open after
    `ntfy_waiting_delay` seconds (most prompts are answered at the desk);
  * an item is pushed once; items the owner added by hand are never pushed;
  * a failed push is not retried in a loop: it uses up the interval like a
    successful one, and the items go out with the next digest.

Privacy: with level "minimal" (the default) a push holds kinds and counts only.
Level "detail" adds repository, number and title of pull requests and makes the
notification open the pull request.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .store import Busy, Queue, QueueError

MEMO = "notify.json"
SEND_LOCK = "notify.lock"
DAEMON_LOCK = "notify-daemon.lock"
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KIND_WORDS = {
    "pr": ("pull request", "pull requests"),
    "waiting": ("session waiting", "sessions waiting"),
    "review": ("session to review", "sessions to review"),
}
MAX_DAEMON_SECONDS = 2 * 3600
MAX_DETAIL_LINES = 5
MAX_REMEMBERED = 2000


class Result:
    def __init__(self, status: str, message: str, next_at: "float | None" = None):
        self.status = status      # sent | failed | limited | idle | off
        self.message = message
        self.next_at = next_at    # when something may be due, if known

    @property
    def ok(self) -> bool:
        return self.status != "failed"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # never carry the topic to another address


class Notifier:
    def __init__(self, queue: Queue, settings: dict, clock=time.time):
        self.queue = queue
        self.settings = settings
        self.clock = clock

    # -- configuration --------------------------------------------------

    def topic_file(self) -> str:
        explicit = self.settings["ntfy_topic_file"]
        if explicit:
            return os.path.abspath(os.path.expanduser(explicit))
        return os.path.join(config.home(), ".config", "owner-queue", "ntfy-topic")

    def _topic(self) -> str:
        """The topic, or "" when there is no usable topic file."""
        try:
            with open(self.topic_file(), "rb") as f:
                first = f.read(256).split(b"\n", 1)[0].decode("ascii", "replace").strip()
        except OSError:
            return ""
        return first if _TOPIC.match(first) else ""

    def server(self) -> "urllib.parse.SplitResult | None":
        parts = urllib.parse.urlsplit(self.settings["ntfy_url"])
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        return parts

    def why_off(self) -> str:
        """"" when notifications are on, else the reason they are off."""
        if not self.settings["ntfy"]:
            return "switched off (ntfy setting)"
        if not os.path.isfile(self.topic_file()):
            return "no topic file"
        if not self._topic():
            return "the topic file does not hold a valid topic name"
        if self.server() is None:
            return "ntfy_url is not an http(s) address"
        return ""

    def wanted(self) -> bool:
        """Cheap check for hooks: is it worth starting the notifier at all?"""
        return bool(self.settings["ntfy"]) and os.path.isfile(self.topic_file())

    # -- what is due ----------------------------------------------------

    def _memo(self) -> dict:
        memo = {"last_attempt": 0.0, "last_ok": 0.0, "last_error": "", "sent": {}}
        try:
            loaded = json.loads(self.queue.read_file(MEMO, 1 << 20).decode("utf-8"))
        except (QueueError, ValueError):
            return memo
        if isinstance(loaded, dict):
            for key in ("last_attempt", "last_ok"):
                if isinstance(loaded.get(key), (int, float)) and not isinstance(loaded.get(key), bool):
                    memo[key] = float(loaded[key])
            if isinstance(loaded.get("last_error"), str):
                memo["last_error"] = loaded["last_error"][:200]
            if isinstance(loaded.get("sent"), dict):
                memo["sent"] = {k: v for k, v in loaded["sent"].items() if isinstance(k, str) and isinstance(v, (int, float))}
        return memo

    def _save(self, memo: dict) -> None:
        self.queue.replace_file(MEMO, json.dumps(memo, separators=(",", ":")).encode("utf-8") + b"\n")

    def _due(self, state, memo: dict, now: float):
        """(items to push now, time the next waiting item becomes due or None)."""
        delay = self.settings["ntfy_waiting_delay"]
        ready, later = [], None
        for item in state.open_items():
            if item.kind == "manual" or item.id in memo["sent"]:
                continue
            if item.kind == "waiting" and now - item.ts < delay:
                later = min(later or item.ts + delay, item.ts + delay)
                continue
            ready.append(item)
        return ready, later

    # -- sending --------------------------------------------------------

    def run_once(self) -> Result:
        """Send one digest if something is due and the interval has passed."""
        off = self.why_off()
        if off:
            return Result("off", "notifications are off: " + off)
        try:
            lock = self.queue.lock(2.0, SEND_LOCK)
        except Busy:
            return Result("limited", "another notifier is sending", self.clock() + 1.0)
        try:
            now = self.clock()
            state = self.queue.load()
            memo = self._memo()
            # Forget items that no longer exist, so the memo cannot grow for ever.
            memo["sent"] = {k: v for k, v in memo["sent"].items() if k in state.items}
            if len(memo["sent"]) > MAX_REMEMBERED:
                newest = sorted(memo["sent"].items(), key=lambda kv: kv[1])[-MAX_REMEMBERED:]
                memo["sent"] = dict(newest)
            ready, later = self._due(state, memo, now)
            if not ready:
                return Result("idle", "nothing to send", later)
            allowed = memo["last_attempt"] + self.settings["ntfy_interval"]
            if now < allowed and now >= memo["last_attempt"]:
                wait = int(allowed - now) + 1
                return Result("limited", f"{len(ready)} waiting to be sent; next push in {wait}s", allowed)
            # Record the attempt before the request: whatever happens next, this
            # interval is used up, so a broken server cannot cause a storm.
            memo["last_attempt"] = now
            self._save(memo)
            title, body, click = self.compose(ready, len(state.open_items()))
            error = self._post(title, body, click)
            if error:
                memo["last_error"] = error
                self._save(memo)
                return Result("failed", "push failed: " + error)
            for item in ready:
                memo["sent"][item.id] = now
            memo["last_ok"], memo["last_error"] = now, ""
            self._save(memo)
            return Result("sent", f"sent: {title}")
        finally:
            os.close(lock)

    def compose(self, items: list, open_total: int):
        """(title, body, click URL or "") for a digest of `items`."""
        counts = {}
        for item in items:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        parts = []
        for kind in ("pr", "waiting", "review"):
            n = counts.get(kind, 0)
            if n:
                parts.append(f"{n} {_KIND_WORDS[kind][n > 1]}")
        title = f"owner-queue: {len(items)} new"
        body = ", ".join(parts) + f". {open_total} open in total."
        click = ""
        if self.settings["ntfy_level"] == "detail":
            prs = [i for i in items if i.kind == "pr"]
            lines = []
            for item in prs[:MAX_DETAIL_LINES]:
                line = f"{item.repo}#{item.number}" if item.repo else item.url
                lines.append(line + (f" {item.title}" if item.title else ""))
            if len(prs) > MAX_DETAIL_LINES:
                lines.append(f"and {len(prs) - MAX_DETAIL_LINES} more")
            if lines:
                body += "\n" + "\n".join(lines)
            if prs and prs[0].url.startswith("https://"):
                click = prs[0].url
        return title, body, click

    def _post(self, title: str, body: str, click: str = "") -> str:
        """One POST to the topic. Returns "" on success, else a short reason without the topic."""
        topic = self._topic()
        server = self.server()
        if not topic or server is None:
            return "not configured"
        url = urllib.parse.urlunsplit(
            (server.scheme, server.netloc, server.path.rstrip("/") + "/" + topic, "", "")
        )
        headers = {"Title": _header(title), "Tags": "inbox_tray", "Content-Type": "text/plain; charset=utf-8"}
        if click:
            headers["Click"] = _header(urllib.parse.quote(click, safe=":/?&=#%@+,;~.-_"))
        request = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=self.settings["ntfy_timeout"]) as response:
                status = response.status
                response.read(1024)
        except urllib.error.HTTPError as exc:
            return f"the server answered {exc.code}"
        except Exception as exc:  # URLError, timeouts, TLS and socket errors, bad addresses
            reason = getattr(exc, "reason", None) or exc
            return _scrub(f"{type(exc).__name__}: {reason}", topic)
        return "" if 200 <= status < 300 else f"the server answered {status}"

    def test(self) -> Result:
        off = self.why_off()
        if off:
            return Result("off", "notifications are off: " + off)
        error = self._post("owner-queue: test", "Test notification from owner-queue. Nothing to do.")
        if error:
            return Result("failed", "push failed: " + error)
        return Result("sent", "test notification sent")

    def status(self) -> str:
        off = self.why_off()
        server = self.server()
        memo = self._memo()
        now = self.clock()
        try:
            ready, later = self._due(self.queue.load(), memo, now)
        except QueueError:
            ready, later = [], None
        lines = [
            "notifications: " + ("on" if not off else "off (" + off + ")"),
            "server: " + (server.hostname if server else "(invalid)"),
            "topic file: " + ("found" if os.path.isfile(self.topic_file()) else "not found") + f" ({_tilde(self.topic_file())})",
            "level: " + ("detail" if self.settings["ntfy_level"] == "detail" else "minimal"),
            f"interval: {self.settings['ntfy_interval']:g}s, waiting delay: {self.settings['ntfy_waiting_delay']:g}s",
            "last push: " + (_ago(now - memo["last_ok"]) if memo["last_ok"] else "never"),
            f"due now: {len(ready)}" + (", more after the waiting delay" if later else ""),
        ]
        if memo["last_error"]:
            lines.append("last error: " + memo["last_error"])
        return "\n".join(lines)

    # -- the detached process ---------------------------------------------

    def serve(self, sleep=time.sleep) -> Result:
        """Stay until everything due has been sent (or one push failed), then exit."""
        try:
            daemon = self.queue.lock(1.0, DAEMON_LOCK)
        except QueueError:
            return Result("limited", "another notifier is already waiting")
        try:
            end = time.monotonic() + MAX_DAEMON_SECONDS
            result = Result("idle", "nothing to send")
            while time.monotonic() < end:
                result = self.run_once()
                if result.status == "sent":
                    continue          # more may have arrived meanwhile
                if result.status in ("failed", "off") or result.next_at is None:
                    break
                pause = result.next_at - self.clock()
                sleep(max(0.05, min(pause, 30.0)))
            return result
        finally:
            os.close(daemon)


def _header(value: str) -> str:
    """HTTP headers are one line of Latin-1; ours are ASCII."""
    return value.encode("ascii", "replace").decode("ascii").replace("\r", " ").replace("\n", " ")[:300]


def _scrub(message: str, topic: str) -> str:
    return message.replace(topic, "[topic]")[:200] if topic else message[:200]


def _tilde(path: str) -> str:
    home = config.home()
    return "~" + path[len(home):] if home and path.startswith(home + os.sep) else path


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def spawn_detached() -> None:
    """Start `owner-queue notify --wait` with no tie to this process, and return at once."""
    package_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)   # the middle process exits immediately
        return
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):   # nothing of ours keeps the hook's pipes open
            os.dup2(null, fd)
        env = dict(os.environ)
        env["PYTHONPATH"] = package_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        os.execve(sys.executable, [sys.executable, "-m", "owner_queue", "notify", "--wait"], env)
    except BaseException:
        pass
    finally:
        os._exit(0)
