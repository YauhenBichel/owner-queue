"""What would be published to npm, PyPI and Homebrew is exactly what is meant to be.

Nothing here publishes, uploads or installs anything outside a temporary directory, and no
registry is contacted. Tests that need a tool this machine does not have (npm, ruby, the
`build` module) are skipped; CI has all of them.
"""

import glob
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT

import owner_queue

sys.path.insert(0, str(ROOT / "scripts"))

import check_package  # noqa: E402
import render_formula  # noqa: E402

PACKAGE = json.loads((ROOT / "package.json").read_text())
PYPROJECT = (ROOT / "pyproject.toml").read_text()
SOURCE = {
    str(path.relative_to(ROOT)): path.read_bytes()
    for path in sorted((ROOT / "src" / "owner_queue").rglob("*.py"))
}


class TheCheckItself(unittest.TestCase):
    """The check must fail when it should, or passing means nothing."""

    def good(self) -> dict:
        files = {"LICENSE": b"MIT", "README.md": b"# owner-queue\n"}
        files.update(SOURCE)
        return files

    def problems(self, files: dict) -> list:
        return check_package.check("test", files, ["LICENSE", "README.md", *check_package.SOURCE])

    def test_the_intended_files_pass(self):
        self.assertEqual(self.problems(self.good()), [])

    def test_a_file_that_is_not_on_the_list_fails(self):
        for name in ("notes.txt", "src/owner_queue/data/x.json", "CONTRIBUTING.md"):
            files = self.good()
            files[name] = b"x"
            self.assertEqual(len(self.problems(files)), 1, name)

    def test_tests_caches_queue_data_and_topic_files_fail_even_if_listed(self):
        for name in ("tests/test_store.py", "src/owner_queue/__pycache__/store.cpython-313.pyc",
                     "queue/events.jsonl", "ntfy-topic", ".env", "src/owner_queue/.DS_Store"):
            problems = check_package.check("test", {name: b"x"}, [name])
            self.assertTrue(any("must never be published" in p for p in problems), name)

    def test_a_missing_file_fails(self):
        files = self.good()
        del files["LICENSE"]
        self.assertEqual(self.problems(files), ["test: LICENSE is missing"])

    def test_personal_data_fails_and_is_not_repeated_in_the_message(self):
        samples = {
            "a home-directory path": "/Users/" + "somebody/project",
            "an e-mail address": "somebody" + "@" + "mail.test",
            "a private network address": "http://192.168." + "1.20:8080/",
            "a local host name": "ssh my-box" + ".local",
            "something shaped like a token": "ghp_" + "A1b2" * 9,
        }
        for what, sample in samples.items():
            files = self.good()
            files["README.md"] = f"see {sample} for more\n".encode()
            problems = self.problems(files)
            self.assertEqual(problems, [f"test: README.md holds {what}"], what)
            self.assertNotIn(sample, problems[0])

    def test_documentation_placeholders_pass(self):
        files = self.good()
        files["README.md"] = (
            b"Co-Authored-By: Claude <noreply@anthropic.com>\nmail someone@example.com\n"
            b"Python 3.10.2, version 1.10.0.1, listening on 127.0.0.1\n"
        )
        self.assertEqual(self.problems(files), [])


class Npm(unittest.TestCase):
    def test_package_json_describes_this_project(self):
        self.assertEqual(PACKAGE["name"], "owner-queue")
        self.assertIn('name = "owner-queue"', PYPROJECT)
        self.assertEqual(PACKAGE["license"], "MIT")
        self.assertIn('license = "MIT"', PYPROJECT)
        # npm trusted publishing refuses a package whose repository is not the one that publishes it.
        self.assertEqual(PACKAGE["repository"]["url"], "git+https://github.com/YauhenBichel/owner-queue.git")
        self.assertEqual(PACKAGE["publishConfig"], {"access": "public", "provenance": True})
        self.assertEqual(list(PACKAGE["bin"]), ["owner-queue"])

    def test_nothing_runs_at_install_time_and_nothing_is_downloaded(self):
        self.assertEqual(list(PACKAGE.get("scripts", {})), ["test"])
        for field in ("dependencies", "optionalDependencies", "peerDependencies", "bundleDependencies"):
            self.assertNotIn(field, PACKAGE)
        launcher = (ROOT / PACKAGE["bin"]["owner-queue"]).read_text()
        for word in ("http", "fetch(", "npm install", "pip install", "shell: true", "exec("):
            self.assertNotIn(word, launcher, word)

    def test_the_files_list_names_files_not_folders(self):
        for entry in PACKAGE["files"]:
            self.assertTrue(os.path.splitext(entry)[1] or entry == "bin/owner-queue", entry)
            self.assertFalse(entry.startswith(("tests", "scripts", ".github")), entry)

    def test_the_plugin_is_complete_inside_the_npm_package(self):
        # Claude Code can install a plugin from an npm package; the hooks then run
        # ${CLAUDE_PLUGIN_ROOT}/bin/owner-queue, which needs src/ beside it.
        for needed in (".claude-plugin/plugin.json", "hooks/hooks.json", "commands/queue.md", "bin/owner-queue"):
            self.assertIn(needed, PACKAGE["files"])
        self.assertIn("src/owner_queue/*.py", PACKAGE["files"])
        self.assertIn("src/owner_queue/handlers/*.py", PACKAGE["files"])

    @unittest.skipUnless(shutil.which("npm"), "npm is not installed")
    def test_npm_would_pack_exactly_the_intended_files(self):
        files, size = check_package.npm_files()
        self.assertEqual(check_package.check("npm", files, check_package.NPM_FILES), [])
        self.assertEqual({name for name in files if name.startswith("src/")}, set(SOURCE), "all of the source, nothing else")
        self.assertLess(size, 100_000, "a small package")
        done = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_package.py"), "npm"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        self.assertEqual((done.returncode, done.stderr), (0, b""))


class PyPI(unittest.TestCase):
    def test_pyproject_declares_one_command_and_no_dependencies(self):
        self.assertIn('owner-queue = "owner_queue.__main__:main"', PYPROJECT)
        self.assertIn("dependencies = []", PYPROJECT)
        self.assertIn('requires-python = ">=3.9"', PYPROJECT)
        self.assertNotRegex(PYPROJECT, r"[\w.+-]+@[\w-]+\.\w+", "no e-mail address in the metadata")

    def dist(self) -> str:
        """A folder with a freshly built sdist and wheel, or skip."""
        given = os.environ.get("OWNER_QUEUE_TEST_DIST")
        if given:
            return given
        if importlib.util.find_spec("build") is None:
            self.skipTest("set OWNER_QUEUE_TEST_DIST to a folder with a built sdist and wheel, or install `build`")
        out = tempfile.mkdtemp(prefix="oq-dist-")
        self.addCleanup(shutil.rmtree, out, True)
        copy = os.path.join(out, "source")     # build from a copy, so nothing is written into the checkout
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.egg-info", "build", "dist", "node_modules"))
        done = subprocess.run([sys.executable, "-m", "build", "--outdir", os.path.join(out, "dist"), copy],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        self.assertEqual(done.returncode, 0, done.stdout.decode(errors="replace")[-2000:])
        return os.path.join(out, "dist")

    def test_the_sdist_and_wheel_hold_exactly_the_intended_files(self):
        dist = self.dist()
        version = owner_queue.__version__
        sdist = os.path.join(dist, f"owner_queue-{version}.tar.gz")
        wheel = os.path.join(dist, f"owner_queue-{version}-py3-none-any.whl")
        self.assertEqual(sorted(glob.glob(os.path.join(dist, "owner_queue-*"))), [wheel, sdist])
        self.assertEqual(check_package.check_dist([sdist, wheel]), [])
        in_wheel = check_package.wheel_files(wheel)
        self.assertEqual({"src/" + name for name in in_wheel if name.endswith(".py")}, set(SOURCE))
        in_sdist = check_package.sdist_files(sdist)
        self.assertEqual({name for name in in_sdist if name.endswith(".py")}, set(SOURCE))
        for name, data in SOURCE.items():
            self.assertEqual(in_sdist[name], data, name)
        meta = next(data for name, data in in_wheel.items() if name.endswith("/METADATA")).decode()
        self.assertIn(f"\nVersion: {version}\n", meta)
        self.assertIn("\nRequires-Python: >=3.9\n", meta)
        self.assertNotIn("Author-email", meta)


class Homebrew(unittest.TestCase):
    TEMPLATE = (ROOT / "packaging" / "homebrew" / "owner-queue.rb").read_text()
    SHA = hashlib.sha256(b"a stand-in for an sdist").hexdigest()

    def url(self, version: "str | None" = None) -> str:
        return f"https://files.pythonhosted.org/packages/ab/cd/ef01/owner_queue-{version or owner_queue.__version__}.tar.gz"

    def test_the_formula_in_the_repository_holds_placeholders_only(self):
        self.assertIn("owner_queue-0.0.0.tar.gz", self.TEMPLATE)
        self.assertIn('sha256 "' + "0" * 64 + '"', self.TEMPLATE)

    def test_it_installs_the_command_with_a_homebrew_python_and_no_other_downloads(self):
        self.assertIn("class OwnerQueue < Formula", self.TEMPLATE)
        self.assertIn("include Language::Python::Virtualenv", self.TEMPLATE)
        self.assertRegex(self.TEMPLATE, r'depends_on "python@3\.\d+"')
        self.assertNotIn("resource ", self.TEMPLATE)
        self.assertIn('license "MIT"', self.TEMPLATE)

    def test_its_test_adds_and_counts_in_a_temporary_queue(self):
        test = self.TEMPLATE.split("  test do\n", 1)[1]
        self.assertIn('ENV["OWNER_QUEUE_DIR"] = testpath/"queue"', test)
        self.assertLess(test.index("OWNER_QUEUE_DIR"), test.index("owner-queue add"))
        for part in ("--version", "owner-queue add", "owner-queue count"):
            self.assertIn(part, test)

    def test_the_caveats_say_what_is_not_installed(self):
        caveats = self.TEMPLATE.split("def caveats", 1)[1].split("test do", 1)[0]
        self.assertIn("claude plugin install owner-queue@owner-queue", caveats)
        self.assertIn("does not", caveats)

    def test_rendering_changes_the_url_and_checksum_and_nothing_else(self):
        out = render_formula.render(self.url(), self.SHA)
        before, after = self.TEMPLATE.splitlines(), out.splitlines()
        changed = [(a, b) for a, b in zip(before, after) if a != b]
        self.assertEqual(len(before), len(after))
        self.assertEqual([b for _, b in changed], [f'  url "{self.url()}"', f'  sha256 "{self.SHA}"'])

    def test_rendering_refuses_what_would_make_a_wrong_formula(self):
        bad = [
            (self.url("9.9.9"), self.SHA),                               # another version
            (self.url().replace("https://", "http://"), self.SHA),       # not https
            (self.url() + '"\n  system "true', self.SHA),                # not a plain URL
            ("https://example.com/owner-queue.zip", self.SHA),
            (self.url(), "0" * 64),                                      # the placeholder
            (self.url(), self.SHA.upper()),
            (self.url(), "abc"),
        ]
        for url, sha in bad:
            with self.assertRaises(ValueError, msg=url):
                render_formula.render(url, sha)

    @unittest.skipUnless(shutil.which("ruby"), "ruby is not installed")
    def test_a_formula_rendered_from_a_real_file_is_valid_ruby(self):
        tmp = tempfile.mkdtemp(prefix="oq-brew-")
        self.addCleanup(shutil.rmtree, tmp, True)
        sdist = os.path.join(os.path.realpath(tmp), f"owner_queue-{owner_queue.__version__}.tar.gz")
        with open(sdist, "wb") as f:
            f.write(b"a stand-in for an sdist")
        out = os.path.join(tmp, "owner-queue.rb")
        script = str(ROOT / "scripts" / "render_formula.py")
        done = subprocess.run([sys.executable, script, "--sdist", sdist, "--url", "file://" + sdist, "-o", out],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual((done.returncode, done.stderr), (0, b""))
        with open(out) as f:
            text = f.read()
        self.assertIn(f'  url "file://{sdist}"', text)
        self.assertIn(f'  sha256 "{self.SHA}"', text)
        for path in (out, str(ROOT / "packaging" / "homebrew" / "owner-queue.rb")):
            self.assertEqual(subprocess.run(["ruby", "-c", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode, 0)
        wrong = subprocess.run([sys.executable, script, "--sdist", sdist, "--url", self.url("9.9.9")],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual((wrong.returncode, wrong.stdout), (1, b""))


class ReleaseWorkflow(unittest.TestCase):
    TEXT = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    def job(self, name: str) -> str:
        return re.split(r"\n  (?=[a-z]+:\n)", self.TEXT.split("\njobs:\n", 1)[1])[["tests", "build", "pypi", "homebrew", "npm"].index(name)]

    def test_it_only_runs_when_a_release_is_published(self):
        self.assertIn("on:\n  release:\n    types: [published]\n", self.TEXT)
        self.assertNotIn("push:", self.TEXT)
        self.assertNotIn("workflow_dispatch", self.TEXT)

    def test_no_registry_token_is_stored(self):
        self.assertEqual(set(re.findall(r"secrets\.(\w+)", self.TEXT)), {"HOMEBREW_TAP_TOKEN"})
        self.assertNotIn("NODE_AUTH_TOKEN", self.TEXT)
        self.assertNotIn("password:", self.TEXT)
        self.assertIn("permissions:\n  contents: read\n\njobs:", self.TEXT)

    def test_each_publishing_job_has_its_environment_and_oidc(self):
        for name in ("pypi", "npm"):
            job = self.job(name)
            self.assertTrue(job.startswith(name + ":"), name)
            self.assertIn(f"environment:\n      name: {name}\n", job)
            self.assertIn("id-token: write", job)
        for name in ("tests", "build", "homebrew"):
            self.assertNotIn("id-token", self.job(name), name)

    def test_the_order_is_tests_build_pypi_then_homebrew(self):
        self.assertIn("needs: tests", self.job("build"))
        self.assertIn("needs: build", self.job("pypi"))
        self.assertIn("needs: pypi", self.job("homebrew"))
        self.assertIn("needs: tests", self.job("npm"))
        self.assertEqual(self.TEXT.count("uv build"), 1, "built once")

    def test_the_tap_job_does_nothing_in_the_tap_without_its_token(self):
        job = self.job("homebrew")
        guard = job.index('if [ -z "$TAP_TOKEN" ]')
        for step in ("gh repo clone", "git push", "gh pr create"):
            self.assertLess(guard, job.index(step), step)
        self.assertNotIn("gh pr merge", self.TEXT)

    def test_every_action_is_pinned_to_a_commit(self):
        for workflow in ("release.yml", "ci.yml"):
            text = (ROOT / ".github" / "workflows" / workflow).read_text()
            for used in re.findall(r"uses: (\S+)", text):
                if not used.startswith("./"):
                    self.assertRegex(used, r"@[0-9a-f]{40}$", used)


if __name__ == "__main__":
    unittest.main()
