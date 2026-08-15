import json
import tempfile
import unittest
from pathlib import Path

from llm_translation_tools.project import (
    FileConflict,
    PROJECT_SETTINGS_FILE,
    Project,
    ProjectError,
    UnsafePath,
)


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


class ProjectAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vn scripts ")
        self.base = Path(self.temporary.name)
        self.script = self.base / "script"
        self.script.mkdir()

        write_json(
            self.script / "dasaku.json",
            [{
                "name": "花子",
                "message": "おはよう。",
                "translated": None,
                "unknown": {"preserve": True},
            }],
        )
        write_json(
            self.script / "config.json",
            [
                {
                    "line": 12,
                    "seg": 0,
                    "key": "pageNumberVarName",
                    "original": "変数名",
                    "translated": None,
                    "context": "pageNumberVarName 変数名",
                    "note": "script variable name -- do not translate",
                },
                {
                    "line": 13,
                    "seg": 0,
                    "key": "voiceNameChangeVar1",
                    "original": "別の変数名",
                    "translated": None,
                    "context": "voiceNameChangeVar1 別の変数名",
                },
            ],
        )
        write_json(
            self.script / "etutane.json",
            {
                "file": "A000.a0",
                "lines": [{
                    "i": 3,
                    "kind": "dialogue",
                    "speaker": "花子",
                    "jp": "元気？ «FE»",
                    "translated": None,
                }],
            },
        )
        write_json(
            self.script / "sstar.json",
            [{
                "scene": "SCENE.BIN",
                "kind": "page",
                "page": 1,
                "slots": [4],
                "speaker": "太郎",
                "jp": "待って\\x81",
                "jp_lines": ["待って\\x81"],
                "tr": None,
            }],
        )
        write_json(self.script / "_names.json", {"花子": None, "太郎": "Taro"})
        self.project = Project.open(str(self.base))

    def tearDown(self):
        self.temporary.cleanup()

    def test_discovers_and_normalizes_all_native_schemas(self):
        summaries = {item["path"]: item for item in self.project.list_files()}

        self.assertEqual(
            {
                "script/_names.json": "glossary",
                "script/config.json": "dasaku-ui",
                "script/dasaku.json": "dasaku",
                "script/etutane.json": "etutane",
                "script/sstar.json": "sstar",
            },
            {path: item["schema"] for path, item in summaries.items()},
        )
        etutane = self.project.read_file("script/etutane.json")["lines"][0]
        self.assertEqual("元気？ «FE»", etutane["source"])
        self.assertEqual("花子", etutane["speaker"])
        self.assertEqual(3, etutane["metadata"]["i"])

        protected = self.project.read_file("script/config.json")["lines"][0]
        self.assertFalse(protected["translatable"])
        self.assertEqual("pageNumberVarName 変数名", protected["context"])
        protected_without_note = self.project.read_file("script/config.json")["lines"][1]
        self.assertFalse(protected_without_note["translatable"])

        sstar = self.project.read_file("script/sstar.json")["lines"][0]
        self.assertEqual(["待って\\x81"], sstar["source_segments"])
        self.assertFalse(sstar["empty_is_applied"])
        self.assertFalse(sstar["translation_active"])

        dasaku = self.project.read_file("script/dasaku.json")["lines"][0]
        self.assertTrue(dasaku["empty_is_applied"])

    def test_update_changes_only_native_translation_field(self):
        snapshot = self.project.read_file("script/dasaku.json")
        line = snapshot["lines"][0]

        updated = self.project.update_file(
            snapshot["path"],
            snapshot["token"],
            [{"id": line["id"], "translation": "Good morning."}],
        )

        document = json.loads((self.script / "dasaku.json").read_text(encoding="utf-8"))
        self.assertEqual("Good morning.", document[0]["translated"])
        self.assertEqual({"preserve": True}, document[0]["unknown"])
        self.assertEqual("おはよう。", document[0]["message"])
        self.assertEqual("Good morning.", updated["lines"][0]["translation"])
        self.assertFalse(any(path.suffix == ".tmp" for path in self.script.iterdir()))

        with self.assertRaises(FileConflict):
            self.project.update_file(snapshot["path"], snapshot["token"], [])

    def test_protected_records_cannot_be_updated(self):
        snapshot = self.project.read_file("script/config.json")
        with self.assertRaises(ProjectError):
            self.project.update_file(
                snapshot["path"],
                snapshot["token"],
                [{"id": snapshot["lines"][0]["id"], "translation": "broken"}],
            )

    def test_paths_are_confined_to_script_workspace(self):
        outside = self.base / "outside.json"
        write_json(outside, [{"message": "outside", "translated": None}])

        with self.assertRaises(UnsafePath):
            self.project.resolve_file("outside.json")
        with self.assertRaises(UnsafePath):
            self.project.resolve_file("../outside.json")

    def test_direct_script_folder_and_state_filename_are_repacker_safe(self):
        direct = Project.open(str(self.script))
        self.assertEqual(".", direct.script_dir_string)
        self.assertFalse(PROJECT_SETTINGS_FILE.lower().endswith(".json"))

        direct.save_project_settings({"game_context": "A mystery."})
        state_path = self.script / PROJECT_SETTINGS_FILE
        self.assertTrue(state_path.is_file())
        self.assertNotIn(state_path.name, [path.name for path in self.script.glob("*.json")])
        self.assertEqual("A mystery.", direct.load_project_settings()["game_context"])


if __name__ == "__main__":
    unittest.main()
