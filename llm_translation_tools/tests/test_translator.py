import copy
import json
import unittest

from llm_translation_tools.lmstudio import LMStudioError
from llm_translation_tools.translator import (
    TranslationEngine,
    TranslationError,
    parse_translation_response,
)


SETTINGS = {
    "model": "local-model",
    "system_prompt": "Translate into {target_language}. Keep continuity.",
    "game_context": "A mystery. Hana is formal and Taro is terse.",
    "target_language": "English",
    "temperature": 0.2,
    "max_tokens": 512,
    "batch_size": 1,
    "context_before": 1,
    "context_after": 1,
}


def line(path, index, source, translation=None, speaker=None, source_segments=None):
    value = {
        "id": "%s#/%d" % (path, index),
        "index": index,
        "source": source,
        "translation": translation,
        "speaker": speaker,
        "kind": "dialogue" if speaker else "narration",
        "translatable": True,
        "context": None,
        "metadata": {},
    }
    if source_segments is not None:
        value["source_segments"] = source_segments
    return value


class RecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_completion(self, messages, model, temperature, max_tokens,
                        response_format=None):
        self.calls.append({
            "messages": copy.deepcopy(list(messages)),
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": copy.deepcopy(response_format),
        })
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response_for(line_id, translation):
    return json.dumps({
        "translations": [{"id": line_id, "translation": translation}],
    }, ensure_ascii=False)


class ResponseValidationTests(unittest.TestCase):
    def test_preserves_exact_ids_order_and_engine_tokens(self):
        expected = [line("script/a.json", 0, "待って\\x81 «FE»")]
        result = parse_translation_response(
            response_for(expected[0]["id"], "Wait\\x81 «FE»"),
            expected,
        )
        self.assertEqual("Wait\\x81 «FE»", result[0]["translation"])

        with self.assertRaises(TranslationError):
            parse_translation_response(
                response_for(expected[0]["id"], "Wait"),
                expected,
            )

    def test_rejects_empty_model_output(self):
        expected = [line("script/a.json", 0, "待って")]
        with self.assertRaises(TranslationError):
            parse_translation_response(response_for(expected[0]["id"], "  "), expected)


class TranslationCycleTests(unittest.TestCase):
    def test_context_history_is_file_local_and_sequential(self):
        path_a = "script/a.json"
        path_b = "script/b.json"
        a_lines = [
            line(path_a, 0, "おはよう", speaker="花子", source_segments=["おは", "よう"]),
            line(path_a, 1, "まだ眠い"),
        ]
        b_lines = [line(path_b, 0, "誰だ？", speaker="太郎")]
        glossary_line = line("script/_names.json", 0, "花子", "Hana")
        glossary_line["kind"] = "name"
        files = [
            {"path": path_a, "lines": a_lines},
            {"path": path_b, "lines": b_lines},
            {"path": "script/_names.json", "schema": "glossary", "lines": [glossary_line]},
        ]
        client = RecordingClient([
            response_for(a_lines[0]["id"], "Good morning."),
            response_for(a_lines[1]["id"], "I'm still sleepy."),
            response_for(b_lines[0]["id"], "Who is it?"),
        ])

        suggestions = TranslationEngine(client).translate(
            files, SETTINGS, file_paths=[path_a, path_b]
        )

        self.assertEqual(3, len(suggestions))
        self.assertEqual(["system", "user"], [
            message["role"] for message in client.calls[0]["messages"]
        ])
        self.assertEqual(["system", "user", "assistant", "user"], [
            message["role"] for message in client.calls[1]["messages"]
        ])
        self.assertEqual(["system", "user"], [
            message["role"] for message in client.calls[2]["messages"]
        ])
        self.assertIn("Hana is formal", client.calls[0]["messages"][0]["content"])
        self.assertIn("SPEAKER TRANSLATION: Hana", client.calls[0]["messages"][-1]["content"])
        self.assertIn(
            "SOURCE SEGMENTS (chronological on-screen lines):\n  1. おは\n  2. よう",
            client.calls[0]["messages"][-1]["content"],
        )
        self.assertIn("Good morning.", client.calls[1]["messages"][-1]["content"])
        self.assertIn("<<<REFERENCE", client.calls[1]["messages"][-1]["content"])
        self.assertIsNotNone(client.calls[0]["response_format"])

    def test_invalid_response_gets_one_contextual_repair(self):
        target = line("script/a.json", 0, "こんにちは")
        valid = response_for(target["id"], "Hello.")
        client = RecordingClient(["not json", valid])

        result = TranslationEngine(client).translate(
            [{"path": "script/a.json", "lines": [target]}],
            SETTINGS,
        )

        self.assertEqual("Hello.", result[0]["suggestion"])
        self.assertEqual(
            ["system", "user", "assistant", "user"],
            [message["role"] for message in client.calls[1]["messages"]],
        )
        self.assertIn("previous response was invalid", client.calls[1]["messages"][-1]["content"])

    def test_structured_output_rejection_falls_back_to_plain_json(self):
        target = line("script/a.json", 0, "こんにちは")
        client = RecordingClient([
            LMStudioError("unsupported response format", status=400),
            response_for(target["id"], "Hello."),
        ])

        result = TranslationEngine(client).translate(
            [{"path": "script/a.json", "lines": [target]}],
            SETTINGS,
        )

        self.assertEqual("Hello.", result[0]["suggestion"])
        self.assertIsNotNone(client.calls[0]["response_format"])
        self.assertIsNone(client.calls[1]["response_format"])


if __name__ == "__main__":
    unittest.main()
