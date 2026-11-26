"""Text captured from other sessions: clean it, cap it, find links, hide secrets.

Everything a hook stores passes through here first. Captured text is data: it is
stripped of control characters (so it cannot move a terminal cursor or hide
itself), cut to a byte limit and scanned for things that look like credentials.
"""

from __future__ import annotations

import re

# C0/C1 controls, DEL, zero-width and bidirectional controls, BOM.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]")
_SPACES = re.compile(r"\s+")

URL = re.compile(r"https?://[^\s<>\"'`()\[\]{}\\^|]+")
_URL_TAIL = ".,;:!?*_~"
PR_URL = re.compile(r"https://([A-Za-z0-9.-]+)/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)(?![\w-])")

REDACTED = "[redacted]"
_SECRET_QUERY = re.compile(
    r"(?i)([?&][^=&#\s]*(?:token|key|secret|sig|signature|password|passwd|auth|code|credential)[^=&#\s]*=)[^&#\s]+"
)
_SECRETS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}"),
    re.compile(r"\bpypi-[A-Za-z0-9_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
]
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}")
_ASSIGNED = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|credential)s?"
    r"[A-Za-z0-9_.-]*)(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_USERINFO = re.compile(r"(https?://)[^/\s@]+@")
# A long run of mixed letters and digits with no spaces is more likely a key than prose.
_LONG = re.compile(r"(?<![A-Za-z0-9/_+=.-])[A-Za-z0-9/_+=-]{40,}(?![A-Za-z0-9/_+=-])")


def clean(value: object) -> str:
    """One line of printable text: controls removed, whitespace collapsed."""
    if not isinstance(value, str):
        return ""
    return _SPACES.sub(" ", _CONTROL.sub(" ", value)).strip()


def cap(value: str, limit: int, keep_end: bool = False) -> str:
    """Cut to at most `limit` bytes of UTF-8 without splitting a character."""
    raw = value.encode("utf-8", "replace")
    if len(raw) <= limit:
        return value
    mark = "\u2026"
    room = max(0, limit - len(mark.encode()))
    if keep_end:
        return mark + raw[len(raw) - room:].decode("utf-8", "ignore").lstrip()
    return raw[:room].decode("utf-8", "ignore").rstrip() + mark


def redact(value: str) -> str:
    """Replace anything that looks like a credential."""
    value = _USERINFO.sub(r"\1" + REDACTED + "@", value)
    value = _SECRET_QUERY.sub(r"\1" + REDACTED, value)
    value = _BEARER.sub(lambda m: m.group(1) + " " + REDACTED, value)
    value = _ASSIGNED.sub(lambda m: m.group(1) + m.group(2) + REDACTED, value)
    for pattern in _SECRETS:
        value = pattern.sub(REDACTED, value)
    return _LONG.sub(_long, value)


def _long(match: "re.Match[str]") -> str:
    word = match.group(0)
    mixed = any(c.isdigit() for c in word) and any(c.isalpha() for c in word)
    return REDACTED if mixed else word


def field(value: object, limit: int, keep_end: bool = False) -> str:
    """Clean, redact and cap one stored field."""
    return cap(redact(clean(value)), limit, keep_end)


def plain(value: object, limit: int) -> str:
    """An identifier or path: cleaned and dropped (not cut) when too long."""
    value = clean(value)
    return value if len(value.encode("utf-8", "replace")) <= limit else ""


def urls(value: object, limit_each: int, limit_count: int) -> list:
    """Distinct http(s) links in the text, in order, redacted; over-long ones are dropped."""
    if not isinstance(value, str):
        return []
    found = []
    for match in URL.finditer(_CONTROL.sub(" ", value)):
        url = redact(match.group(0).rstrip(_URL_TAIL))
        if len(url) < 12 or len(url.encode("utf-8", "replace")) > limit_each or url in found:
            continue
        found.append(url)
        if len(found) >= limit_count:
            break
    return found


def pull_request(url: str):
    """(canonical url, "owner/repo", number) for a pull request link, or None."""
    match = PR_URL.search(url or "")
    if not match:
        return None
    host, owner, repo, number = match.groups()
    return f"https://{host.lower()}/{owner}/{repo}/pull/{number}", f"{owner}/{repo}", int(number)


def pr_key(url: str) -> str:
    """Hosts, owners and repository names are case-insensitive on GitHub."""
    return "pr:" + url.lower()
