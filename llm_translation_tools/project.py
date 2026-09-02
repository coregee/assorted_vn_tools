"""Safe project discovery and lossless adapters for extracted VN script JSON.

The editor exposes one normalized line shape, but writes only the translation value
back into the original document.  Unknown keys and mapping insertion order are left
alone so the three game-specific repackers continue to receive their native schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple


# Deliberately not ``*.json``: when the opened root is the script directory itself,
# every existing repacker globs JSON files and would try to ingest application config.
PROJECT_SETTINGS_FILE = ".llm_translation_tools.settings"
PROJECT_REVIEW_FILE = ".llm_translation_tools.review"
PROJECT_SETTING_KEYS = frozenset(
    ("system_prompt", "game_context", "target_language", "model", "enable_thinking", "temperature",
     "context_window", "response_reserve_percent", "context_clear_percent",
     "batch_mode", "batch_limit")
)


class ProjectError(Exception):
    """Base class for errors safe to return through the local HTTP API."""

    status = 400


class ProjectNotOpen(ProjectError):
    status = 409


class UnsafePath(ProjectError):
    status = 403


class FileConflict(ProjectError):
    status = 409


class InvalidScript(ProjectError):
    status = 422


def _inside(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(candidate))) == str(root)
    except (ValueError, OSError):
        return False


def _json_pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _token(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _atomic_json(path: Path, document: Any) -> bytes:
    """Serialize UTF-8 JSON and atomically replace ``path`` in the same directory."""
    text = json.dumps(document, ensure_ascii=False, indent=1)
    raw = text.encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return raw


def _review_flag_items(flag: Mapping[str, Any]) -> List[Dict[str, Any]]:
    nested = flag.get("flags")
    if isinstance(nested, list):
        return [dict(item) for item in nested if isinstance(item, Mapping)]
    return [dict(flag)]


def _packed_review_flags(flags: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    packed = [dict(flag) for flag in flags]
    return packed[0] if len(packed) == 1 else {"flags": packed}


def _display_review_flag(flag: Mapping[str, Any], source: str) -> Optional[Dict[str, Any]]:
    items = [item for item in _review_flag_items(flag)
             if item.get("source") in (None, source)]
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    return {
        "category": "multiple",
        "reason": "\n".join(item.get("reason", "Review this translation.") for item in items),
        "source": source,
        "flags": items,
    }


@dataclass
class _Binding:
    pointer: str
    entry: MutableMapping[str, Any]
    source_key: str
    translation_key: str
    source: str
    translation: Optional[str]
    speaker: Optional[str]
    speaker_translation: Optional[str]
    kind: Optional[str]
    translatable: bool
    context: Optional[str]
    metadata: Dict[str, Any]
    source_segments: List[str]
    empty_is_applied: bool

    @property
    def translation_active(self) -> bool:
        return self.translation is not None and (self.empty_is_applied or bool(self.translation))


_DASAKU_PROTECT_KEY = re.compile(r"(?:VarName|Var\d+)$")


def _line_binding(entry: Any, pointer: str, schema_hint: str) -> Optional[_Binding]:
    if not isinstance(entry, MutableMapping):
        return None

    if isinstance(entry.get("jp"), str):
        source_key = "jp"
        translation_key = "tr" if schema_hint == "sstar" or "tr" in entry else "translated"
    elif isinstance(entry.get("message"), str):
        source_key, translation_key = "message", "translated"
    elif isinstance(entry.get("original"), str):
        source_key, translation_key = "original", "translated"
    elif isinstance(entry.get("source"), str):
        source_key = "source"
        translation_key = "translation" if "translation" in entry else "translated"
    else:
        return None

    translation = entry.get(translation_key)
    if translation is not None and not isinstance(translation, str):
        raise InvalidScript("%s: %s must be a string or null" % (pointer, translation_key))

    speaker_value = entry.get("speaker")
    if speaker_value is None:
        speaker_value = entry.get("name")
    speaker = speaker_value if isinstance(speaker_value, str) else None
    speaker_translation_value = entry.get("speaker_tr")
    if speaker_translation_value is None:
        speaker_translation_value = entry.get("name_translated")
    speaker_translation = (speaker_translation_value
                           if isinstance(speaker_translation_value, str) else None)
    kind_value = entry.get("kind")
    kind = kind_value if isinstance(kind_value, str) else None
    context_value = entry.get("context")
    context = context_value if isinstance(context_value, str) else None
    if schema_hint == "sstar":
        details = []
        if isinstance(entry.get("scene"), str):
            details.append("scene %s" % entry["scene"])
        if isinstance(entry.get("page"), int):
            details.append("page %d" % entry["page"])
        if isinstance(entry.get("tag"), str) and entry["tag"]:
            details.append("choice tag %s" % entry["tag"])
        if details:
            context = ((context + " · ") if context else "") + " · ".join(details)
    note = entry.get("note")
    protected = isinstance(note, str) and "do not translate" in note.lower()
    key_value = entry.get("key")
    if (schema_hint == "dasaku-ui" and isinstance(key_value, str)
            and _DASAKU_PROTECT_KEY.search(key_value)):
        protected = True
    source = entry[source_key]
    source_segments = [source]
    if isinstance(entry.get("jp_lines"), list):
        candidate_segments = entry["jp_lines"]
        if candidate_segments and all(isinstance(segment, str) for segment in candidate_segments):
            source_segments = list(candidate_segments)
            source = "\n".join(source_segments)
    translatable = bool(source.strip()) and not protected
    empty_is_applied = schema_hint in ("dasaku", "dasaku-ui", "generic")

    omitted = {source_key, translation_key, "speaker", "name", "speaker_tr",
               "name_translated", "kind", "context", "jp_lines"}
    metadata = {key: value for key, value in entry.items() if key not in omitted}
    return _Binding(pointer, entry, source_key, translation_key, source, translation,
                    speaker, speaker_translation, kind, translatable, context, metadata, source_segments,
                    empty_is_applied)


def _schema_and_bindings(document: Any) -> Tuple[str, List[_Binding]]:
    bindings: List[_Binding] = []
    if isinstance(document, MutableMapping) and isinstance(document.get("lines"), list):
        schema = "etutane"
        for index, entry in enumerate(document["lines"]):
            binding = _line_binding(entry, "/lines/%d" % index, schema)
            if binding is not None:
                bindings.append(binding)
        return schema, bindings

    if isinstance(document, MutableMapping):
        # Speaker-name glossaries in all three repos are {source: translation|null}.
        if all(isinstance(key, str) and (value is None or isinstance(value, str))
               for key, value in document.items()):
            for key, value in document.items():
                synthetic: MutableMapping[str, Any] = {"source": key, "translation": value}
                bindings.append(_Binding(
                    "/" + _json_pointer_part(key), synthetic, "source", "translation", key,
                    value, None, None, "name", bool(key.strip()), None, {"key": key}, [key], False))
            return "glossary", bindings
        raise InvalidScript("JSON object is not a recognized extracted-script schema")

    if not isinstance(document, list):
        raise InvalidScript("top-level JSON value must be a list or object")

    records = [entry for entry in document if isinstance(entry, Mapping)]
    if any("jp" in entry and ("tr" in entry or "slots" in entry or "scene" in entry)
           for entry in records):
        schema = "sstar"
    elif any("message" in entry for entry in records):
        schema = "dasaku"
    elif any("original" in entry for entry in records):
        schema = "dasaku-ui"
    else:
        schema = "generic"
    for index, entry in enumerate(document):
        binding = _line_binding(entry, "/%d" % index, schema)
        if binding is not None:
            bindings.append(binding)
    # Empty extracted files are valid; non-empty files with no text are asset metadata.
    if document and not bindings:
        raise InvalidScript("JSON list contains no recognized translatable records")
    return schema, bindings


class Project:
    """An opened game directory and its constrained extracted-script workspace."""

    def __init__(self, root: Path, script_root: Path):
        self.root = root
        self.script_root = script_root
        self._lock = threading.RLock()

    @classmethod
    def open(cls, path: str, script_dir: Optional[str] = None,
             base_dir: Optional[Path] = None) -> "Project":
        if not isinstance(path, str) or not path.strip():
            raise ProjectError("path is required")
        try:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = (base_dir or Path.cwd()) / candidate
            root = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProjectError("game folder does not exist: %s" % path) from exc
        if not root.is_dir():
            raise ProjectError("game folder is not a directory: %s" % path)

        if script_dir is not None and not isinstance(script_dir, str):
            raise ProjectError("script_dir must be a path string")
        if script_dir:
            try:
                requested = Path(script_dir).expanduser()
                if not requested.is_absolute():
                    requested = root / requested
                scripts = requested.resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ProjectError("script folder does not exist: %s" % script_dir) from exc
            if not _inside(root, scripts):
                raise UnsafePath("script folder must be inside the opened game folder")
            if not scripts.is_dir():
                raise ProjectError("script folder is not a directory: %s" % script_dir)
        else:
            default = root / "script"
            if default.is_dir():
                scripts = default.resolve(strict=True)
                if not _inside(root, scripts):
                    raise UnsafePath("script folder resolves outside the opened game folder")
            elif any(item.is_file() and item.suffix.lower() == ".json"
                     for item in root.iterdir()):
                scripts = root
            else:
                raise ProjectError("no extracted script folder found (expected %s)" % default)
        return cls(root, scripts)

    @property
    def root_string(self) -> str:
        return str(self.root)

    @property
    def script_dir_string(self) -> str:
        return self._relative(self.script_root)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    def resolve_file(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path:
            raise ProjectError("file path is required")
        try:
            supplied = Path(relative_path)
        except (TypeError, ValueError) as exc:
            raise UnsafePath("invalid file path") from exc
        if supplied.is_absolute():
            raise UnsafePath("file paths must be relative to the opened game folder")
        try:
            candidate = (self.root / supplied).resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise UnsafePath("invalid file path") from exc
        if not _inside(self.root, candidate) or not _inside(self.script_root, candidate):
            raise UnsafePath("file is outside the opened script folder")
        if not candidate.is_file() or candidate.suffix.lower() != ".json":
            raise ProjectError("not a script JSON file: %s" % relative_path)
        return candidate

    def _load_path(self, path: Path) -> Tuple[bytes, Any, str, List[_Binding]]:
        try:
            raw = path.read_bytes()
            document = json.loads(raw.decode("utf-8-sig"))
        except UnicodeDecodeError as exc:
            raise InvalidScript("%s is not UTF-8 JSON" % self._relative(path)) from exc
        except json.JSONDecodeError as exc:
            raise InvalidScript("invalid JSON in %s: %s" % (self._relative(path), exc)) from exc
        schema, bindings = _schema_and_bindings(document)
        return raw, document, schema, bindings

    def _load_review_flags(self) -> Dict[str, Dict[str, Any]]:
        path = self.root / PROJECT_REVIEW_FILE
        if not path.exists():
            return {}
        try:
            resolved = path.resolve(strict=True)
            if not _inside(self.root, resolved) or not resolved.is_file():
                return {}
            value = json.loads(resolved.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(value, Mapping):
            return {}
        return {
            line_id: dict(flag)
            for line_id, flag in value.items()
            if isinstance(line_id, str) and isinstance(flag, Mapping)
        }

    def replace_review_flags(self, category: str,
                             issues: Sequence[Mapping[str, Any]]) -> int:
        """Replace one category of sidecar review flags without touching script JSON.

        Repackers identify entries by their project-relative JSON path and normalized
        JSON pointer. Other review categories (for example engine-token mismatches)
        remain intact. Returns the number of current lines flagged by ``issues``.
        """
        if not isinstance(category, str) or not category.strip():
            raise ProjectError("review flag category is required")
        if not isinstance(issues, list):
            raise ProjectError("review issues must be a JSON array")
        with self._lock:
            review_flags = self._load_review_flags()
            original_review_flags = dict(review_flags)
            retained_flags: Dict[str, Dict[str, Any]] = {}
            for line_id, stored in review_flags.items():
                retained = [flag for flag in _review_flag_items(stored)
                            if flag.get("category") != category]
                if retained:
                    retained_flags[line_id] = _packed_review_flags(retained)
            review_flags = retained_flags
            grouped: Dict[str, Dict[str, Any]] = {}
            issues_by_path: Dict[str, List[Mapping[str, Any]]] = {}
            for issue in issues:
                if not isinstance(issue, Mapping):
                    raise ProjectError("each review issue must be an object")
                relative_path = issue.get("path")
                pointer = issue.get("pointer")
                reason = issue.get("reason")
                if not isinstance(relative_path, str) or not relative_path:
                    raise ProjectError("review issue path is required")
                if not isinstance(pointer, str) or not pointer.startswith("/"):
                    raise ProjectError("review issue pointer is invalid")
                if not isinstance(reason, str) or not reason.strip():
                    raise ProjectError("review issue reason is required")
                details = issue.get("details")
                if details is not None and not isinstance(details, Mapping):
                    raise ProjectError("review issue details must be an object")
                issues_by_path.setdefault(relative_path, []).append(issue)
            for relative_path, file_issues in issues_by_path.items():
                path = self.resolve_file(relative_path)
                _raw, _document, _schema, bindings = self._load_path(path)
                by_pointer = {binding.pointer: binding for binding in bindings}
                for issue in file_issues:
                    pointer = issue["pointer"]
                    reason = issue["reason"].strip()
                    binding = by_pointer.get(pointer)
                    if binding is None:
                        raise ProjectError("review issue points to an unknown line: %s#%s" %
                                           (relative_path, pointer))
                    line_id = self._relative(path) + "#" + pointer
                    current = grouped.get(line_id)
                    if current is None:
                        current = {
                            "category": category,
                            "reason": reason,
                            "source": binding.source,
                        }
                        details = issue.get("details")
                        if details:
                            current["details"] = dict(details)
                        grouped[line_id] = current
                    elif reason not in current["reason"].split("\n"):
                        current["reason"] += "\n" + reason
            for line_id, flag in grouped.items():
                retained = _review_flag_items(review_flags[line_id]) if line_id in review_flags else []
                review_flags[line_id] = _packed_review_flags(retained + [flag])
            if review_flags != original_review_flags:
                _atomic_json(self.root / PROJECT_REVIEW_FILE, review_flags)
            return len(grouped)

    def _normalized(self, path: Path, schema: str, bindings: Sequence[_Binding],
                    review_flags: Optional[Mapping[str, Mapping[str, Any]]] = None,
                    ) -> List[Dict[str, Any]]:
        relative = self._relative(path)
        lines: List[Dict[str, Any]] = []
        for index, binding in enumerate(bindings):
            line_id = relative + "#" + binding.pointer
            stored_review_flag = (review_flags or {}).get(line_id)
            review_flag = (_display_review_flag(stored_review_flag, binding.source)
                           if stored_review_flag else None)
            lines.append({
                "id": line_id,
                "index": index,
                "source": binding.source,
                "source_segments": binding.source_segments,
                "translation": binding.translation,
                "translation_active": binding.translation_active,
                "empty_is_applied": binding.empty_is_applied,
                "speaker": binding.speaker,
                "speaker_translation": binding.speaker_translation,
                "kind": binding.kind,
                "translatable": binding.translatable,
                "context": binding.context,
                "metadata": binding.metadata,
                "review_flag": review_flag,
            })
        return lines

    def list_files(self) -> List[Dict[str, Any]]:
        files: List[Dict[str, Any]] = []
        with self._lock:
            review_flags = self._load_review_flags()
            candidates = (path for path in self.script_root.iterdir()
                          if path.is_file() and path.suffix.lower() == ".json")
            for path in sorted(candidates, key=lambda p: p.as_posix().lower()):
                try:
                    resolved = path.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if (not _inside(self.script_root, resolved)
                        or resolved.name.lower() in ("manifest.json", "_manifest.json")):
                    continue
                try:
                    raw, _document, schema, bindings = self._load_path(resolved)
                except InvalidScript:
                    # The script directory can contain manifests and other JSON assets.
                    continue
                relative = self._relative(resolved)
                files.append({
                    "path": relative,
                    "schema": schema,
                    "line_count": len(bindings),
                    "translatable_count": sum(line.translatable for line in bindings),
                    "translated_count": sum(line.translatable and line.translation_active
                                              for line in bindings),
                    "flagged_count": sum(
                        _display_review_flag(review_flags[relative + "#" + line.pointer], line.source)
                        is not None
                        for line in bindings
                        if relative + "#" + line.pointer in review_flags),
                    "token": _token(raw),
                })
        return files

    def read_file(self, relative_path: str) -> Dict[str, Any]:
        with self._lock:
            path = self.resolve_file(relative_path)
            raw, _document, schema, bindings = self._load_path(path)
            review_flags = self._load_review_flags()
            return {
                "path": self._relative(path),
                "schema": schema,
                "token": _token(raw),
                "lines": self._normalized(path, schema, bindings, review_flags),
            }

    def update_file(self, relative_path: str, expected_token: str,
                    updates: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        if not isinstance(expected_token, str) or not expected_token:
            raise ProjectError("a file concurrency token is required")
        if not isinstance(updates, list):
            raise ProjectError("updates must be a JSON array")
        with self._lock:
            path = self.resolve_file(relative_path)
            raw, document, schema, bindings = self._load_path(path)
            if _token(raw) != expected_token:
                raise FileConflict("file changed on disk; reload it before saving")
            relative = self._relative(path)
            by_id = {relative + "#" + line.pointer: line for line in bindings}
            review_flags = self._load_review_flags()
            original_review_flags = dict(review_flags)
            seen = set()
            for update in updates:
                if not isinstance(update, Mapping):
                    raise ProjectError("each update must be an object")
                line_id = update.get("id")
                if not isinstance(line_id, str):
                    raise ProjectError("each update id must be a string")
                if line_id in seen:
                    raise ProjectError("duplicate line id: %s" % line_id)
                seen.add(line_id)
                binding = by_id.get(line_id)
                if binding is None:
                    raise ProjectError("unknown line id for %s: %s" % (relative, line_id))
                if "translation" not in update:
                    raise ProjectError("update for %s is missing 'translation'" % line_id)
                value = update.get("translation")
                if value is not None and not isinstance(value, str):
                    raise ProjectError("translation for %s must be a string or null" % line_id)
                if value is not None and not binding.translatable:
                    raise ProjectError("line is protected and cannot be translated: %s" % line_id)
                flagged = update.get("flagged", False)
                if not isinstance(flagged, bool):
                    raise ProjectError("flagged for %s must be a boolean" % line_id)
                if flagged:
                    reason = update.get("flag_reason")
                    expected_tokens = update.get("expected_engine_tokens", [])
                    returned_tokens = update.get("returned_engine_tokens", [])
                    if reason is not None and not isinstance(reason, str):
                        raise ProjectError("flag reason for %s must be a string" % line_id)
                    if (not isinstance(expected_tokens, list) or
                            not all(isinstance(token, str) for token in expected_tokens)):
                        raise ProjectError("expected engine tokens for %s must be strings" % line_id)
                    if (not isinstance(returned_tokens, list) or
                            not all(isinstance(token, str) for token in returned_tokens)):
                        raise ProjectError("returned engine tokens for %s must be strings" % line_id)
                    delimiter_flag = {
                        "category": "engine_delimiters",
                        "reason": reason or "Engine delimiters differ from the source; review this translation.",
                        "source": binding.source,
                        "expected_engine_tokens": expected_tokens,
                        "returned_engine_tokens": returned_tokens,
                    }
                    retained = [flag for flag in _review_flag_items(review_flags[line_id])
                                if flag.get("category") != "engine_delimiters"] \
                               if line_id in review_flags else []
                    review_flags[line_id] = _packed_review_flags(retained + [delimiter_flag])
                else:
                    review_flags.pop(line_id, None)
                if schema == "glossary":
                    key = binding.metadata["key"]
                    document[key] = value
                else:
                    binding.entry[binding.translation_key] = value
            new_raw = _atomic_json(path, document)
            if review_flags != original_review_flags:
                _atomic_json(self.root / PROJECT_REVIEW_FILE, review_flags)
            _raw, _document, new_schema, new_bindings = self._load_path(path)
            return {
                "path": relative,
                "schema": new_schema,
                "token": _token(new_raw),
                "lines": self._normalized(path, new_schema, new_bindings, review_flags),
            }

    def load_project_settings(self) -> Dict[str, Any]:
        path = self.root / PROJECT_SETTINGS_FILE
        if not path.exists():
            return {}
        try:
            resolved = path.resolve(strict=True)
            if not _inside(self.root, resolved) or not resolved.is_file():
                return {}
            value = json.loads(resolved.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(value, Mapping):
            return {}
        return {key: value[key] for key in PROJECT_SETTING_KEYS if key in value}

    def save_project_settings(self, settings: Mapping[str, Any]) -> None:
        document = {key: settings[key] for key in PROJECT_SETTING_KEYS if key in settings}
        with self._lock:
            _atomic_json(self.root / PROJECT_SETTINGS_FILE, document)


class ProjectStore:
    """Thread-safe holder for the single project shown by the browser client."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = (base_dir or Path.cwd()).resolve()
        self._project: Optional[Project] = None
        self._lock = threading.RLock()

    def open(self, path: str, script_dir: Optional[str] = None) -> Project:
        project = Project.open(path, script_dir, self.base_dir)
        # Validate/discover before publishing it to concurrent request handlers.
        if not project.list_files():
            raise ProjectError(
                "no recognized extracted script JSON files found; open a tool/game root "
                "containing script/, open the extracted script directory itself, or pass script_dir")
        with self._lock:
            self._project = project
        return project

    def current(self) -> Project:
        with self._lock:
            if self._project is None:
                raise ProjectNotOpen("open a game folder first")
            return self._project

    def clear(self) -> None:
        with self._lock:
            self._project = None

    def describe(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            project = self._project
        if project is None:
            return None
        files = project.list_files()
        stats = {
            "file_count": len(files),
            "line_count": sum(item["line_count"] for item in files),
            "translatable_count": sum(item["translatable_count"] for item in files),
            "translated_count": sum(item["translated_count"] for item in files),
        }
        return {
            "root": project.root_string,
            "script_dir": project.script_dir_string,
            "files": files,
            "stats": stats,
            **stats,
        }
