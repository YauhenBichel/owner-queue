"""Shared test scaffolding: every test runs in its own temporary home and queue directory.

Nothing here touches the real ~/.claude, the real `gh`, a browser or the network.
All repositories, sessions and tokens in the tests are made up.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# By default the tests run the source in this folder, the way the plugin does. With
# OWNER_QUEUE_TEST_INSTALLED=1 they run the owner_queue installed in this Python instead:
# that is how a built wheel is tested before a release (see RELEASING.md).
INSTALLED = os.environ.get("OWNER_QUEUE_TEST_INSTALLED") == "1"
if not INSTALLED:
    sys.path.insert(0, str(ROOT / "src"))

from owner_queue import config  # noqa: E402
from owner_queue.store import Queue  # noqa: E402

BIN = str(ROOT / "bin" / "owner-queue")
CMD = [sys.executable, "-m", "owner_queue"] if INSTALLED else [sys.executable, BIN]
PR = "https://github.com/example/repo/pull/"

FAKE_GH = """#!/bin/sh
# A stand-in for gh: answers from the URL alone and records how it was called.
echo "$*" >> "$FAKE_GH_LOG"
case "$3" in
  */pull/1) echo '{"state":"MERGED","mergedAt":"2026-01-01T00:00:00Z"}' ;;
  */pull/2) echo '{"state":"CLOSED","mergedAt":null}' ;;
  */pull/3) echo '{"state":"OPEN","mergedAt":null}' ;;
  *) echo "could not resolve to a PullRequest" >&2; exit 1 ;;
esac
"""
FAKE_OPENER = """#!/bin/sh
echo "$*" >> "$FAKE_OPEN_LOG"
"""


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="oq-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.dir = os.path.join(self.tmp, "queue")
        self.fakebin = os.path.join(self.tmp, "bin")
        os.makedirs(self.home)
        os.makedirs(self.fakebin)
        self.gh_log = os.path.join(self.tmp, "gh.log")
        self.open_log = os.path.join(self.tmp, "open.log")
        self._script("opener", FAKE_OPENER)
        self.env = {
            "PATH": self.fakebin,   # no real gh, no real open
            "HOME": self.home,
            "OWNER_QUEUE_DIR": self.dir,
            "OWNER_QUEUE_NTFY": "off",
            "OWNER_QUEUE_OPENER": os.path.join(self.fakebin, "opener"),
            "FAKE_GH_LOG": self.gh_log,
            "FAKE_OPEN_LOG": self.open_log,
        }
        self.clock = Clock()

    def _script(self, name: str, body: str) -> str:
        path = os.path.join(self.fakebin, name)
        with open(path, "w") as f:
            f.write(body.replace("#!/bin/sh", "#!/bin/sh\nPATH=/usr/bin:/bin", 1))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def fake_gh(self) -> None:
        self._script("gh", FAKE_GH)

    def queue(self) -> Queue:
        return Queue(self.dir, clock=self.clock)

    def settings(self, **changes) -> dict:
        settings = dict(config.DEFAULTS)
        settings.update(changes)
        return settings

    def cli(self, *args, stdin: bytes = b"", env: "dict | None" = None, timeout: float = 30):
        full = dict(self.env)
        full.update(env or {})
        return subprocess.run(
            [*CMD, *args], input=stdin, env=full, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def out(self, *args, **kwargs) -> str:
        done = self.cli(*args, **kwargs)
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        return done.stdout.decode()

    def hook(self, event, env: "dict | None" = None):
        raw = event if isinstance(event, bytes) else json.dumps(event).encode()
        done = self.cli("hook", stdin=raw, env=env)
        self.assertEqual(done.returncode, 0, "a hook must always exit 0")
        self.assertEqual(done.stdout, b"", "a hook must print nothing on stdout")
        return done

    def items(self) -> list:
        return Queue(self.dir).load().open_items()

    def log(self) -> str:
        try:
            with open(os.path.join(self.dir, "events.jsonl"), encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return ""


# -- hook input, shaped as the Claude Code hooks reference documents it --------

def common(name: str, session: str = "0f3c1a2b-1111-4222-8333-444455556666", cwd: str = "/work/alpha/app", **extra):
    event = {
        "session_id": session,
        "transcript_path": f"/nonexistent/projects/x/{session}.jsonl",
        "cwd": cwd,
        "permission_mode": "default",
        "hook_event_name": name,
    }
    event.update(extra)
    return event


def bash_done(command: str, stdout: str = "", **extra):
    return common(
        "PostToolUse", tool_name="Bash",
        tool_input={"command": command, "description": "run a command"},
        tool_response={"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False},
        tool_use_id="toolu_01ABCDEFGHJKMNPQRSTVWXYZ", **extra,
    )


def notification(ntype: str = "permission_prompt", message: str = "Claude needs your permission to use Bash", **extra):
    return common("Notification", message=message, title="Permission needed", notification_type=ntype, **extra)


def stop(message: str, **extra):
    return common("Stop", stop_hook_active=False, last_assistant_message=message, **extra)
