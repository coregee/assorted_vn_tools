"""Context-aware, sequential translation orchestration for extracted VN scripts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .lmstudio import (LMStudioClient, LMStudioError,
                       TRANSLATIONS_RESPONSE_FORMAT)


DEFAULT_SYSTEM_PROMPT = """You are translating a Japanese visual novel into polished,
natural {target_language}. Preserve character voice, subtext, terminology, names, and the
relationship implied by honorifics. Use the supplied chronological scene context instead
of translating lines in isolation. Do not add explanations. Preserve every engine token
exactly, including literal \\xHH and «HH» tokens. Return only the requested JSON object and
one translation for every target ID, in the same order."""

HISTORY_MAX_MESSAGES = 16
HISTORY_MAX_CHARS = 24000


class TranslationError(Exception):
    pass


class TranslationCancelled(TranslationError):
    pass


@dataclass
class TranslationBatch:
    file_path: str
    all_lines: Sequence[Mapping[str, Any]]
    targets: Sequence[Mapping[str, Any]]


_ENGINE_TOKEN = re.compile(r"(?:\\x[0-9A-Fa-f]{2}|«[0-9A-Fa-f]{2}»)")


def _engine_tokens(text: str) -> List[str]:
    return _ENGINE_TOKEN.findall(text)


def parse_translation_response(raw: str,
                               expected: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """Strictly validate response structure, ID order, and engine-token preservation."""
    text = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TranslationError("response is not valid JSON: %s" % exc) from exc
    if not isinstance(value, dict) or set(value) != {"translations"}:
        raise TranslationError("response must be an object containing only 'translations'")
    rows = value["translations"]
    if not isinstance(rows, list):
        raise TranslationError("'translations' must be an array")
    expected_ids = [line["id"] for line in expected]
    if len(rows) != len(expected_ids):
        raise TranslationError("expected %d translations, received %d" %
                               (len(expected_ids), len(rows)))
    result: List[Dict[str, str]] = []
    for index, (row, expected_line) in enumerate(zip(rows, expected)):
        if not isinstance(row, dict) or set(row) != {"id", "translation"}:
            raise TranslationError("translation %d must contain only 'id' and 'translation'" % index)
        if row.get("id") != expected_line["id"]:
            raise TranslationError("translation IDs must exactly match the requested order")
        translated = row.get("translation")
        if not isinstance(translated, str):
            raise TranslationError("translation for %s must be a string" % row.get("id"))
        if not translated.strip():
            raise TranslationError("translation for %s is empty" % row.get("id"))
        source_tokens = _engine_tokens(expected_line["source"])
        translated_tokens = _engine_tokens(translated)
        if translated_tokens != source_tokens:
            raise TranslationError("translation for %s changed or dropped engine tokens" % row["id"])
        result.append({"id": row["id"], "translation": translated})
    return result


def select_lines(files: Sequence[Mapping[str, Any]],
                 file_paths: Optional[Sequence[str]] = None,
                 line_ids: Optional[Sequence[str]] = None) -> List[Tuple[Mapping[str, Any], Mapping[str, Any]]]:
    if file_paths is not None and not all(isinstance(path, str) for path in file_paths):
        raise TranslationError("files must contain relative path strings")
    if line_ids is not None and not all(isinstance(line_id, str) for line_id in line_ids):
        raise TranslationError("line_ids must contain strings")
    requested_files = set(file_paths or ())
    requested_ids = set(line_ids or ())
    if len(requested_ids) != len(line_ids or ()):
        raise TranslationError("line_ids contains duplicates")

    found_files = set()
    found_ids = set()
    selected: List[Tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for file_data in files:
        path = file_data["path"]
        if requested_files and path not in requested_files:
            continue
        found_files.add(path)
        for line in file_data["lines"]:
            if requested_ids:
                if line["id"] not in requested_ids:
                    continue
                if line.get("translatable"):
                    found_ids.add(line["id"])
            elif "translation_active" in line:
                if line.get("translation_active"):
                    continue
            else:
                translated = line.get("translation")
                empty_applies = file_data.get("schema") in ("dasaku", "dasaku-ui", "generic")
                if translated is not None and (empty_applies or bool(translated)):
                    continue
            if line.get("translatable"):
                selected.append((file_data, line))

    missing_files = requested_files - found_files
    if missing_files:
        raise TranslationError("unknown files: %s" % ", ".join(sorted(missing_files)))
    missing_ids = requested_ids - found_ids
    if missing_ids:
        raise TranslationError("unknown or protected line IDs: %s" % ", ".join(sorted(missing_ids)))
    return selected


def make_batches(files: Sequence[Mapping[str, Any]],
                 selected: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]],
                 batch_size: int, context_before: int,
                 context_after: int) -> List[TranslationBatch]:
    """Create chronological, file-local batches; split very sparse selections."""
    by_file: Dict[str, List[Mapping[str, Any]]] = {}
    data_by_file: Dict[str, Mapping[str, Any]] = {}
    for file_data, line in selected:
        path = file_data["path"]
        data_by_file[path] = file_data
        by_file.setdefault(path, []).append(line)

    ordered_paths = [file_data["path"] for file_data in files if file_data["path"] in by_file]
    batches: List[TranslationBatch] = []
    proximity = max(2, context_before + context_after + 1)
    for path in ordered_paths:
        targets = sorted(by_file[path], key=lambda line: line["index"])
        group: List[Mapping[str, Any]] = []
        for target in targets:
            too_far = bool(group and target["index"] - group[-1]["index"] > proximity)
            if group and (len(group) >= batch_size or too_far):
                batches.append(TranslationBatch(path, data_by_file[path]["lines"], tuple(group)))
                group = []
            group.append(target)
        if group:
            batches.append(TranslationBatch(path, data_by_file[path]["lines"], tuple(group)))
    return batches


def _glossary_aliases(source: str) -> List[str]:
    aliases = [source]
    if source.startswith("【") and source.endswith("】") and len(source) > 2:
        aliases.append(source[1:-1])
    return aliases


def speaker_glossary(files: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    glossary: Dict[str, str] = {}
    for file_data in files:
        if file_data.get("schema") != "glossary":
            continue
        for line in file_data.get("lines", ()):
            translated = line.get("translation")
            active = line.get("translation_active", translated is not None and bool(translated))
            if not active or not isinstance(translated, str) or not translated:
                continue
            for alias in _glossary_aliases(line["source"]):
                glossary[alias] = translated
    return glossary


def _display_line(line: Mapping[str, Any], role: str,
                  suggested: Mapping[str, str],
                  glossary: Optional[Mapping[str, str]] = None) -> str:
    parts = ["<<<%s %s>>>" % (role, json.dumps(line["id"], ensure_ascii=False))]
    if line.get("speaker"):
        parts.append("SPEAKER: " + line["speaker"])
        translated_speaker = line.get("speaker_translation") or (glossary or {}).get(line["speaker"])
        if translated_speaker:
            parts.append("SPEAKER TRANSLATION: " + translated_speaker)
    if line.get("kind"):
        parts.append("KIND: " + line["kind"])
    segments = line.get("source_segments")
    if isinstance(segments, list) and len(segments) > 1 and all(isinstance(s, str) for s in segments):
        parts.append("SOURCE SEGMENTS (chronological on-screen lines):")
        parts.extend("  %d. %s" % (number, segment)
                     for number, segment in enumerate(segments, 1))
    else:
        parts.append("SOURCE: " + line["source"])
    existing = suggested.get(line["id"], line.get("translation"))
    if existing is not None:
        parts.append("CURRENT TRANSLATION: " + existing)
    if line.get("context"):
        parts.append("SOURCE RECORD CONTEXT: " + line["context"])
    parts.append("<<<END %s>>>" % role)
    return "\n".join(parts)


def batch_prompt(batch: TranslationBatch, context_before: int, context_after: int,
                 suggested: Mapping[str, str],
                 glossary: Optional[Mapping[str, str]] = None) -> str:
    target_ids = {line["id"] for line in batch.targets}
    first = batch.targets[0]["index"]
    last = batch.targets[-1]["index"]
    start = max(0, first - context_before)
    end = min(len(batch.all_lines), last + context_after + 1)
    context = [line for line in batch.all_lines[start:end] if line["id"] not in target_ids]
    blocks = [
        "FILE: " + batch.file_path,
        "The REFERENCE lines are chronological context only; do not return them.",
        "Translate every TARGET line in the listed order.",
    ]
    if context:
        blocks.append("\n".join(_display_line(line, "REFERENCE", suggested, glossary)
                                for line in context))
    blocks.append("\n".join(_display_line(line, "TARGET", suggested, glossary)
                            for line in batch.targets))
    blocks.append(
        'Return exactly: {"translations":[{"id":"the exact target ID",'
        '"translation":"translated text"}, ...]}. Preserve all engine tokens verbatim.'
    )
    return "\n\n".join(blocks)


def trim_history(history: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    """Keep system context plus a bounded sliding window of complete chat pairs."""
    if not history:
        return []
    system = dict(history[0])
    tail = [dict(message) for message in history[1:]]
    while len(tail) > HISTORY_MAX_MESSAGES or sum(len(m.get("content", "")) for m in tail) > HISTORY_MAX_CHARS:
        # Stored history is appended as complete user/assistant pairs, including repair pairs.
        del tail[:min(2, len(tail))]
    return [system] + tail


class TranslationEngine:
    def __init__(self, client: LMStudioClient):
        self.client = client

    def _complete(self, messages: Sequence[Mapping[str, str]], model: str,
                  temperature: float, max_tokens: int,
                  structured_supported: bool) -> Tuple[str, bool]:
        response_format = TRANSLATIONS_RESPONSE_FORMAT if structured_supported else None
        try:
            return self.client.chat_completion(
                messages, model, temperature, max_tokens, response_format), structured_supported
        except LMStudioError as exc:
            # Some loaded models/LM Studio versions reject response_format. Retry the same
            # first request without it; parsing remains strict below.
            if structured_supported and exc.status is not None and 400 <= exc.status < 500:
                return self.client.chat_completion(
                    messages, model, temperature, max_tokens, None), False
            raise

    def translate(self, files: Sequence[Mapping[str, Any]], settings: Mapping[str, Any],
                  file_paths: Optional[Sequence[str]] = None,
                  line_ids: Optional[Sequence[str]] = None,
                  cancelled: Optional[Callable[[], bool]] = None,
                  progress: Optional[Callable[[int, int, int, int], None]] = None
                  ) -> List[Dict[str, Any]]:
        selected = select_lines(files, file_paths, line_ids)
        if not selected:
            return []
        batch_size = int(settings["batch_size"])
        context_before = int(settings["context_before"])
        context_after = int(settings["context_after"])
        batches = make_batches(files, selected, batch_size, context_before, context_after)
        total = len(selected)
        done = 0
        suggestions: List[Dict[str, Any]] = []
        suggested: Dict[str, str] = {}
        glossary = speaker_glossary(files)
        structured_supported = True
        current_file: Optional[str] = None
        history: List[Dict[str, str]] = []

        custom_prompt = settings.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        custom_prompt = custom_prompt.replace("{target_language}", settings["target_language"])
        system_content = (
            custom_prompt.rstrip() + "\n\nTARGET LANGUAGE:\n" + settings["target_language"] +
            "\n\nGAME CONTEXT:\n" + (settings.get("game_context") or "No game context provided.")
        )

        for batch_number, batch in enumerate(batches, 1):
            if cancelled and cancelled():
                raise TranslationCancelled("translation cancelled")
            if batch.file_path != current_file:
                # Script filenames are not guaranteed to have story-contiguous ordering.
                # Retain dialogue history across batches only within the same file.
                history = [{"role": "system", "content": system_content}]
                current_file = batch.file_path
            history = trim_history(history)
            user_content = batch_prompt(batch, context_before, context_after, suggested, glossary)
            request_messages = history + [{"role": "user", "content": user_content}]
            raw, structured_supported = self._complete(
                request_messages, settings["model"], float(settings["temperature"]),
                int(settings["max_tokens"]), structured_supported)
            try:
                parsed = parse_translation_response(raw, batch.targets)
                history.extend((
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": raw},
                ))
            except TranslationError as first_error:
                if cancelled and cancelled():
                    raise TranslationCancelled("translation cancelled")
                repair = (
                    "Your previous response was invalid: %s\nRepair it now. Return only the "
                    "exact JSON object, with every requested ID once and in order. Preserve every "
                    "engine token from its source line." % first_error
                )
                repair_messages = request_messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": repair},
                ]
                repaired, structured_supported = self._complete(
                    repair_messages, settings["model"], float(settings["temperature"]),
                    int(settings["max_tokens"]), structured_supported)
                try:
                    parsed = parse_translation_response(repaired, batch.targets)
                except TranslationError as second_error:
                    raise TranslationError("model response stayed invalid after one repair: %s" %
                                           second_error) from second_error
                history.extend((
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": repair},
                    {"role": "assistant", "content": repaired},
                ))

            targets_by_id = {line["id"]: line for line in batch.targets}
            for row in parsed:
                line = targets_by_id[row["id"]]
                suggested[row["id"]] = row["translation"]
                if line.get("kind") == "name":
                    for alias in _glossary_aliases(line["source"]):
                        glossary[alias] = row["translation"]
                suggestions.append({
                    "id": row["id"],
                    "file": batch.file_path,
                    "source": line["source"],
                    "previous_translation": line.get("translation"),
                    "suggestion": row["translation"],
                    "speaker": line.get("speaker"),
                    "kind": line.get("kind"),
                })
            done += len(parsed)
            if progress:
                progress(done, total, batch_number, len(batches))
        return suggestions
