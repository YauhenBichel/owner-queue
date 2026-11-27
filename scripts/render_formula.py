#!/usr/bin/env python3
"""Fill in the Homebrew formula for one release.

    scripts/render_formula.py --sdist dist/owner_queue-1.2.3.tar.gz --url <where that file is served> [-o FILE]
    scripts/render_formula.py --sha256 <64 hex digits> --url <...> [-o FILE]

It reads packaging/homebrew/owner-queue.rb, replaces the placeholder `url` and `sha256`, and
writes the result to FILE (standard output by default). With --sdist the checksum is computed
from that file, so the formula names exactly the file that was built. The file name at the end
of the URL must be the sdist of this version, because Homebrew reads the version from it.

Standard library only. Nothing is downloaded, pushed or installed.
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "packaging" / "homebrew" / "owner-queue.rb"

URL_LINE = re.compile(r'^  url "[^"\n]*"$', re.M)
SHA_LINE = re.compile(r'^  sha256 "[0-9a-f]{64}"$', re.M)
SDIST_NAME = re.compile(r"^owner_queue-(\d+\.\d+\.\d+(?:[a-z0-9.]*)?)\.tar\.gz$")
SAFE_URL = re.compile(r"^(?:https|file)://[A-Za-z0-9._~/%+@:-]+$")


def package_version() -> str:
    text = (ROOT / "src" / "owner_queue" / "__init__.py").read_text()
    return re.search(r'^__version__ = "([^"]+)"$', text, re.M).group(1)


def render(url: str, sha256: str, template: "str | None" = None) -> str:
    if not SAFE_URL.match(url):
        raise ValueError("the URL must be a plain https:// or file:// address")
    name = SDIST_NAME.match(url.rsplit("/", 1)[-1])
    if not name:
        raise ValueError("the URL must end in owner_queue-<version>.tar.gz")
    if name.group(1) != package_version():
        raise ValueError(f"the URL is for version {name.group(1)}, this checkout is {package_version()}")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256) or sha256 == "0" * 64:
        raise ValueError("sha256 must be 64 lower-case hex digits of a real file")
    text = TEMPLATE.read_text() if template is None else template
    if len(URL_LINE.findall(text)) != 1 or len(SHA_LINE.findall(text)) != 1:
        raise ValueError("the formula must have exactly one url line and one sha256 line")
    text = URL_LINE.sub(lambda _: f'  url "{url}"', text)
    return SHA_LINE.sub(lambda _: f'  sha256 "{sha256}"', text)


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description="Fill in the Homebrew formula for one release.")
    parser.add_argument("--url", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sdist")
    source.add_argument("--sha256")
    parser.add_argument("-o", "--output")
    args = parser.parse_args(argv)
    try:
        if args.sdist:
            sdist = Path(args.sdist)
            if sdist.name != args.url.rsplit("/", 1)[-1]:
                raise ValueError(f"the URL does not end in the name of the sdist, {sdist.name}")
            sha256 = hashlib.sha256(sdist.read_bytes()).hexdigest()
        else:
            sha256 = args.sha256
        text = render(args.url, sha256)
    except (OSError, ValueError) as error:
        sys.stderr.write(f"render_formula: {error}\n")
        return 1
    if args.output:
        Path(args.output).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
