import json
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from llm_translation_tools.server import AppState, SettingsStore, create_server


class FakeLMStudioClient:
    def __init__(self, *args, **kwargs):
        pass

    def models(self):
        return [{"id": "fixture-model", "owned_by": "local"}]

    def chat_completion(self, messages, model, temperature, max_tokens,
                        response_format=None, reasoning_effort=None):
        prompt = messages[-1]["content"]
        encoded_ids = re.findall(r"<<<TARGET (\"(?:\\.|[^\"])*\")>>>", prompt)
        line_ids = [json.loads(value) for value in encoded_ids]
        return json.dumps({
            "translations": [
                {"id": line_id, "translation": "Translated line %d" % (index + 1)}
                for index, line_id in enumerate(line_ids)
            ],
        })


class BlockingSecondTurnClient(FakeLMStudioClient):
    calls = 0
    lock = threading.Lock()
    second_started = threading.Event()
    release_second = threading.Event()

    @classmethod
    def reset(cls):
        with cls.lock:
            cls.calls = 0
        cls.second_started.clear()
        cls.release_second.clear()

    def chat_completion(self, messages, model, temperature, max_tokens,
                        response_format=None, reasoning_effort=None):
        with self.lock:
            type(self).calls += 1
            call_number = type(self).calls
        if call_number == 2:
            self.second_started.set()
            self.release_second.wait(timeout=5)
        return super().chat_completion(
            messages, model, temperature, max_tokens, response_format,
            reasoning_effort)


class ServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vn web api ")
        self.root = Path(self.temporary.name) / "game with spaces"
        self.script = self.root / "script"
        self.script.mkdir(parents=True)
        (self.script / "scene.json").write_text(
            json.dumps([
                {"name": "花子", "message": "こんにちは", "translated": None},
                {"message": "朝になった。", "translated": None},
            ], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        self.defaults_path = Path(self.temporary.name) / "user config" / "defaults.json"
        self.state = AppState(
            base_dir=Path(self.temporary.name), defaults_path=self.defaults_path)
        self.server = create_server(port=0, state=self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base_url = "http://%s:%d" % (host, port)
        self.origin = self.base_url

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path, method="GET", body=None, origin=None,
                content_type="application/json"):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            if content_type:
                headers["Content-Type"] = content_type
            headers["Origin"] = self.origin if origin is None else origin
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            content_type_header = response.headers.get("Content-Type", "")
            value = json.loads(raw) if "json" in content_type_header else raw
            return response.status, value, response.headers

    def error(self, path, method="GET", body=None, origin=None,
              content_type="application/json"):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(path, method, body, origin, content_type)
        error = raised.exception
        try:
            return error.code, json.loads(error.read())
        finally:
            error.close()

    def open_project(self):
        return self.request(
            "/api/project/open",
            "POST",
            {"path": str(self.root)},
        )[1]

    def test_static_app_and_project_file_editing_api(self):
        status, html, headers = self.request("/")
        self.assertEqual(200, status)
        self.assertIn(b"VN Translation Workbench", html)
        self.assertIn(b'id="browse-project-button"', html)
        self.assertIn(b'id="choose-folder-button"', html)
        self.assertIn(b'id="extract-button"', html)
        self.assertIn(b'id="repack-button"', html)
        self.assertIn(b'id="theme-toggle"', html)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(200, self.request("/theme.js")[0])
        self.assertEqual(200, self.request("/styles.css")[0])
        self.assertEqual(200, self.request("/app.js")[0])

        opened = self.open_project()
        self.assertEqual(str(self.root), opened["project"]["root"])
        self.assertEqual(1, len(opened["files"]))
        restored = self.request("/api/project")[1]
        self.assertEqual(1, len(restored["files"]))

        path = opened["files"][0]["path"]
        snapshot = self.request("/api/file?" + urllib.parse.urlencode({"path": path}))[1]
        first = snapshot["lines"][0]
        updated = self.request(
            "/api/file",
            "PUT",
            {
                "path": path,
                "token": snapshot["token"],
                "updates": [{"id": first["id"], "translation": "Hello."}],
            },
        )[1]
        self.assertEqual("Hello.", updated["lines"][0]["translation"])

        stale_status, stale = self.error(
            "/api/file",
            "PUT",
            {"path": path, "token": snapshot["token"], "updates": []},
        )
        self.assertEqual(409, stale_status)
        self.assertIn("changed on disk", stale["error"]["message"])

    def test_native_folder_picker_returns_selection_and_cancel(self):
        picker = mock.Mock(return_value=str(self.root))
        self.state.folder_picker = picker
        status, selected, _headers = self.request(
            "/api/project/pick",
            "POST",
            {"initial_path": str(self.temporary.name)},
        )
        self.assertEqual(200, status)
        self.assertEqual(str(self.root), selected["path"])
        picker.assert_called_once_with(str(self.temporary.name))

        self.state.folder_picker = mock.Mock(return_value=None)
        cancelled = self.request("/api/project/pick", "POST", {})[1]
        self.assertIsNone(cancelled["path"])

        status, payload = self.error(
            "/api/project/pick", "POST", {"initial_path": 123})
        self.assertEqual(400, status)
        self.assertIn("must be a string", payload["error"]["message"])

    def test_script_tool_job_uses_selected_target_and_reports_output(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return mock.Mock(returncode=0, stdout="scripts extracted\n", stderr="")

        self.state.tool_jobs._runner = runner
        tools = self.request("/api/tools")[1]["toolsets"]
        self.assertEqual(["dasaku", "etutane", "sstar"],
                         [item["id"] for item in tools])

        created = self.request(
            "/api/tool-jobs", "POST",
            {"action": "extract", "path": str(self.root), "toolset": "dasaku"},
        )[1]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.request("/api/tool-jobs/" + created["id"])[1]
            if job["status"] in ("completed", "failed"):
                break
            time.sleep(0.02)
        else:
            self.fail("extract job did not finish")

        self.assertEqual("completed", job["status"], job.get("error"))
        self.assertEqual("scripts extracted\n", job["output"])
        self.assertEqual("dasaku_tools", Path(job["project_path"]).name)
        command, options = calls[0]
        self.assertEqual("extract.py", Path(command[1]).name)
        self.assertEqual(["-p", str(self.root)], command[-2:])
        self.assertEqual(str(Path(command[1]).parent), options["cwd"])
        self.assertFalse(options["check"])

        sstar_target = Path(self.temporary.name) / "shining star game"
        sstar_target.mkdir()
        (sstar_target / "script.dat").write_bytes(b"fixture signature")
        status, payload = self.error(
            "/api/tool-jobs", "POST",
            {"action": "repack", "path": str(sstar_target), "toolset": None})
        self.assertEqual(400, status)
        self.assertIn("explicit confirmation", payload["error"]["message"])
        auto = self.request(
            "/api/tool-jobs", "POST",
            {"action": "repack", "path": str(sstar_target), "toolset": None,
             "confirmed": True},
        )[1]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            detected = self.request("/api/tool-jobs/" + auto["id"])[1]
            if detected["status"] in ("completed", "failed"):
                break
            time.sleep(0.02)
        else:
            self.fail("auto-detected repack job did not finish")
        self.assertEqual("completed", detected["status"], detected.get("error"))
        self.assertEqual("sstar", detected["toolset"])
        self.assertEqual("repack.py", Path(calls[1][0][1]).name)

        ambiguous = Path(self.temporary.name) / "unknown game"
        ambiguous.mkdir()
        status, payload = self.error(
            "/api/tool-jobs", "POST",
            {"action": "extract", "path": str(ambiguous), "toolset": None})
        self.assertEqual(422, status)
        self.assertIn("choose one explicitly", payload["error"]["message"])

    def test_unextracted_target_replaces_the_previous_project(self):
        self.open_project()
        target = Path(self.temporary.name) / "fresh game"
        target.mkdir()
        opened = self.request(
            "/api/project/open", "POST", {"path": str(target)})[1]
        self.assertIsNone(opened["project"])
        self.assertEqual([], opened["files"])
        self.assertEqual(str(target), opened["target"]["path"])
        self.assertFalse(opened["target"]["extracted"])

        restored = self.request("/api/project")[1]
        self.assertIsNone(restored["project"])
        self.assertEqual([], restored["files"])
        self.assertEqual(str(target), restored["target"]["path"])
        self.assertEqual(409, self.error("/api/files")[0])

    def test_dasaku_target_keeps_game_folder_separate_from_tool_corpus(self):
        tool_root = Path(self.temporary.name) / "tool repo"
        corpus = tool_root / "dasaku_tools" / "script"
        corpus.mkdir(parents=True)
        (corpus / "route.json").write_text(
            json.dumps([{"message": "原文", "translated": None}], ensure_ascii=False),
            encoding="utf-8",
        )
        self.state.tool_jobs.tool_root = tool_root

        game = Path(self.temporary.name) / "dasaku game"
        game.mkdir()
        (game / "dasaku_HD.exe").write_bytes(b"fixture")
        opened = self.request(
            "/api/project/open", "POST", {"path": str(game)})[1]
        self.assertEqual(str(game), opened["target"]["path"])
        self.assertEqual(str(tool_root / "dasaku_tools"), opened["project"]["root"])
        self.assertEqual(1, len(opened["files"]))

    def test_security_rejects_traversal_cross_origin_and_remote_lm(self):
        self.open_project()
        status, _payload = self.error(
            "/api/file?" + urllib.parse.urlencode({"path": "../outside.json"})
        )
        self.assertEqual(403, status)

        status, _payload = self.error(
            "/api/settings",
            "PUT",
            {"target_language": "French"},
            origin="https://attacker.example",
        )
        self.assertEqual(403, status)

        status, payload = self.error(
            "/api/settings",
            "PUT",
            {"base_url": "http://example.com:1234/v1"},
        )
        self.assertEqual(400, status)
        self.assertIn("loopback", payload["error"]["message"])

    def test_batch_and_context_settings_are_validated(self):
        self.open_project()
        _status, settings, _headers = self.request(
            "/api/settings",
            "PUT",
            {
                "batch_mode": "characters",
                "batch_limit": 200,
                "context_window": 65536,
                "response_reserve_percent": 15,
                "enable_thinking": False,
            },
        )
        self.assertEqual("characters", settings["batch_mode"])
        self.assertEqual(200, settings["batch_limit"])
        self.assertEqual(65536, settings["context_window"])
        self.assertEqual(15, settings["response_reserve_percent"])
        self.assertFalse(settings["enable_thinking"])

        status, payload = self.error(
            "/api/settings",
            "PUT",
            {"enable_thinking": "no"},
        )
        self.assertEqual(400, status)
        self.assertIn("must be a boolean", payload["error"]["message"])

        status, payload = self.error(
            "/api/settings",
            "PUT",
            {"response_reserve_percent": 51},
        )
        self.assertEqual(400, status)
        self.assertIn("between 5 and 50", payload["error"]["message"])

        status, payload = self.error(
            "/api/settings",
            "PUT",
            {"batch_mode": "tokens"},
        )
        self.assertEqual(400, status)
        self.assertIn("messages", payload["error"]["message"])

    def test_default_settings_persist_and_seed_future_projects(self):
        self.open_project()
        _status, saved, _headers = self.request(
            "/api/settings/defaults", "PUT",
            {
                "model": "default-model",
                "target_language": "French",
                "game_context": "Default terminology.",
                "context_window": 65536,
            },
        )
        self.assertEqual(str(self.defaults_path), saved["path"])
        self.assertEqual("default-model", saved["settings"]["model"])
        self.assertEqual("French", saved["settings"]["target_language"])
        self.assertTrue(self.defaults_path.is_file())

        reloaded = SettingsStore(self.defaults_path).get()
        self.assertEqual("default-model", reloaded["model"])
        self.assertEqual("Default terminology.", reloaded["game_context"])
        self.assertEqual(65536, reloaded["context_window"])

        future_root = Path(self.temporary.name) / "future game"
        future_script = future_root / "script"
        future_script.mkdir(parents=True)
        (future_script / "scene.json").write_text(
            json.dumps([{"message": "未来", "translated": None}], ensure_ascii=False),
            encoding="utf-8",
        )
        future = self.request(
            "/api/project/open", "POST", {"path": str(future_root)})[1]
        self.assertEqual("default-model", future["settings"]["model"])
        self.assertEqual("French", future["settings"]["target_language"])
        self.assertEqual("Default terminology.", future["settings"]["game_context"])

        override_root = Path(self.temporary.name) / "existing project"
        override_script = override_root / "script"
        override_script.mkdir(parents=True)
        (override_script / "scene.json").write_text(
            json.dumps([{"message": "既存", "translated": None}], ensure_ascii=False),
            encoding="utf-8",
        )
        (override_root / ".llm_translation_tools.settings").write_text(
            json.dumps({"target_language": "German"}), encoding="utf-8")
        existing = self.request(
            "/api/project/open", "POST", {"path": str(override_root)})[1]
        self.assertEqual("German", existing["settings"]["target_language"])
        self.assertEqual("default-model", existing["settings"]["model"])

        project_settings = json.loads(
            (self.root / ".llm_translation_tools.settings").read_text(encoding="utf-8"))
        self.assertEqual("French", project_settings["target_language"])

        status, payload = self.error(
            "/api/settings/defaults", "PUT", {"unknown": True})
        self.assertEqual(400, status)
        self.assertIn("unknown settings", payload["error"]["message"])

    @mock.patch("llm_translation_tools.server.LMStudioClient", FakeLMStudioClient)
    def test_job_writes_multiple_files_in_sequence(self):
        (self.script / "scene_b.json").write_text(
            json.dumps([
                {"message": "次の場面。", "translated": None},
            ], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        opened = self.open_project()
        paths = [item["path"] for item in opened["files"]]

        self.request(
            "/api/settings",
            "PUT",
            {"model": "fixture-model", "game_context": "A formal morning greeting."},
        )
        _status, created, _headers = self.request(
            "/api/jobs",
            "POST",
            {"files": paths},
        )
        job_id = created["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.request("/api/jobs/" + job_id)[1]
            if job["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.02)
        else:
            self.fail("translation job did not finish")

        self.assertEqual("completed", job["status"], job.get("error"))
        self.assertEqual(paths, job["written_files"])
        result = self.request("/api/jobs/%s/result" % job_id)[1]
        self.assertEqual(3, len(result["suggestions"]))
        self.assertEqual("Translated line 1", result["suggestions"][0]["suggestion"])

        first = self.request("/api/file?" + urllib.parse.urlencode({"path": paths[0]}))[1]
        second = self.request("/api/file?" + urllib.parse.urlencode({"path": paths[1]}))[1]
        self.assertTrue(all(line["translation"] for line in first["lines"]))
        self.assertEqual("Translated line 1", second["lines"][0]["translation"])
        self.assertEqual(200, self.request(
            "/api/jobs/%s/cancel" % job_id,
            "POST",
            {},
        )[0])

    @mock.patch("llm_translation_tools.server.LMStudioClient", BlockingSecondTurnClient)
    def test_job_saves_each_turn_and_preserves_it_when_cancelled(self):
        (self.script / "scene.json").write_text(
            json.dumps([
                {"message": "一", "translated": None},
                {"message": "二", "translated": None},
                {"message": "三", "translated": None},
            ], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        BlockingSecondTurnClient.reset()
        opened = self.open_project()
        path = opened["files"][0]["path"]
        self.request(
            "/api/settings",
            "PUT",
            {"model": "fixture-model", "batch_mode": "messages", "batch_limit": 1},
        )
        job_id = self.request(
            "/api/jobs", "POST", {"files": [path]},
        )[1]["id"]

        try:
            self.assertTrue(
                BlockingSecondTurnClient.second_started.wait(timeout=5),
                "second request turn did not start",
            )
            partial = self.request(
                "/api/file?" + urllib.parse.urlencode({"path": path})
            )[1]
            self.assertEqual("Translated line 1", partial["lines"][0]["translation"])
            self.assertIsNone(partial["lines"][1]["translation"])
            self.assertIsNone(partial["lines"][2]["translation"])

            self.request("/api/jobs/%s/cancel" % job_id, "POST", {})
        finally:
            BlockingSecondTurnClient.release_second.set()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.request("/api/jobs/" + job_id)[1]
            if job["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.02)
        else:
            self.fail("cancelled translation job did not finish")

        self.assertEqual("cancelled", job["status"], job.get("error"))
        self.assertEqual(2, job["progress"]["completed"])
        saved = self.request(
            "/api/file?" + urllib.parse.urlencode({"path": path})
        )[1]
        self.assertEqual("Translated line 1", saved["lines"][0]["translation"])
        self.assertEqual("Translated line 1", saved["lines"][1]["translation"])
        self.assertIsNone(saved["lines"][2]["translation"])


if __name__ == "__main__":
    unittest.main()
