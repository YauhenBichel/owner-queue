# Security

Please report security problems privately through GitHub's
"Report a vulnerability" button on the Security tab of this repository, not in
a public issue. You will get an answer within a week.

What this plugin touches:

- It reads the JSON that Claude Code passes to hooks and, only when a Stop
  event carries no message, the last 64 KB of that session's transcript.
- It writes the queue (an event log, a lock file and small state files) to
  `~/.claude/owner-queue`, readable by the owner only. Stored per item: kind,
  links, repository, pull request number and title, working directory, session
  id and a short excerpt. Command lines and tool output are not stored, and
  anything that looks like a token is redacted before it is written.
- Hooks make no network calls. `owner-queue sync` runs the owner's own `gh`
  (`gh pr view <url> --json state,mergedAt`). Phone notifications, when the
  owner has switched them on, send one HTTPS request per digest to the
  configured ntfy server; with the default level it holds kinds and counts
  only.
- The ntfy topic is a secret: it is read from a file the owner creates, used
  in the request address and never printed, logged or stored elsewhere.

Queue items are text written by other sessions. The command prints them as
quoted data and the `/queue` command tells the model not to act on them; it
never merges, approves or runs anything an item names.
