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
PROJECT_SETTING_KEYS = frozenset(
    ("system_prompt", "game_context", "target_language", "model", "enable_thinking", "temperature",
     "context_window", "response_reserve_percent", "batch_mode", "batch_limit")
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
    if schema_hint == "sstar" and isinstance(entry.get("jp_lines"), list):
        candidate_segments = entry["jp_lines"]
        if candidate_segments and all(isinstance(segment, str) for segment in candidate_segments):
            source_segments = list(candidate_segments)
            source = "\n".join(source_segments)
    translatable = bool(source.strip()) and not protected
    empty_is_applied = schema_hint in ("dasaku", "dasaku-ui", "generic")

    omitted = {source_key, translation_key, "speaker", "name", "speaker_tr",
               "name_translated", "kind", "context"}
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

    def _normalized(self, path: Path, schema: str, bindings: Sequence[_Binding]) -> List[Dict[str, Any]]:
        relative = self._relative(path)
        lines: List[Dict[str, Any]] = []
        for index, binding in enumerate(bindings):
            lines.append({
                "id": relative + "#" + binding.pointer,
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
            })
        return lines

    def list_files(self) -> List[Dict[str, Any]]:
        files: List[Dict[str, Any]] = []
        with self._lock:
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
                files.append({
                    "path": self._relative(resolved),
                    "schema": schema,
                    "line_count": len(bindings),
                    "translatable_count": sum(line.translatable for line in bindings),
                    "translated_count": sum(line.translatable and line.translation_active
                                              for line in bindings),
                    "token": _token(raw),
                })
        return files

    def read_file(self, relative_path: str) -> Dict[str, Any]:
        with self._lock:
            path = self.resolve_file(relative_path)
            raw, _document, schema, bindings = self._load_path(path)
            return {
                "path": self._relative(path),
                "schema": schema,
                "token": _token(raw),
                "lines": self._normalized(path, schema, bindings),
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
                if schema == "glossary":
                    key = binding.metadata["key"]
                    document[key] = value
                else:
                    binding.entry[binding.translation_key] = value
            new_raw = _atomic_json(path, document)
            _raw, _document, new_schema, new_bindings = self._load_path(path)
            return {
                "path": relative,
                "schema": new_schema,
                "token": _token(new_raw),
                "lines": self._normalized(path, new_schema, new_bindings),
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
