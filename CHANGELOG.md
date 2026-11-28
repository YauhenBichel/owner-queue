# Changelog

## 0.1.1 — 2026-11-28

- `/queue list` asks `gh` which pull requests were merged or closed before it lists, as `/queue` already
  did. Before, the list kept showing merged pull requests until the next `/queue`.
- A title given as `--title \"…\"` (the command inside `bash -c "…"` or `ssh host "…"`) is read whole.
  Before, the item's title was the first word with a stray `\"`.

## 0.1.0 — 2026-11-26

First version.

- Hooks collect what Claude Code sessions and subagents leave for the owner
  into one queue: pull requests opened with `gh pr create`, sessions waiting on
  a permission prompt or for input, and turns that end with links or a question.
- `owner-queue` command: `next`, `list`, `done`, `skip`, `open`, `reopen`,
  `count`, `add`, `sync`, `statusline`, `notify`, `compact`.
- `/queue` slash command: one item at a time, oldest first.
- `owner-queue sync` closes pull requests that were merged or closed (through
  the owner's own `gh`).
- Optional phone notifications through ntfy: off until a topic file exists,
  batched, rate-limited, counts only by default.
- Packaging for npm (the source and a small Node launcher), PyPI and Homebrew,
  with checks of what each package would publish. Nothing is published until
  the first release.
