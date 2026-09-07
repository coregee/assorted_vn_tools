"""Check the shared DOM bindings used by settings and project initialization."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path


class StaticBindingsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for editor behavior checks")
    def test_editing_during_translation(self):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("editor_behavior.cjs"))],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_element_lookups_are_registered_and_exist_in_html(self):
        static = Path(__file__).resolve().parents[1] / "static"
        script = (static / "app.js").read_text(encoding="utf-8")
        html = (static / "index.html").read_text(encoding="utf-8")
        registry = re.search(r"const elementIds = \[(.*?)\];", script, re.S).group(1)
        registered = set(re.findall(r'"([^"]+)"', registry))
        referenced = set(re.findall(r'\$\("([^"]+)"\)', script))
        html_ids = set(re.findall(r'\bid="([^"]+)"', html))
        self.assertEqual(set(), referenced - registered, "Unregistered element lookups")
        self.assertEqual(set(), registered - html_ids, "Missing HTML elements")


if __name__ == "__main__":
    unittest.main()
