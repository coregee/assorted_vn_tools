"""Tiny standard-library client for OpenAI-compatible chat APIs."""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence


DEFAULT_BASE_URL = "http://localhost:8000/api/v1"

TRANSLATIONS_RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "vn_line_translations",
        "strict": True,
        "schema": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


class OpenAIError(Exception):
    def __init__(self, message: str, status: Optional[int] = None,
                 body: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.body = body


def validate_base_url(value: str, allow_remote: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("LLM server base_url is required")
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("LLM server base_url must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("LLM server base_url cannot contain credentials, a query, or a fragment")
    if not allow_remote:
        host = parsed.hostname.rstrip(".").lower()
        loopback = host == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise ValueError("LLM server base_url must use localhost or a loopback IP")
    # Users can supply either the normal /v1 root or a host root.
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class OpenAIClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, allow_remote: bool = False,
                 timeout: float = 180.0,
                 opener: Optional[Callable[..., Any]] = None,
                 api_key: str = ""):
        self.base_url = validate_base_url(base_url, allow_remote)
        self.timeout = timeout
        self._opener = opener
        self._reasoning_supported = True
        self.api_key = api_key.strip()

    def _request(self, method: str, endpoint: str,
                 payload: Optional[Mapping[str, Any]] = None) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + endpoint, data=data,
                                         headers=headers, method=method)
        opener = self._opener or urllib.request.urlopen
        try:
            with opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            finally:
                exc.close()
            detail = body[:1000] if body else str(exc.reason)
            raise OpenAIError("LLM server returned HTTP %s: %s" % (exc.code, detail),
                                exc.code, body) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise OpenAIError("could not reach LLM server at %s: %s" %
                                (self.base_url, reason)) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenAIError("LLM server returned invalid JSON") from exc

    def models(self) -> List[Dict[str, Any]]:
        response = self._request("GET", "/models")
        if not isinstance(response, Mapping) or not isinstance(response.get("data"), list):
            raise OpenAIError("LLM server /models response has an unexpected shape")
        models = []
        for item in response["data"]:
            if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                models.append({"id": item["id"], "owned_by": item.get("owned_by")})
        return models

    def chat_completion(self, messages: Sequence[Mapping[str, str]], model: str,
                        temperature: float = 0.2, max_tokens: int = 4096,
                        response_format: Optional[Mapping[str, Any]] = None,
                        reasoning_effort: Optional[str] = None) -> str:
        if not isinstance(model, str) or not model.strip():
            raise OpenAIError("select a loaded LLM server model first")
        payload: Dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if reasoning_effort is not None and self._reasoning_supported:
            payload["reasoning_effort"] = reasoning_effort
        try:
            response = self._request("POST", "/chat/completions", payload)
        except OpenAIError as exc:
            detail = (str(exc) + "\n" + (exc.body or "")).lower()
            if ("reasoning_effort" not in payload or
                    exc.status not in (400, 422) or "reasoning_effort" not in detail):
                raise
            # Reasoning controls are optional across compatible servers/models.
            del payload["reasoning_effort"]
            self._reasoning_supported = False
            response = self._request("POST", "/chat/completions", payload)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenAIError("LLM server chat response has an unexpected shape") from exc
        if not isinstance(content, str):
            raise OpenAIError("LLM server chat response content is not text")
        return content
