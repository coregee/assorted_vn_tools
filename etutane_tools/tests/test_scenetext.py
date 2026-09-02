import json
import struct
import tempfile
import unittest
from pathlib import Path

from etutane_tools.libraries import scenetext, tinkerbell


def record(payload):
    return struct.pack("<I", len(payload)) + payload


def source_string(text):
    return tinkerbell.encrypt_string(text.encode("cp932") + bytes([scenetext.TERM]))


def blob(*payloads):
    return b"".join(record(payload) for payload in payloads)


COMMAND = b"M#N\x01\x00\x00\x00"


class SceneTextPageTests(unittest.TestCase):
    def test_extract_groups_consecutive_display_lines_into_pages(self):
        names = {}
        source = blob(
            source_string("【花子】"),
            COMMAND,
            source_string("一行目"),
            source_string("二行目"),
            scenetext.PAGE_ADVANCE,
            source_string("説明一"),
            source_string("説明二"),
            scenetext.PAGE_ADVANCE,
        )

        pages = scenetext.extract_pages(source, names)

        self.assertEqual({"【花子】": None}, names)
        self.assertEqual(2, len(pages))
        self.assertEqual([1, 2], pages[0]["string_indices"])
        self.assertEqual(["一行目", "二行目"], pages[0]["jp_lines"])
        self.assertEqual("一行目\n二行目", pages[0]["jp"])
        self.assertEqual("花子", pages[0]["speaker"])
        self.assertEqual("dialogue", pages[0]["kind"])
        self.assertEqual([3, 4], pages[1]["string_indices"])
        self.assertEqual("narration", pages[1]["kind"])

    def test_non_page_ui_strings_stay_independent(self):
        names = {}
        source = blob(
            source_string("選択肢一"),
            COMMAND,
            source_string("選択肢二"),
            COMMAND,
        )

        pages = scenetext.extract_pages(source, names)

        self.assertEqual([[0], [1]], [page["string_indices"] for page in pages])

    def test_old_line_translations_are_merged_without_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            previous = Path(temporary) / "scene.json"
            previous.write_text(json.dumps({"lines": [
                {"i": 1, "translated": "First part,"},
                {"i": 2, "translated": "second part."},
            ]}), encoding="utf-8")
            pages = [{"i": 1, "string_indices": [1, 2], "translated": None}]

            scenetext._merge_existing_translated(pages, str(previous))

            self.assertEqual("First part,\nsecond part.", pages[0]["translated"])

    def test_build_reflows_a_page_into_the_required_number_of_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original"
            scripts = root / "script"
            output = root / "output"
            original.mkdir()
            scripts.mkdir()
            source = blob(
                source_string("一行目"),
                source_string("二行目"),
                scenetext.PAGE_ADVANCE,
            )
            (original / "scene.a0").write_bytes(source)
            (scripts / "scene.json").write_text(json.dumps({
                "file": "scene.a0",
                "lines": [{
                    "i": 0,
                    "string_indices": [0, 1],
                    "kind": "narration",
                    "jp": "一行目\n二行目",
                    "jp_lines": ["一行目", "二行目"],
                    "translated": "one two three four five six",
                }],
            }), encoding="utf-8")
            names = root / "names.json"
            names.write_text("{}", encoding="utf-8")

            scenetext.build(str(scripts), str(original), str(output), str(names), cols=10)

            records = list(tinkerbell.parse_records((output / "scene.a0").read_bytes()))
            strings = [payload for _offset, payload in records
                       if tinkerbell.is_string_record(payload)]
            self.assertEqual(3, len(strings))
            self.assertEqual(scenetext.PAGE_ADVANCE, records[-1][1])

    def test_build_reports_every_truncated_page_by_editor_pointer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original, scripts, output = root / "original", root / "script", root / "output"
            original.mkdir()
            scripts.mkdir()
            source = blob(source_string("一行目"), scenetext.PAGE_ADVANCE,
                          source_string("二行目"), scenetext.PAGE_ADVANCE)
            (original / "scene.a0").write_bytes(source)
            (scripts / "scene.json").write_text(json.dumps({
                "file": "scene.a0",
                "lines": [
                    {
                        "i": 0,
                        "string_indices": [0],
                        "kind": "dialogue",
                        "jp": "一行目",
                        "jp_lines": ["一行目"],
                        "translated": "one two three four five six seven eight nine ten eleven twelve",
                    },
                    {
                        "i": 1,
                        "string_indices": [1],
                        "kind": "dialogue",
                        "jp": "二行目",
                        "jp_lines": ["二行目"],
                        "translated": "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda",
                    },
                ],
            }), encoding="utf-8")
            names = root / "names.json"
            names.write_text("{}", encoding="utf-8")
            report = root / "review.json"

            scenetext.build(str(scripts), str(original), str(output), str(names),
                            cols=10, review_report=str(report))

            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(1, payload["version"])
            self.assertEqual(2, len(payload["issues"]))
            self.assertEqual("script/scene.json", payload["issues"][0]["path"])
            self.assertEqual("/lines/0", payload["issues"][0]["pointer"])
            self.assertEqual("/lines/1", payload["issues"][1]["pointer"])
            self.assertTrue(payload["issues"][0]["details"]["dropped_text"])


if __name__ == "__main__":
    unittest.main()
