import json
import unittest

from llm_translation_tools.lmstudio import LMStudioClient


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({
            "choices": [{"message": {"content": "done"}}],
        }).encode("utf-8")


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


if __name__ == "__main__":
    unittest.main()
