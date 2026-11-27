import json
import unittest

from helpers import ROOT

import owner_queue


class VersionsAgree(unittest.TestCase):
    """One version: `__version__` in the package. Everything else reads it or must equal it."""

    def test_plugin_json_matches_package(self):
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(plugin["version"], owner_queue.__version__)

    def test_marketplace_entry_does_not_pin_another_version(self):
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        for entry in market["plugins"]:
            self.assertEqual(entry.get("version", owner_queue.__version__), owner_queue.__version__)

    def test_changelog_has_this_version(self):
        self.assertIn(f"## {owner_queue.__version__} ", (ROOT / "CHANGELOG.md").read_text())

    def test_pyproject_reads_version_from_package(self):
        text = (ROOT / "pyproject.toml").read_text()
        self.assertIn('dynamic = ["version"]', text)
        self.assertIn('version = { attr = "owner_queue.__version__" }', text)

    def test_package_json_matches_package(self):
        package = json.loads((ROOT / "package.json").read_text())
        self.assertEqual(package["version"], owner_queue.__version__)

    def test_the_formula_does_not_name_a_version(self):
        # Homebrew reads the version from the sdist URL, which the release fills in.
        formula = (ROOT / "packaging/homebrew/owner-queue.rb").read_text()
        self.assertNotRegex(formula, r'^\s*version "', "no version line")
        self.assertIn("owner_queue-0.0.0.tar.gz", formula)


if __name__ == "__main__":
    unittest.main()
