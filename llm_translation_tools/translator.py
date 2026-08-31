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

MESSAGE_OVERHEAD_TOKENS = 12
MIN_RESPONSE_TOKENS = 64
TURN_RETRY_COUNT = 3


class TranslationError(Exception):
    pass


class TranslationCancelled(TranslationError):
    pass


def _retryable_lmstudio_error(error: LMStudioError) -> bool:
    return (error.status is None or error.status in (408, 429) or
            error.status >= 500)


def _context_overflow_lmstudio_error(error: LMStudioError) -> bool:
    if error.status not in (400, 413, 422):
        return False
    detail = (str(error) + "\n" + (error.body or "")).lower()
    markers = (
        "context length", "context window", "context size", "maximum context",
        "too many tokens", "input too long", "prompt is too long", "n_ctx",
    )
    return any(marker in detail for marker in markers)


def _missing_previous_response_error(error: LMStudioError) -> bool:
    if error.status not in (400, 404, 410):
        return False
    detail = (str(error) + "\n" + (error.body or "")).lower()
    return ("previous_response_id" in detail or
            "previous response" in detail or
            ("response" in detail and "not found" in detail))


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
                 batch_mode: str = "messages", batch_limit: int = 1,
                 ) -> List[TranslationBatch]:
    """Create chronological turns bounded by message count or source characters."""
    if batch_mode not in ("messages", "characters"):
        raise TranslationError("batch_mode must be 'messages' or 'characters'")
    if isinstance(batch_limit, bool) or not isinstance(batch_limit, int) or batch_limit < 1:
        raise TranslationError("batch_limit must be a positive integer")
    by_file: Dict[str, List[Mapping[str, Any]]] = {}
    data_by_file: Dict[str, Mapping[str, Any]] = {}
    for file_data, line in selected:
        path = file_data["path"]
        data_by_file[path] = file_data
        by_file.setdefault(path, []).append(line)

    ordered_paths = [file_data["path"] for file_data in files if file_data["path"] in by_file]
    batches: List[TranslationBatch] = []
    for path in ordered_paths:
        targets = sorted(by_file[path], key=lambda line: line["index"])
        pending: List[Mapping[str, Any]] = []
        pending_characters = 0
        for target in targets:
            target_characters = len(str(target.get("source", "")))
            if batch_mode == "messages":
                full = len(pending) >= batch_limit
            else:
                full = bool(pending) and pending_characters + target_characters > batch_limit
            if full:
                batches.append(TranslationBatch(
                    path, data_by_file[path]["lines"], tuple(pending)))
                pending = []
                pending_characters = 0
            pending.append(target)
            pending_characters += target_characters
        if pending:
            batches.append(TranslationBatch(
                path, data_by_file[path]["lines"], tuple(pending)))
    return batches


def _glossary_aliases(source: str) -> List[str]:
    aliases = [source]
    if source.startswith("【") and source.endswith("】") and len(source) > 2:
        aliases.append(source[1:-1])
    return aliases


def _source_for_prompt(source: str) -> str:
    """Remove visual line wrapping from Japanese source text sent to the model."""
    return re.sub(r"[\r\n\u2028\u2029]", "", source)


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
        parts.append("SPEAKER: " + _source_for_prompt(line["speaker"]))
        translated_speaker = line.get("speaker_translation") or (glossary or {}).get(line["speaker"])
        if translated_speaker:
            parts.append("SPEAKER TRANSLATION: " + translated_speaker)
    if line.get("kind"):
        parts.append("KIND: " + line["kind"])
    segments = line.get("source_segments")
    if isinstance(segments, list) and len(segments) > 1 and all(isinstance(s, str) for s in segments):
        parts.append("SOURCE: " + "".join(_source_for_prompt(segment) for segment in segments))
    else:
        parts.append("SOURCE: " + _source_for_prompt(line["source"]))
    existing = suggested.get(line["id"], line.get("translation"))
    if existing is not None:
        parts.append("CURRENT TRANSLATION: " + existing)
    if line.get("context"):
        parts.append("SOURCE RECORD CONTEXT: " + line["context"])
    parts.append("<<<END %s>>>" % role)
    return "\n".join(parts)


def batch_prompt(batch: TranslationBatch, context_start: int,
                 suggested: Mapping[str, str],
                 glossary: Optional[Mapping[str, str]] = None,
                 omitted_references: int = 0) -> str:
    """Build the next chronological turn, including lines unseen by this conversation."""
    target_ids = {line["id"] for line in batch.targets}
    last = batch.targets[-1]["index"]
    chronological = [line for line in batch.all_lines
                     if context_start <= line["index"] <= last]
    references = [line for line in chronological if line["id"] not in target_ids]
    omitted_ids = {line["id"] for line in references[:omitted_references]}
    blocks = [
        "FILE: " + batch.file_path,
        "This is the next chronological part of the file. REFERENCE lines are context only; "
        "do not return them.",
        "Translate every TARGET line in the listed order.",
    ]
    if omitted_ids:
        blocks.append("[%d older reference line%s omitted to fit the context window.]" %
                      (len(omitted_ids), "" if len(omitted_ids) == 1 else "s"))
    displayed = []
    for line in chronological:
        if line["id"] in omitted_ids:
            continue
        role = "TARGET" if line["id"] in target_ids else "REFERENCE"
        displayed.append(_display_line(line, role, suggested, glossary))
    blocks.append("\n".join(displayed))
    blocks.append(
        'Return exactly: {"translations":[{"id":"the exact target ID",'
        '"translation":"translated text"}, ...]}. Preserve all engine tokens verbatim.'
    )
    return "\n\n".join(blocks)


def estimate_message_tokens(messages: Sequence[Mapping[str, str]]) -> int:
    """Return a conservative, tokenizer-independent upper bound for chat content.

    A byte-fallback tokenizer cannot require more tokens than the text has UTF-8 bytes,
    so counting one token per byte deliberately errs toward trimming too early. The
    fixed allowance covers role markers and the model's chat template.
    """
    return sum(len(message.get("content", "").encode("utf-8")) +
               MESSAGE_OVERHEAD_TOKENS for message in messages)


def response_token_budget(context_window: int, reserve_percent: int) -> int:
    return max(MIN_RESPONSE_TOKENS, context_window * reserve_percent // 100)


def _trim_old_turns(messages: Sequence[Mapping[str, str]], prompt_limit: int,
                    preserve_tail: int) -> List[Dict[str, str]]:
    """Drop oldest complete user/assistant turns while preserving the system and tail."""
    if not messages:
        return []
    system = dict(messages[0])
    tail_start = max(1, len(messages) - preserve_tail)
    history = [dict(message) for message in messages[1:tail_start]]
    required = [dict(message) for message in messages[tail_start:]]
    while history and estimate_message_tokens([system] + history + required) > prompt_limit:
        del history[:min(2, len(history))]
    fitted = [system] + history + required
    if estimate_message_tokens(fitted) > prompt_limit:
        raise TranslationError(
            "the system prompt and current translation turn exceed the configured context window; "
            "increase the context window or shorten the prompt text"
        )
    return fitted


def _drop_oldest_complete_turn(messages: Sequence[Mapping[str, str]],
                               preserve_tail: int) -> Optional[List[Dict[str, str]]]:
    """Drop one oldest user/assistant pair while preserving the system and active turn."""
    if not messages:
        return None
    tail_start = max(1, len(messages) - preserve_tail)
    history = [dict(message) for message in messages[1:tail_start]]
    if (len(history) < 2 or history[0].get("role") != "user" or
            history[1].get("role") != "assistant"):
        return None
    return ([dict(messages[0])] + history[2:] +
            [dict(message) for message in messages[tail_start:]])


def fit_batch_request(history: Sequence[Mapping[str, str]], batch: TranslationBatch,
                      context_start: int, suggested: Mapping[str, str],
                      glossary: Optional[Mapping[str, str]],
                      prompt_limit: int) -> List[Dict[str, str]]:
    """Fit a chronological batch turn, trimming oldest turns and reference lines first."""
    user_content = batch_prompt(batch, context_start, suggested, glossary)
    candidate = [dict(message) for message in history] + [
        {"role": "user", "content": user_content}
    ]

    # Previous completed turns are older than every line in this new turn.
    try:
        return _trim_old_turns(candidate, prompt_limit, preserve_tail=1)
    except TranslationError:
        pass

    target_ids = {target["id"] for target in batch.targets}
    reference_count = sum(
        line["id"] not in target_ids
        for line in batch.all_lines
        if context_start <= line["index"] <= batch.targets[-1]["index"]
    )
    # At this point all removable completed turns have already been discarded. Drop the
    # oldest reference lines from the current turn until its targets fit.
    base_history = [dict(history[0])]
    low, high = 0, reference_count
    fitted_result: Optional[List[Dict[str, str]]] = None
    while low <= high:
        omitted = (low + high) // 2
        content = batch_prompt(batch, context_start, suggested, glossary, omitted)
        request = base_history + [{"role": "user", "content": content}]
        if estimate_message_tokens(request) <= prompt_limit:
            fitted_result = request
            high = omitted - 1
        else:
            low = omitted + 1
    if fitted_result is None:
        raise TranslationError(
            "the system prompt and target lines exceed the configured context window; "
            "increase the context window or shorten the prompt text"
        )
    return fitted_result


class TranslationEngine:
    def __init__(self, client: LMStudioClient):
        self.client = client

    def _complete(self, messages: Sequence[Mapping[str, str]], model: str,
                  temperature: float, max_tokens: int,
                  structured_supported: bool,
                  enable_thinking: bool,
                  previous_response_id: Optional[str] = None,
                  ) -> Tuple[str, bool, Optional[str]]:
        reasoning_effort = "medium" if enable_thinking else "none"
        stateful = getattr(self.client, "response_completion", None)
        using_stateful_responses = callable(stateful)
        # LM Studio currently documents grammar-enforced schemas only for
        # /chat/completions. Stateful /responses output remains strictly validated below.
        response_format = (TRANSLATIONS_RESPONSE_FORMAT
                           if structured_supported and not using_stateful_responses else None)

        def request(active_format: Optional[Mapping[str, Any]]) -> Tuple[str, Optional[str]]:
            if using_stateful_responses:
                result = stateful(
                    messages, model, temperature, max_tokens, active_format,
                    reasoning_effort=reasoning_effort,
                    previous_response_id=previous_response_id,
                )
                return result.content, result.response_id
            return (self.client.chat_completion(
                messages, model, temperature, max_tokens, active_format,
                reasoning_effort=reasoning_effort), None)

        try:
            content, response_id = request(response_format)
            return content, structured_supported and not using_stateful_responses, response_id
        except LMStudioError as exc:
            # Some loaded models/LM Studio versions reject response_format. Retry the same
            # first request without it; parsing remains strict below.
            if (response_format is not None and exc.status in (400, 404, 415, 422) and
                    not _context_overflow_lmstudio_error(exc) and
                    not _missing_previous_response_error(exc)):
                content, response_id = request(None)
                return content, False, response_id
            raise

    def translate(self, files: Sequence[Mapping[str, Any]], settings: Mapping[str, Any],
                  file_paths: Optional[Sequence[str]] = None,
                  line_ids: Optional[Sequence[str]] = None,
                  cancelled: Optional[Callable[[], bool]] = None,
                  progress: Optional[Callable[[int, int, int, int], None]] = None,
                  turn_completed: Optional[
                      Callable[[str, Sequence[Mapping[str, Any]]], None]
                  ] = None,
                  ) -> List[Dict[str, Any]]:
        selected = select_lines(files, file_paths, line_ids)
        if not selected:
            return []
        context_window = int(settings["context_window"])
        reserve_percent = int(settings["response_reserve_percent"])
        max_tokens = response_token_budget(context_window, reserve_percent)
        prompt_limit = context_window - max_tokens
        batches = make_batches(
            files,
            selected,
            settings.get("batch_mode", "messages"),
            settings.get("batch_limit", 1),
        )
        total = len(selected)
        done = 0
        suggestions: List[Dict[str, Any]] = []
        suggested: Dict[str, str] = {}
        glossary = speaker_glossary(files)
        structured_supported = True
        current_file: Optional[str] = None
        history: List[Dict[str, str]] = []
        previous_response_id: Optional[str] = None
        context_start = 0

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
                # Retain dialogue history across turns only within the same file.
                history = [{"role": "system", "content": system_content}]
                previous_response_id = None
                current_file = batch.file_path
                context_start = 0
            request_messages = fit_batch_request(
                history, batch, context_start, suggested, glossary, prompt_limit)
            can_continue = (
                previous_response_id is not None and
                request_messages[:-1] == history
            )
            attempt_messages = request_messages
            attempt_input = ([dict(request_messages[-1])] if can_continue
                             else [dict(message) for message in request_messages])
            attempt_previous_response_id = previous_response_id if can_continue else None
            parsed: Optional[List[Dict[str, str]]] = None
            repair_attempt = 0
            request_failures = 0
            successful_response_id: Optional[str] = None
            while repair_attempt <= TURN_RETRY_COUNT:
                if (repair_attempt or request_failures) and cancelled and cancelled():
                    raise TranslationCancelled("translation cancelled")
                try:
                    raw, structured_supported, response_id = self._complete(
                        attempt_input, settings["model"],
                        float(settings["temperature"]), max_tokens,
                        structured_supported,
                        bool(settings.get("enable_thinking", True)),
                        attempt_previous_response_id)
                except LMStudioError as error:
                    if (attempt_previous_response_id is not None and
                            _missing_previous_response_error(error)):
                        attempt_input = [dict(message) for message in attempt_messages]
                        attempt_previous_response_id = None
                        previous_response_id = None
                        continue
                    if _context_overflow_lmstudio_error(error):
                        preserve_tail = 3 if repair_attempt else 1
                        trimmed = _drop_oldest_complete_turn(
                            attempt_messages, preserve_tail)
                        if trimmed is not None:
                            attempt_messages = trimmed
                            if repair_attempt:
                                request_messages = attempt_messages[:-2]
                            else:
                                request_messages = attempt_messages
                            attempt_input = [dict(message) for message in attempt_messages]
                            attempt_previous_response_id = None
                            previous_response_id = None
                            continue
                    if (request_failures >= TURN_RETRY_COUNT or
                            not _retryable_lmstudio_error(error)):
                        if request_failures:
                            raise TranslationError(
                                "LM Studio request failed after %d retries: %s" %
                                (request_failures, error)
                            ) from error
                        raise
                    request_failures += 1
                    continue

                request_failures = 0
                try:
                    parsed = parse_translation_response(raw, batch.targets)
                except TranslationError as error:
                    if repair_attempt >= TURN_RETRY_COUNT:
                        raise TranslationError(
                            "model response stayed invalid after %d retries: %s" %
                            (repair_attempt, error)
                        ) from error
                    repair_attempt += 1
                    repair = (
                        "Your previous response was invalid: %s\nRetry this same turn now. "
                        "Return only the exact JSON object, with every requested ID once and "
                        "in order. Preserve every engine token from its source line." % error
                    )
                    repair_candidate = request_messages + [
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content": repair},
                    ]
                    attempt_messages = _trim_old_turns(
                        repair_candidate, prompt_limit, preserve_tail=3)
                    repair_trimmed = attempt_messages != repair_candidate
                    request_messages = attempt_messages[:-2]
                    # The first repair can branch directly from the invalid response. Later
                    # repairs restart from canonical history so retry chatter does not grow.
                    if response_id is not None and repair_attempt == 1 and not repair_trimmed:
                        attempt_input = [{"role": "user", "content": repair}]
                        attempt_previous_response_id = response_id
                    else:
                        attempt_input = [dict(message) for message in attempt_messages]
                        attempt_previous_response_id = None
                    continue

                # Future context only needs the valid canonical turn, not retry chatter.
                history = request_messages + [{"role": "assistant", "content": raw}]
                successful_response_id = response_id if repair_attempt == 0 else None
                break

            if parsed is None:
                raise TranslationError("translation turn ended without a valid response")
            previous_response_id = successful_response_id

            targets_by_id = {line["id"]: line for line in batch.targets}
            turn_suggestions: List[Dict[str, Any]] = []
            for row in parsed:
                line = targets_by_id[row["id"]]
                suggested[row["id"]] = row["translation"]
                if line.get("kind") == "name":
                    for alias in _glossary_aliases(line["source"]):
                        glossary[alias] = row["translation"]
                turn_suggestions.append({
                    "id": row["id"],
                    "file": batch.file_path,
                    "source": line["source"],
                    "previous_translation": line.get("translation"),
                    "suggestion": row["translation"],
                    "speaker": line.get("speaker"),
                    "kind": line.get("kind"),
                })
            if turn_completed:
                turn_completed(batch.file_path, turn_suggestions)
            suggestions.extend(turn_suggestions)
            done += len(parsed)
            context_start = batch.targets[-1]["index"] + 1
            if progress:
                progress(done, total, batch_number, len(batches))
        return suggestions
