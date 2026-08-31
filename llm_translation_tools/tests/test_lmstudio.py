import json
import unittest

from llm_translation_tools.lmstudio import LMStudioClient


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


class LMStudioClientTests(unittest.TestCase):
    def test_chat_completion_sends_reasoning_effort(self):
        captured = {}

        def opener(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        client = LMStudioClient(opener=opener)
        result = client.chat_completion(
            [{"role": "user", "content": "Translate this."}],
            "fixture-model",
            reasoning_effort="none",
        )

        self.assertEqual("done", result)
        self.assertEqual("none", captured["payload"]["reasoning_effort"])

    def test_response_completion_sends_stateful_responses_payload(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse({
                "id": "resp_next",
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": "{\"ok\":true}"}],
                }],
            })

        client = LMStudioClient(opener=opener)
        result = client.response_completion(
            [{"role": "user", "content": "Continue."}],
            "fixture-model",
            max_tokens=512,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "fixture",
                    "strict": True,
                    "schema": {"type": "object"},
                },
            },
            reasoning_effort="medium",
            previous_response_id="resp_previous",
        )

        self.assertEqual("{\"ok\":true}", result.content)
        self.assertEqual("resp_next", result.response_id)
        self.assertTrue(captured["url"].endswith("/v1/responses"))
        self.assertEqual([{"role": "user", "content": "Continue."}],
                         captured["payload"]["input"])
        self.assertEqual("resp_previous", captured["payload"]["previous_response_id"])
        self.assertEqual(512, captured["payload"]["max_output_tokens"])
        self.assertEqual({"effort": "medium"}, captured["payload"]["reasoning"])
        self.assertEqual("json_schema", captured["payload"]["text"]["format"]["type"])
        self.assertEqual("fixture", captured["payload"]["text"]["format"]["name"])


if __name__ == "__main__":
    unittest.main()
