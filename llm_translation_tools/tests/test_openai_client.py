import json
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path

from llm_translation_tools.openai_client import OpenAIClient
from llm_translation_tools.server import SettingsStore
from llm_translation_tools.translator import TranslationEngine
from llm_translation_tools.tests.test_translator import SETTINGS, line


class FakeResponse:
    def __init__(self, body=None):
        self.body = body or {
            "choices": [{"message": {"content": "done"}}],
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.body).encode("utf-8")


class OpenAIClientTests(unittest.TestCase):
    def test_optional_bearer_auth_applies_to_models_and_chat(self):
        for key in ("", "test-key"):
            with self.subTest(key=key):
                def opener(request, timeout):
                    self.assertEqual("Bearer test-key" if key else None,
                                     request.get_header("Authorization"))
                    return FakeResponse({"data": []} if request.method == "GET" else None)

                client = OpenAIClient(opener=opener, api_key=key)
                client.models()
                client.chat_completion([{"role": "user", "content": "Hello"}], "model")

    def test_real_client_uses_chat_and_resends_history(self):
        calls = []

        def opener(request, timeout):
            payload = json.loads(request.data)
            calls.append((request.full_url, payload))
            return FakeResponse({"choices": [{"message": {
                "content": json.dumps(["Translation %d" % len(calls)])}}]})

        client = OpenAIClient("http://localhost:8000/api/v1/", opener=opener)
        files = [{"path": "scene.json", "lines": [
            line("scene.json", 0, "First"), line("scene.json", 1, "Second")]}]
        result = TranslationEngine(client).translate(files, SETTINGS)
        self.assertEqual(2, len(result))
        for url, payload in calls:
            self.assertEqual("http://localhost:8000/api/v1/chat/completions", url)
            self.assertNotIn("input", payload)
            self.assertNotIn("previous_response_id", payload)
            self.assertNotIn("store", payload)
        self.assertEqual(["system", "user", "assistant", "user"],
                         [m["role"] for m in calls[1][1]["messages"]])
        self.assertEqual('["Translation 1"]', calls[1][1]["messages"][2]["content"])

    def test_models_preserves_custom_api_prefix(self):
        def opener(request, timeout):
            self.assertEqual("http://localhost:8000/api/v1/models", request.full_url)
            self.assertEqual("GET", request.method)
            return FakeResponse({"data": [{"id": "my-model"}]})

        self.assertEqual("my-model", OpenAIClient(opener=opener).models()[0]["id"])

    def test_reasoning_rejection_retries_and_remembers_support(self):
        calls = []

        def opener(request, timeout):
            calls.append(json.loads(request.data))
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {},
                                             io.BytesIO(b'Unsupported reasoning_effort'))
            return FakeResponse()

        client = OpenAIClient(opener=opener)
        for _ in range(2):
            self.assertEqual("done", client.chat_completion(
                [{"role": "user", "content": "Translate"}], "model",
                reasoning_effort="none"))
        self.assertEqual(3, len(calls))
        self.assertIn("reasoning_effort", calls[0])
        self.assertNotIn("reasoning_effort", calls[1])
        self.assertNotIn("reasoning_effort", calls[2])

    def test_legacy_connection_defaults_migrate_without_losing_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "defaults.json"
            path.write_text(json.dumps({"base_url": "http://example.com/v1",
                                        "allow_remote_lmstudio": True}), encoding="utf-8")
            settings = SettingsStore(path)
            self.assertEqual("http://example.com/v1", settings.get()["base_url"])
            self.assertTrue(settings.get()["allow_remote_endpoint"])
            self.assertNotIn("allow_remote_lmstudio", settings.get())
            migrated = settings.merged({"base_url": "http://localhost:8000/api/v1",
                                        "allow_remote_lmstudio": False})
            self.assertFalse(migrated["allow_remote_endpoint"])

    def test_chat_completion_sends_reasoning_effort(self):
        captured = {}

        def opener(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        client = OpenAIClient(opener=opener)
        result = client.chat_completion(
            [{"role": "user", "content": "Translate this."}],
            "fixture-model",
            reasoning_effort="none",
        )

        self.assertEqual("done", result)
        self.assertEqual("none", captured["payload"]["reasoning_effort"])



if __name__ == "__main__":
    unittest.main()
