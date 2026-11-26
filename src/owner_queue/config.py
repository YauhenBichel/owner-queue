"""Settings: defaults, then ~/.claude/owner-queue.json, then environment.

Config file example (all keys optional):

    {
      "dir": "~/.claude/owner-queue",
      "idle": true,
      "ntfy": true, "ntfy_level": "minimal",
      "ntfy_interval": 300, "ntfy_waiting_delay": 120
    }

Environment variables use the same names, upper case, with an OWNER_QUEUE_
prefix: OWNER_QUEUE_DIR, OWNER_QUEUE_NTFY=off and so on. A hook, the CLI in a
plain terminal and a status line command read the same settings.

The size and time limits below are constants: they bound what the queue stores.
"""

from __future__ import annotations

import json
import os

# Sizes are in bytes of UTF-8, not characters.
MAX_TEXT = 400          # excerpt of an assistant message, or a manual note
MAX_MESSAGE = 300       # a notification message
MAX_TITLE = 200         # a pull request title
MAX_URL = 500           # longer URLs are dropped, not cut (a cut URL is a wrong URL)
MAX_URLS = 5
MAX_PATH = 400          # working directory
MAX_ID = 100            # session id, agent id, tool name
MAX_LINE = 4096         # one event in the log; longer lines are ignored on replay
MAX_STDIN = 8 * 1024 * 1024     # hook input; larger input is not parsed
MAX_LOG_READ = 16 * 1024 * 1024  # only the tail of a log larger than this is replayed
TRANSCRIPT_TAIL = 64 * 1024     # bytes read from the end of a transcript

COMPACT_BYTES = 256 * 1024      # compact the log when it is larger than this
MAX_OPEN = 500                  # hooks stop adding when this many items are open
KEEP_DONE = 200                 # closed items kept (they stop a PR being queued twice)
KEEP_DONE_SECONDS = 30 * 86400
WAITING_TTL = 24 * 3600         # a "waiting" item nobody closed expires after this
REVIEW_TTL = 14 * 86400
NOTE_TTL = 600                  # a permission request is matched to its notification within this

HOOK_LOCK_WAIT = 2.0            # seconds a hook waits for the lock before giving up
CLI_LOCK_WAIT = 10.0
GH_TIMEOUT = 15                 # seconds for one `gh pr view`
MAX_SYNC = 50                   # pull requests checked by one `sync`


DEFAULTS = {
    "dir": "",                   # the queue directory; empty = ~/.claude/owner-queue
    "disable": False,            # true: every hook is a no-op
    "idle": True,                # false: "session is idle" notifications do not become items
    "gh_hosts": "github.com",    # hosts `sync` may ask about, comma-separated
    "ntfy": True,                # false: never push; true: push when a topic file exists
    "ntfy_topic_file": "",       # empty = ~/.config/owner-queue/ntfy-topic
    "ntfy_url": "https://ntfy.sh",
    "ntfy_level": "minimal",     # "minimal": kinds and counts only; "detail": repo, number, title, link
    "ntfy_interval": 300.0,      # at most one push in this many seconds
    "ntfy_waiting_delay": 120.0,  # a waiting item is pushed only if still open after this
    "ntfy_timeout": 8.0,         # seconds for one push
}
_LIMITS = {"ntfy_interval": (0.0, 86400.0), "ntfy_waiting_delay": (0.0, 86400.0), "ntfy_timeout": (0.5, 60.0)}
_FALSE = ("0", "no", "off", "false", "")


def home() -> str:
    return os.environ.get("HOME") or os.path.expanduser("~")


def load(path: "str | None" = None) -> dict:
    """Every setting, with a usable value whatever the file or the environment holds."""
    path = path or os.environ.get("OWNER_QUEUE_CONFIG") or os.path.join(home(), ".claude", "owner-queue.json")
    data = {}
    try:
        with open(path, "rb") as f:
            loaded = json.loads(f.read(65536).decode("utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    settings = {}
    for key, default in DEFAULTS.items():
        env = os.environ.get("OWNER_QUEUE_" + key.upper())
        raw = env if env not in (None, "") else data.get(key)
        settings[key] = _coerce(key, default, raw)
    return settings


def _coerce(key: str, default, raw):
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw if isinstance(raw, bool) else str(raw).strip().lower() not in _FALSE
    if isinstance(default, float):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return default
        low, high = _LIMITS[key]
        return max(low, min(high, value)) if value == value else default
    return raw.strip() if isinstance(raw, str) else default


def queue_dir(settings: "dict | None" = None) -> str:
    """The queue directory: the `dir` setting, or ~/.claude/owner-queue."""
    explicit = (settings or load())["dir"]
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return os.path.join(home(), ".claude", "owner-queue")
