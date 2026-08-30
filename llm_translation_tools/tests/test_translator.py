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
    "batch_mode": "messages",
    "batch_limit": 1,
    "context_window": 8192,
    "response_reserve_percent": 25,
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


def response_for_many(rows):
    return json.dumps({
        "translations": [
            {"id": target["id"], "translation": translation}
            for target, translation in rows
        ],
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
    def test_batches_by_message_count_without_crossing_file_boundaries(self):
        path = "script/a.json"
        lines = [line(path, index, source) for index, source in enumerate(("一", "二", "三"))]
        client = RecordingClient([
            response_for_many(((lines[0], "One."), (lines[1], "Two."))),
            response_for(lines[2]["id"], "Three."),
        ])

        committed = []
        result = TranslationEngine(client).translate(
            [{"path": path, "lines": lines}],
            {**SETTINGS, "batch_limit": 2},
            turn_completed=lambda file_path, rows: committed.append(
                (file_path, [row["id"] for row in rows])),
        )

        self.assertEqual(3, len(result))
        self.assertEqual(2, len(client.calls))
        self.assertIn(lines[0]["id"], client.calls[0]["messages"][-1]["content"])
        self.assertIn(lines[1]["id"], client.calls[0]["messages"][-1]["content"])
        self.assertNotIn(lines[2]["id"], client.calls[0]["messages"][-1]["content"])
        self.assertEqual([
            (path, [lines[0]["id"], lines[1]["id"]]),
            (path, [lines[2]["id"]]),
        ], committed)

    def test_batches_by_source_characters_and_keeps_oversized_message(self):
        path = "script/a.json"
        lines = [
            line(path, 0, "a" * 210),
            line(path, 1, "b" * 60),
            line(path, 2, "c" * 70),
            line(path, 3, "d" * 66),
            line(path, 4, "e" * 5),
        ]
        client = RecordingClient([
            response_for(lines[0]["id"], "Long."),
            response_for_many(tuple(
                (lines[index], "Batch %d." % index) for index in (1, 2, 3)
            )),
            response_for(lines[4]["id"], "Last."),
        ])

        TranslationEngine(client).translate(
            [{"path": path, "lines": lines}],
            {**SETTINGS, "batch_mode": "characters", "batch_limit": 200},
        )

        self.assertEqual(3, len(client.calls))
        middle_prompt = client.calls[1]["messages"][-1]["content"]
        for index in (1, 2, 3):
            self.assertIn(lines[index]["id"], middle_prompt)
        self.assertNotIn(lines[4]["id"], middle_prompt)

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
        self.assertNotIn("まだ眠い", client.calls[0]["messages"][-1]["content"])
        self.assertIn("Good morning.", client.calls[1]["messages"][-2]["content"])
        self.assertNotIn("おはよう", client.calls[1]["messages"][-1]["content"])
        self.assertNotIn("<<<REFERENCE", client.calls[1]["messages"][-1]["content"])
        self.assertEqual(2048, client.calls[0]["max_tokens"])
        self.assertIsNotNone(client.calls[0]["response_format"])

    def test_first_turn_for_a_late_target_contains_all_past_lines_but_no_future(self):
        path = "script/a.json"
        lines = [
            line(path, 0, "最初の行", "The first line."),
            line(path, 1, "二番目の行"),
            line(path, 2, "翻訳対象"),
            line(path, 3, "未来の行"),
        ]
        client = RecordingClient([response_for(lines[2]["id"], "The target.")])

        TranslationEngine(client).translate(
            [{"path": path, "lines": lines}], SETTINGS, line_ids=[lines[2]["id"]]
        )

        prompt = client.calls[0]["messages"][-1]["content"]
        self.assertLess(prompt.index("最初の行"), prompt.index("二番目の行"))
        self.assertLess(prompt.index("二番目の行"), prompt.index("翻訳対象"))
        self.assertIn("CURRENT TRANSLATION: The first line.", prompt)
        self.assertNotIn("未来の行", prompt)

    def test_old_reference_lines_are_trimmed_to_reserve_response_space(self):
        path = "script/a.json"
        oldest = "古" * 160
        lines = [
            line(path, 0, oldest),
            line(path, 1, "近" * 80),
            line(path, 2, "翻訳対象"),
        ]
        settings = {
            **SETTINGS,
            "context_window": 1400,
            "response_reserve_percent": 25,
        }
        client = RecordingClient([response_for(lines[2]["id"], "The target.")])

        TranslationEngine(client).translate(
            [{"path": path, "lines": lines}], settings, line_ids=[lines[2]["id"]]
        )

        prompt = client.calls[0]["messages"][-1]["content"]
        self.assertIn("older reference line", prompt)
        self.assertNotIn(oldest, prompt)
        self.assertIn("翻訳対象", prompt)
        self.assertEqual(350, client.calls[0]["max_tokens"])

    def test_old_completed_turns_are_dropped_as_pairs_when_history_fills(self):
        path = "script/a.json"
        lines = [
            line(path, 0, "一" * 100),
            line(path, 1, "二" * 100),
        ]
        settings = {
            **SETTINGS,
            "context_window": 1500,
            "response_reserve_percent": 25,
        }
        client = RecordingClient([
            response_for(lines[0]["id"], "First."),
            response_for(lines[1]["id"], "Second."),
        ])

        TranslationEngine(client).translate(
            [{"path": path, "lines": lines}], settings
        )

        self.assertEqual(
            ["system", "user"],
            [message["role"] for message in client.calls[1]["messages"]],
        )
        self.assertNotIn("一" * 100, client.calls[1]["messages"][-1]["content"])
        self.assertIn("二" * 100, client.calls[1]["messages"][-1]["content"])

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

    def test_timeout_retries_the_same_turn_before_committing(self):
        target = line("script/a.json", 0, "こんにちは")
        client = RecordingClient([
            LMStudioError("request timed out"),
            response_for(target["id"], "Hello."),
        ])
        committed = []

        result = TranslationEngine(client).translate(
            [{"path": "script/a.json", "lines": [target]}],
            SETTINGS,
            turn_completed=lambda _path, rows: committed.append(list(rows)),
        )

        self.assertEqual("Hello.", result[0]["suggestion"])
        self.assertEqual(2, len(client.calls))
        self.assertEqual(client.calls[0]["messages"], client.calls[1]["messages"])
        self.assertEqual(1, len(committed))

    def test_wrong_line_count_is_retried_until_the_turn_is_complete(self):
        path = "script/a.json"
        targets = [line(path, 0, "一"), line(path, 1, "二")]
        incomplete = response_for(targets[0]["id"], "One.")
        client = RecordingClient([
            incomplete,
            incomplete,
            response_for_many(((targets[0], "One."), (targets[1], "Two."))),
        ])

        result = TranslationEngine(client).translate(
            [{"path": path, "lines": targets}],
            {**SETTINGS, "batch_limit": 2},
        )

        self.assertEqual(2, len(result))
        self.assertEqual(3, len(client.calls))
        self.assertIn("expected 2 translations, received 1",
                      client.calls[1]["messages"][-1]["content"])

    def test_invalid_turn_stops_after_three_retries(self):
        path = "script/a.json"
        targets = [line(path, 0, "一"), line(path, 1, "二")]
        incomplete = response_for(targets[0]["id"], "One.")
        client = RecordingClient([incomplete] * 4)

        with self.assertRaisesRegex(TranslationError, "after 3 retries"):
            TranslationEngine(client).translate(
                [{"path": path, "lines": targets}],
                {**SETTINGS, "batch_limit": 2},
            )

        self.assertEqual(4, len(client.calls))

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
