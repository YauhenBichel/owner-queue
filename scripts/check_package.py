#!/usr/bin/env python3
"""Check what a package of owner-queue would publish, before anything is published.

    scripts/check_package.py npm                 what `npm pack` would put in the tarball
    scripts/check_package.py dist dist/*         a built sdist (.tar.gz) and wheel (.whl)

For each package it checks two things:
  1. the list of files is exactly the intended one: no tests, caches, queue data or stray files;
  2. no file holds something personal: a home-directory path, an e-mail address, a private
     network address, a local host name, or something shaped like a token.

It prints one line per package (size, number of files) and exits 1 with the reasons if a check
fails. Standard library only. Nothing is uploaded and nothing outside this folder is read.
"""

import fnmatch
import io
import json
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SOURCE = ["src/owner_queue/*.py", "src/owner_queue/handlers/*.py"]

NPM_FILES = [
    ".claude-plugin/marketplace.json",
    ".claude-plugin/plugin.json",
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "bin/owner-queue",
    "commands/queue.md",
    "hooks/hooks.json",
    "package.json",
    "packaging/npm/bin/owner-queue.js",
    *SOURCE,
]

# Inside owner_queue-<version>/ in the sdist.
SDIST_FILES = [
    "CHANGELOG.md",
    "LICENSE",
    "MANIFEST.in",
    "PKG-INFO",
    "README.md",
    "pyproject.toml",
    "setup.cfg",
    "src/owner_queue.egg-info/*",
    *SOURCE,
]

WHEEL_FILES = [
    "owner_queue/*.py",
    "owner_queue/handlers/*.py",
    "owner_queue-*.dist-info/METADATA",
    "owner_queue-*.dist-info/RECORD",
    "owner_queue-*.dist-info/WHEEL",
    "owner_queue-*.dist-info/entry_points.txt",
    "owner_queue-*.dist-info/top_level.txt",
    "owner_queue-*.dist-info/licenses/LICENSE",
]

# Names that must never be in a package, whatever the allow-list says.
NEVER = [
    "tests/*", "*/tests/*", "*__pycache__*", "*.pyc", "*.jsonl", "*ntfy-topic*", "*.env", ".env*",
    "*.DS_Store", "*.log", "*.pem", "*.key", "*/lock", "*/cursor", "*.tgz", "*.whl",
]

PERSONAL = [
    ("a home-directory path", re.compile(r"(?:/Users/|/home/)[A-Za-z0-9_.-]+")),
    ("an e-mail address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("a private network address", re.compile(
        r"(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01])|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))"
        r"\.\d{1,3}\.\d{1,3}(?![\d.])")),
    ("a local host name", re.compile(r"\b[a-z0-9][a-z0-9-]*\.(?:local|lan|internal|home\.arpa)\b")),
    ("something shaped like a token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|npm_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_-]{40,}"
        r"|sk-[A-Za-z0-9_-]{30,}|tk_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b")),
]

# What a match may be without being personal: documentation placeholders only.
ALLOWED = re.compile(
    r"^(?:/home/(?:you|user|me)|/Users/(?:you|user|me)"
    r"|[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)*example(?:\.(?:com|org|net))?"
    r"|noreply@anthropic\.com)$"
)


def matches(name: str, patterns: list) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def check(label: str, files: dict, allowed: list) -> list:
    """Problems found in one package. `files` maps a path in the package to its bytes."""
    problems = []
    for name in sorted(files):
        if matches(name, NEVER):
            problems.append(f"{label}: {name} must never be published")
        elif not matches(name, allowed):
            problems.append(f"{label}: {name} is not on the list of intended files")
    for pattern in allowed:
        if "*" not in pattern and pattern not in files:
            problems.append(f"{label}: {pattern} is missing")
    for name, data in sorted(files.items()):
        text = data.decode("utf-8", errors="replace")
        for what, regex in PERSONAL:
            for found in regex.findall(text):
                if not ALLOWED.match(found):
                    problems.append(f"{label}: {name} holds {what}")   # the value itself is not printed
                    break
    return problems


def npm_files() -> tuple:
    """(files, packed size) of what `npm pack` would publish, read from the working tree."""
    done = subprocess.run(
        ["npm", "pack", "--dry-run", "--json", "--ignore-scripts"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
    )
    if done.returncode != 0:
        raise RuntimeError("npm pack failed: " + done.stderr.decode(errors="replace").strip()[-300:])
    report = json.loads(done.stdout)[0]
    files = {entry["path"]: (ROOT / entry["path"]).read_bytes() for entry in report["files"]}
    return files, report["size"]


def sdist_files(path: Path) -> dict:
    files = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            name = member.name.split("/", 1)[1] if "/" in member.name else member.name
            if not member.isfile():
                files[name] = b""
                raise RuntimeError(f"{path.name}: {member.name} is not a regular file")
            files[name] = archive.extractfile(member).read()
    return files


def wheel_files(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist() if not info.is_dir()}


def check_npm() -> list:
    files, size = npm_files()
    package = json.loads(files["package.json"])
    problems = check("npm", files, NPM_FILES)
    scripts = package.get("scripts", {})
    for hook in ("preinstall", "install", "postinstall", "prepare", "prepublish", "prepublishOnly", "prepack", "postpack"):
        if hook in scripts:
            problems.append(f"npm: package.json has a {hook} script; this package runs nothing at install time")
    for field in ("dependencies", "optionalDependencies", "peerDependencies", "bundleDependencies", "bundledDependencies"):
        if package.get(field):
            problems.append(f"npm: package.json has {field}; this package has none")
    print(f"npm: owner-queue {package['version']}, {len(files)} files, {size} bytes packed")
    return problems


def check_dist(paths: list) -> list:
    problems = []
    for path in map(Path, paths):
        if path.name.endswith(".tar.gz"):
            files, allowed, label = sdist_files(path), SDIST_FILES, "sdist"
        elif path.name.endswith(".whl"):
            files, allowed, label = wheel_files(path), WHEEL_FILES, "wheel"
        else:
            problems.append(f"{path.name}: not an sdist (.tar.gz) or a wheel (.whl)")
            continue
        problems += check(label, files, allowed)
        if label == "wheel":
            entry = next((data for name, data in files.items() if name.endswith("entry_points.txt")), b"")
            if b"owner-queue = owner_queue.__main__:main" not in entry:
                problems.append("wheel: the owner-queue command is not declared")
            meta = next((data for name, data in files.items() if name.endswith("/METADATA")), b"")
            if re.search(rb"^Requires-Dist:", meta, re.M):
                problems.append("wheel: it declares a dependency; this package has none")
        print(f"{label}: {path.name}, {len(files)} files, {path.stat().st_size} bytes")
    return problems


def main(argv: list) -> int:
    if argv[:1] == ["npm"] and len(argv) == 1:
        problems = check_npm()
    elif argv[:1] == ["dist"] and len(argv) > 1:
        problems = check_dist(argv[1:])
    else:
        sys.stderr.write(__doc__.split("\n\n")[1] + "\n")
        return 2
    for problem in problems:
        sys.stderr.write(problem + "\n")
    if not problems:
        print("ok")
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, RuntimeError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        sys.stderr.write(f"check_package: {error}\n")
        sys.exit(1)
