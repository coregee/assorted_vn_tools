"""Conventional loopback-only HTTP server for the local translation editor."""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from .lmstudio import DEFAULT_BASE_URL, LMStudioClient, LMStudioError, validate_base_url
from .project import (PROJECT_SETTING_KEYS, FileConflict, InvalidScript, Project,
                      ProjectError, ProjectStore)
from .translator import (DEFAULT_SYSTEM_PROMPT, TranslationCancelled,
                         TranslationEngine, TranslationError, select_lines)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 2 * 1024 * 1024

DEFAULT_SETTINGS: Dict[str, Any] = {
    "base_url": DEFAULT_BASE_URL,
    "model": "",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "game_context": "",
    "target_language": "English",
    "temperature": 0.2,
    "batch_mode": "messages",
    "batch_limit": 8,
    "context_window": 32768,
    "response_reserve_percent": 20,
    "allow_remote_lmstudio": False,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_loopback_host(host: Optional[str]) -> bool:
    if not host:
        return False
    host = host.rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class SettingsStore:
    """Validated settings, with connection settings global and prompt settings per project."""

    _GLOBAL_KEYS = frozenset(("base_url", "allow_remote_lmstudio"))
    _ALL_KEYS = frozenset(DEFAULT_SETTINGS)

    def __init__(self):
        self._settings = dict(DEFAULT_SETTINGS)
        self._lock = threading.RLock()

    @classmethod
    def _validate(cls, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        result = dict(candidate)
        for key in ("model", "system_prompt", "game_context", "target_language"):
            if not isinstance(result.get(key), str):
                raise APIError(400, "%s must be a string" % key)
        if not result["target_language"].strip():
            raise APIError(400, "target_language cannot be empty")
        if len(result["system_prompt"]) > 50000 or len(result["game_context"]) > 100000:
            raise APIError(400, "prompt or game context is too large")
        if not isinstance(result.get("allow_remote_lmstudio"), bool):
            raise APIError(400, "allow_remote_lmstudio must be a boolean")
        temperature = result.get("temperature")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise APIError(400, "temperature must be a number")
        if not 0 <= float(temperature) <= 2:
            raise APIError(400, "temperature must be between 0 and 2")
        result["temperature"] = float(temperature)
        if result.get("batch_mode") not in ("messages", "characters"):
            raise APIError(400, "batch_mode must be 'messages' or 'characters'")
        for key, minimum, maximum in (
                ("batch_limit", 1, 1000000),
                ("context_window", 1024, 1048576),
                ("response_reserve_percent", 5, 50)):
            value = result.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise APIError(400, "%s must be an integer" % key)
            if not minimum <= value <= maximum:
                raise APIError(400, "%s must be between %d and %d" % (key, minimum, maximum))
        try:
            result["base_url"] = validate_base_url(
                result.get("base_url"), result["allow_remote_lmstudio"])
        except ValueError as exc:
            raise APIError(400, str(exc)) from exc
        return result

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._settings)

    def activate_project(self, project: Project) -> Dict[str, Any]:
        saved = project.load_project_settings()
        with self._lock:
            global_values = {key: self._settings[key] for key in self._GLOBAL_KEYS}
            candidate = dict(DEFAULT_SETTINGS)
            candidate.update(global_values)
            candidate.update(saved)
            self._settings = self._validate(candidate)
            return dict(self._settings)

    def merged(self, changes: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(changes, Mapping):
            raise APIError(400, "settings must be an object")
        unknown = set(changes) - self._ALL_KEYS
        if unknown:
            raise APIError(400, "unknown settings: %s" % ", ".join(sorted(unknown)))
        with self._lock:
            candidate = dict(self._settings)
        candidate.update(changes)
        return self._validate(candidate)

    def update(self, changes: Mapping[str, Any], project: Optional[Project]) -> Dict[str, Any]:
        candidate = self.merged(changes)
        if project is not None:
            project.save_project_settings(candidate)
        with self._lock:
            self._settings = candidate
        return dict(candidate)


@dataclass
class TranslationJob:
    id: str
    project_root: str
    total: int
    status: str = "queued"
    completed: int = 0
    batch: int = 0
    batches: int = 0
    suggestions: List[Dict[str, Any]] = field(default_factory=list)
    written_files: List[str] = field(default_factory=list)
    error: Optional[str] = None
    created_at: str = field(default_factory=_utc_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            visible_status = self.status
            if self.cancel_event.is_set() and visible_status in ("queued", "running"):
                visible_status = "cancelling"
            return {
                "id": self.id,
                "status": visible_status,
                "project_root": self.project_root,
                "progress": {
                    "completed": self.completed,
                    "total": self.total,
                    "batch": self.batch,
                    "batches": self.batches,
                },
                "result_count": len(self.suggestions),
                "written_files": list(self.written_files),
                "error": self.error,
                "cancellation_requested": self.cancel_event.is_set(),
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


class JobManager:
    def __init__(self):
        self._jobs: Dict[str, TranslationJob] = {}
        self._lock = threading.RLock()

    def create(self, project: Project, settings: Mapping[str, Any],
               file_paths: Optional[Sequence[str]], line_ids: Optional[Sequence[str]]) -> TranslationJob:
        summaries = project.list_files()
        files = [project.read_file(summary["path"]) for summary in summaries]
        selected = select_lines(files, file_paths, line_ids)
        if not settings.get("model", "").strip():
            raise TranslationError("select a loaded LM Studio model first")
        if not selected:
            raise TranslationError("no matching translatable lines were selected")
        job = TranslationJob(uuid.uuid4().hex, project.root_string, len(selected))
        with self._lock:
            # Keep completed history bounded without disturbing active jobs.
            complete = [item for item in self._jobs.values()
                        if item.status in ("completed", "failed", "cancelled")]
            for old in sorted(complete, key=lambda item: item.created_at)[:-99]:
                self._jobs.pop(old.id, None)
            self._jobs[job.id] = job
        thread = threading.Thread(
            target=self._run,
            args=(job, project, files, dict(settings), file_paths, line_ids),
            name="translation-%s" % job.id[:8], daemon=True)
        thread.start()
        return job

    def _run(self, job: TranslationJob, project: Project,
             files: Sequence[Mapping[str, Any]],
             settings: Mapping[str, Any], file_paths: Optional[Sequence[str]],
             line_ids: Optional[Sequence[str]]) -> None:
        with job.lock:
            if job.cancel_event.is_set():
                job.status = "cancelled"
                job.finished_at = _utc_now()
                return
            job.status = "running"
            job.started_at = _utc_now()

        try:
            client = LMStudioClient(
                settings["base_url"], settings["allow_remote_lmstudio"])
            file_by_path = {file_data["path"]: file_data for file_data in files}

            def persist_turn(path: str,
                             suggestions: Sequence[Mapping[str, Any]]) -> None:
                file_data = file_by_path[path]
                updates = [{
                    "id": suggestion["id"],
                    "translation": suggestion["suggestion"],
                } for suggestion in suggestions]
                updated = project.update_file(path, file_data["token"], updates)
                file_data["token"] = updated["token"]
                file_data["lines"] = updated["lines"]
                with job.lock:
                    if path not in job.written_files:
                        job.written_files.append(path)
                    job.suggestions.extend(suggestions)

            def progress(completed: int, total: int, batch: int,
                         batches: int) -> None:
                with job.lock:
                    job.completed = completed
                    job.total = total
                    job.batch = batch
                    job.batches = batches

            suggestions = TranslationEngine(client).translate(
                files, settings, file_paths, line_ids,
                job.cancel_event.is_set, progress, persist_turn)
            with job.lock:
                if job.cancel_event.is_set():
                    job.status = "cancelled"
                else:
                    job.completed = len(suggestions)
                    job.status = "completed"
                job.finished_at = _utc_now()
        except TranslationCancelled:
            with job.lock:
                job.status = "cancelled"
                job.finished_at = _utc_now()
        except Exception as exc:
            # This is a daemon worker boundary. Keep failures inspectable via the job API.
            with job.lock:
                job.status = "failed"
                job.error = str(exc) or exc.__class__.__name__
                job.finished_at = _utc_now()

    def get(self, job_id: str) -> TranslationJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise APIError(404, "translation job not found")
        return job

    def cancel(self, job_id: str) -> TranslationJob:
        job = self.get(job_id)
        with job.lock:
            if job.status not in ("completed", "failed", "cancelled"):
                job.cancel_event.set()
        return job

    def result(self, job_id: str) -> Dict[str, Any]:
        job = self.get(job_id)
        with job.lock:
            if job.status not in ("completed", "cancelled"):
                raise APIError(409, "translation job is not complete")
            return {
                "id": job.id,
                "status": job.status,
                "suggestions": list(job.suggestions),
            }


class AppState:
    def __init__(self, base_dir: Optional[Path] = None,
                 static_dir: Optional[Path] = None):
        self.projects = ProjectStore(base_dir)
        self.settings = SettingsStore()
        self.jobs = JobManager()
        self.static_dir = (static_dir or Path(__file__).with_name("static")).resolve()


class TranslationHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Any, handler: Any, state: AppState):
        self.state = state
        super().__init__(address, handler)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "LLMTranslationTools/1"

    @property
    def state(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )

    def _send_json(self, status: int, value: Any) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": {"status": status, "message": message}})

    def _check_host(self) -> None:
        host_header = self.headers.get("Host")
        if not host_header:
            return
        try:
            host = urlsplit("//" + host_header).hostname
        except ValueError:
            host = None
        if not _is_loopback_host(host):
            raise APIError(403, "requests must use a loopback Host header")

    def _check_mutation(self) -> None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise APIError(415, "Content-Type must be application/json")
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed = urlsplit(origin)
            except ValueError:
                raise APIError(403, "invalid Origin header")
            if parsed.scheme != "http" or not _is_loopback_host(parsed.hostname):
                raise APIError(403, "cross-origin requests are not allowed")
            server_port = self.server.server_address[1]
            try:
                origin_port = parsed.port or 80
            except ValueError as exc:
                raise APIError(403, "invalid Origin header") from exc
            if origin_port != server_port:
                raise APIError(403, "Origin port does not match this server")
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            raise APIError(403, "cross-site requests are not allowed")

    def _read_json(self) -> Any:
        self._check_mutation()
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise APIError(411, "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise APIError(400, "invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise APIError(413, "request body is too large")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError(400, "request body is not valid UTF-8 JSON") from exc

    def _project_or_none(self) -> Optional[Project]:
        try:
            return self.state.projects.current()
        except ProjectError:
            return None

    def _dispatch(self) -> None:
        self._check_host()
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)

        if self.command == "GET" and path == "/api/project":
            description = self.state.projects.describe()
            self._send_json(200, {
                "project": description,
                "files": description.get("files", []) if description else [],
            })
            return
        if self.command == "POST" and path == "/api/project/open":
            body = self._read_json()
            if not isinstance(body, Mapping):
                raise APIError(400, "request body must be an object")
            project = self.state.projects.open(body.get("path"), body.get("script_dir"))
            settings = self.state.settings.activate_project(project)
            self._send_json(200, {
                "project": self.state.projects.describe(),
                "files": project.list_files(),
                "settings": settings,
            })
            return
        if self.command == "GET" and path == "/api/files":
            self._send_json(200, {"files": self.state.projects.current().list_files()})
            return
        if self.command == "GET" and path == "/api/file":
            values = query.get("path")
            if not values or not values[0]:
                raise APIError(400, "path query parameter is required")
            self._send_json(200, self.state.projects.current().read_file(values[0]))
            return
        if self.command == "PUT" and path == "/api/file":
            body = self._read_json()
            if not isinstance(body, Mapping):
                raise APIError(400, "request body must be an object")
            self._send_json(200, self.state.projects.current().update_file(
                body.get("path"), body.get("token"), body.get("updates")))
            return
        if self.command == "GET" and path == "/api/settings":
            self._send_json(200, self.state.settings.get())
            return
        if self.command == "PUT" and path == "/api/settings":
            body = self._read_json()
            if not isinstance(body, Mapping):
                raise APIError(400, "request body must be an object")
            self._send_json(200, self.state.settings.update(body, self._project_or_none()))
            return
        if self.command == "GET" and path == "/api/models":
            settings = self.state.settings.get()
            client = LMStudioClient(settings["base_url"], settings["allow_remote_lmstudio"],
                                    timeout=15.0)
            self._send_json(200, {"models": client.models()})
            return
        if self.command == "POST" and path == "/api/jobs":
            body = self._read_json()
            if not isinstance(body, Mapping):
                raise APIError(400, "request body must be an object")
            job_keys = {"files", "line_ids", "settings"}
            flat_settings = {key: value for key, value in body.items()
                             if key in DEFAULT_SETTINGS}
            unknown = set(body) - job_keys - set(DEFAULT_SETTINGS)
            if unknown:
                raise APIError(400, "unknown job fields: %s" % ", ".join(sorted(unknown)))
            nested = body.get("settings", {})
            if not isinstance(nested, Mapping):
                raise APIError(400, "job settings must be an object")
            overrides = dict(nested)
            overrides.update(flat_settings)
            settings = self.state.settings.merged(overrides)
            files = body.get("files")
            line_ids = body.get("line_ids")
            if files is not None and not isinstance(files, list):
                raise APIError(400, "files must be an array")
            if line_ids is not None and not isinstance(line_ids, list):
                raise APIError(400, "line_ids must be an array")
            job = self.state.jobs.create(self.state.projects.current(), settings, files, line_ids)
            self._send_json(202, job.snapshot())
            return

        job_prefix = "/api/jobs/"
        if path.startswith(job_prefix):
            remainder = path[len(job_prefix):]
            parts = [part for part in remainder.split("/") if part]
            if len(parts) == 1 and self.command == "GET":
                self._send_json(200, self.state.jobs.get(parts[0]).snapshot())
                return
            if len(parts) == 2 and parts[1] == "cancel" and self.command == "POST":
                body = self._read_json()
                if body not in ({}, None):
                    raise APIError(400, "cancel request body must be an empty object")
                self._send_json(200, self.state.jobs.cancel(parts[0]).snapshot())
                return
            if len(parts) == 2 and parts[1] == "result" and self.command == "GET":
                self._send_json(200, self.state.jobs.result(parts[0]))
                return

        if self.command in ("GET", "HEAD") and not path.startswith("/api/"):
            self._serve_static(path)
            return
        raise APIError(404, "endpoint not found")

    def _serve_static(self, request_path: str) -> None:
        relative = unquote(request_path).lstrip("/") or "index.html"
        try:
            candidate = (self.state.static_dir / relative).resolve(strict=False)
            if candidate.is_dir():
                candidate = candidate / "index.html"
            if not candidate.is_relative_to(self.state.static_dir):
                raise APIError(403, "invalid static path")
        except (OSError, RuntimeError, ValueError) as exc:
            raise APIError(403, "invalid static path") from exc
        if not candidate.is_file():
            raise APIError(404, "static file not found")
        raw = candidate.read_bytes()
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _handle(self) -> None:
        try:
            self._dispatch()
        except APIError as exc:
            self._send_error_json(exc.status, str(exc))
        except ProjectError as exc:
            self._send_error_json(getattr(exc, "status", 400), str(exc))
        except TranslationError as exc:
            self._send_error_json(422, str(exc))
        except LMStudioError as exc:
            self._send_error_json(502, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.log_error("unhandled request error: %r", exc)
            self._send_error_json(500, "internal server error")

    def do_GET(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()


def create_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  state: Optional[AppState] = None) -> TranslationHTTPServer:
    if not _is_loopback_host(host):
        raise ValueError("the editor may only bind to a loopback address")
    return TranslationHTTPServer((host, port), RequestHandler, state or AppState())


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, open_browser: bool = True,
        project_path: Optional[str] = None, script_dir: Optional[str] = None) -> None:
    state = AppState()
    if project_path:
        project = state.projects.open(project_path, script_dir)
        state.settings.activate_project(project)
    server = create_server(host, port, state)
    actual_port = server.server_address[1]
    display_host = "[%s]" % host if ":" in host and not host.startswith("[") else host
    url = "http://%s:%d/" % (display_host, actual_port)
    print("LLM Translation Tools running at %s" % url)
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.25, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
