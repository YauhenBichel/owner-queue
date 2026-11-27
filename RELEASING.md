# Releasing

A release is one version of the same source in four places: the plugin (this
repository), PyPI, the Homebrew tap and npm. Publishing a GitHub Release starts
`.github/workflows/release.yml`, which does the rest. No registry token is
stored anywhere: PyPI and npm trust this repository's workflow (OIDC).

Nothing in this file has been done yet for the first version. The one-time
setup below is the owner's, by hand, in a browser.

## What is never automated

- Deciding to release, and publishing the GitHub Release.
- The one-time setup on pypi.org, npmjs.com and GitHub (below).
- The first `npm publish` (npm cannot trust a workflow for a package that does
  not exist yet).
- Merging the pull request in the Homebrew tap. The workflow opens it; a person
  reads and merges it.
- Approving a deployment, if the `pypi` and `npm` environments are given
  required reviewers (recommended).

## One-time setup

Do these once, before the first release.

**GitHub, this repository** (Settings, Environments): create the environments
`pypi` and `npm`. For each, add yourself as a required reviewer and limit
deployments to tags `v*`. The workflow then waits for your approval before
each upload.

**PyPI.** The names `owner-queue`, `owner_queue` and `ownerqueue` were free on
26 November 2026; check again. On pypi.org, Account, Publishing, add a pending
publisher: project `owner-queue`, owner `YauhenBichel`, repository
`owner-queue`, workflow `release.yml`, environment `pypi`. The first release
creates the project.

**npm.** The name `owner-queue` was free on 26 November 2026; check again. npm
has no pending publishers, so the first version is published by hand from a
clean checkout of the release tag:

```sh
npm test && python3 scripts/check_package.py npm
npm pack --dry-run                      # read the list once more
npm login
npm publish --access public --provenance=false
```

Then, on npmjs.com, package `owner-queue`, Settings, Trusted Publisher: GitHub
Actions, user `YauhenBichel`, repository `owner-queue`, workflow `release.yml`,
environment `npm`, and allow `npm publish`. A new trusted publisher expires
unless it publishes within 2 days, so add it shortly before the second
release. In the same settings, require two-factor authentication and disallow
tokens.

For the first release the `npm` job of the workflow therefore has two possible
orders: publish by hand first, and the job sees the version on npm and does
nothing; or let the job fail, publish by hand, and leave it failed.

**Homebrew tap.** Create a fine-grained personal access token that can reach
only `YauhenBichel/homebrew-tap`, with Contents: read and write and Pull
requests: read and write, and save it in this repository as the Actions secret
`HOMEBREW_TAP_TOKEN`. Without it the release still works: the `homebrew` job
keeps the filled-in formula as the artifact `homebrew-formula` and opens
nothing. The first pull request also needs a row for `owner-queue` in the
tap's README, added by hand.

## Each release

1. Change the version in **one** place, `src/owner_queue/__init__.py`, then
   make the copies agree: `.claude-plugin/plugin.json` and `package.json`.
   `pyproject.toml` reads the version from the package, and the Homebrew
   formula takes it from the sdist URL. `tests/test_versions.py` fails if a
   copy disagrees.
2. Add the version and date to `CHANGELOG.md`.
3. Check locally:

   ```sh
   python3 -m unittest discover -s tests
   npm test
   python3 scripts/check_package.py npm
   uv build && python3 scripts/check_package.py dist dist/* && rm -rf dist
   actionlint .github/workflows/*.yml
   ```

4. Merge to `main` and wait for CI, which repeats all of this and also runs
   the tests against the installed wheel.
5. Publish a GitHub Release with the tag `vX.Y.Z` (the same version, with a
   `v`). The workflow refuses a tag that does not match.

## What the workflow does, in order

1. `tests`: everything CI runs.
2. `build`: the sdist and wheel, built once; the content check; the wheel is
   installed and run.
3. `pypi`: uploads those two files to PyPI.
4. `homebrew`: waits until PyPI serves the sdist with the checksum of the file
   built in step 2, fills in `packaging/homebrew/owner-queue.rb` with that URL
   and checksum (`scripts/render_formula.py`), and opens a pull request in the
   tap. PyPI goes first because the formula downloads the sdist from PyPI, as
   the tap's other Python formula does.
5. `npm`: tests the launcher, checks the tarball's contents and publishes. It
   does not wait for PyPI: the npm package carries the source itself.

A job that failed can be re-run: each one skips what is already published.

## After the release

- `pipx install owner-queue==X.Y.Z` and `npm install -g owner-queue@X.Y.Z`
  install, and `owner-queue --version` says `X.Y.Z`.
- Read the tap pull request, then, on a Mac, test it before merging:

  ```sh
  brew install --build-from-source ./Formula/owner-queue.rb
  brew test owner-queue && brew audit --strict owner-queue
  ```

- In Claude Code: `claude plugin marketplace update owner-queue`, then
  `claude plugin update owner-queue@owner-queue`.

## What was checked before the first release, and what was not

Checked on a Mac, with nothing published: the npm tarball installed into a
temporary prefix (also with `--ignore-scripts`) and run; the wheel installed
into a new virtual environment and the whole test suite run against it; the
sdist installed the way the formula installs it (`pip install --no-deps
--no-binary=:all:`); the formula rendered from a locally built sdist, `ruby -c`
and `brew style`; both workflows with `actionlint`.

Not checked, because it needs the real services: the workflow itself, trusted
publishing on PyPI and npm, the tap pull request, and `brew install` of the
formula (which builds with the tap's `python@3.14` and lets pip fetch
`setuptools` at build time, as the tap's other Python formula does).
