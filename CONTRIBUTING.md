# Contributing

Thanks for helping.

- Open an issue first for anything bigger than a small fix.
- Work on a branch and open a pull request against `main`. CI must be green.
- Keep the package free of third-party dependencies: it runs as a hook on
  every tool call, with only the Python standard library, on Python 3.9+.
- Add or change a test in `tests/` for every behaviour you change, and run
  `python3 -m unittest discover -s tests` before pushing.
- Hooks must never break or slow a session: on any error they print nothing on
  stdout and exit 0, they make no network calls and they do not run `gh`.
- Tests use temporary directories, a fake `gh` and a fake ntfy server on a
  local port. Fixtures use made-up repositories (`example/repo`), sessions and
  tokens only: never paste data from a real session.
- Text captured from sessions is data. Anything that stores or prints it goes
  through `owner_queue/text.py` (byte caps, control characters, redaction).
