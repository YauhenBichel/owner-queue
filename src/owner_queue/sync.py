"""`owner-queue sync`: close pull request items that were merged or closed.

The only place the queue itself touches the network, and only through the
owner's own `gh`: one `gh pr view <url> --json state,mergedAt` per open pull
request. When `gh` is missing or fails, the items stay and the reason is said.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.parse

from . import config
from .store import Queue


def run(queue: Queue, settings: dict) -> "tuple[list, int]":
    """Returns (lines to print, number of items closed)."""
    hosts = [h.strip().lower() for h in settings["gh_hosts"].split(",") if h.strip()]
    items = [i for i in queue.load().open_items() if i.kind == "pr" and i.url]
    if not items:
        return [], 0
    gh = shutil.which("gh")
    if gh is None:
        return [f"sync: gh not found; kept {len(items)} pull request item(s) as they are"], 0
    lines, closed, failed, skipped = [], 0, 0, 0
    for item in items[: config.MAX_SYNC]:
        if (urllib.parse.urlsplit(item.url).hostname or "") not in hosts:
            skipped += 1
            continue
        state = _state(gh, item.url)
        if state is None:
            failed += 1
        elif state in ("MERGED", "CLOSED"):
            if queue.mark("done", item.id, by="sync"):
                closed += 1
                lines.append(f"sync: {item.repo}#{item.number} is {state.lower()}; closed {item.id}")
    if failed:
        lines.append(f"sync: gh failed for {failed} pull request(s); kept them")
    if skipped:
        lines.append(f"sync: {skipped} pull request(s) on other hosts not checked (gh_hosts)")
    if len(items) > config.MAX_SYNC:
        lines.append(f"sync: checked the oldest {config.MAX_SYNC} of {len(items)} pull requests")
    return lines, closed


def _state(gh: str, url: str) -> "str | None":
    """MERGED, CLOSED or OPEN; None when gh could not say."""
    env = dict(os.environ, GH_PROMPT_DISABLED="1", NO_COLOR="1")
    try:
        done = subprocess.run(
            [gh, "pr", "view", url, "--json", "state,mergedAt"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=config.GH_TIMEOUT, env=env,
        )
        if done.returncode != 0:
            return None
        data = json.loads(done.stdout.decode("utf-8", "replace"))
        state = data.get("state")
        if data.get("mergedAt"):
            return "MERGED"
        return state.upper() if isinstance(state, str) else None
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None
