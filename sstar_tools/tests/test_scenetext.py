import json
import tempfile
import unittest
from pathlib import Path

from sstar_tools.libraries import scenetext


class SceneTextReviewReportTests(unittest.TestCase):
    def test_build_reports_truncated_page_by_editor_pointer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts, scenes, output = root / "script", root / "scenes", root / "output"
            scripts.mkdir()
            scenes.mkdir()
            (scripts / "_names.json").write_text("{}", encoding="utf-8")
            (scripts / "scene.json").write_text(json.dumps([{
                "scene": "SCENE.BIN",
                "kind": "page",
                "page": 1,
                "slots": [0],
                "jp": "原文",
                "jp_lines": ["原文"],
                "tr": "word " * 100,
            }]), encoding="utf-8")
            slot = bytearray(scenetext.SLOT)
            slot[0] = scenetext.OP_D
            slot[1:3] = "原".encode("cp932")
            (scenes / "SCENE.BIN").write_bytes(slot)
            report = root / "review.json"

            scenetext.build(str(scripts), str(scenes), str(output), cols=12,
                            review_report=str(report))

            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(1, payload["version"])
            self.assertEqual("script/scene.json", payload["issues"][0]["path"])
            self.assertEqual("/0", payload["issues"][0]["pointer"])
            self.assertEqual("line_overflow", payload["issues"][0]["details"]["kind"])


if __name__ == "__main__":
    unittest.main()
