"""Check the shared DOM bindings used by settings and project initialization."""

import re
import unittest
from pathlib import Path


class StaticBindingsTests(unittest.TestCase):
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
