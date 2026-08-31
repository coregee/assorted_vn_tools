"""Tiny standard-library client for LM Studio's OpenAI-compatible local API."""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence


DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"

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


class LMStudioError(Exception):
    def __init__(self, message: str, status: Optional[int] = None,
                 body: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class LMStudioCompletion:
    content: str
    response_id: Optional[str] = None


def validate_base_url(value: str, allow_remote: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("LM Studio base_url is required")
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("LM Studio base_url must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("LM Studio base_url cannot contain credentials, a query, or a fragment")
    if not allow_remote:
        host = parsed.hostname.rstrip(".").lower()
        loopback = host == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise ValueError("LM Studio base_url must use localhost or a loopback IP")
    # Users can supply either the normal /v1 root or a host root.
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class LMStudioClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, allow_remote: bool = False,
                 timeout: float = 180.0,
                 opener: Optional[Callable[..., Any]] = None):
        self.base_url = validate_base_url(base_url, allow_remote)
        self.timeout = timeout
        self._opener = opener

    def _request(self, method: str, endpoint: str,
                 payload: Optional[Mapping[str, Any]] = None) -> Any:
        data = None
        headers = {"Accept": "application/json"}
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
            detail = body[:1000] if body else str(exc.reason)
            raise LMStudioError("LM Studio returned HTTP %s: %s" % (exc.code, detail),
                                exc.code, body) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise LMStudioError("could not reach LM Studio at %s: %s" %
                                (self.base_url, reason)) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LMStudioError("LM Studio returned invalid JSON") from exc

    def models(self) -> List[Dict[str, Any]]:
        response = self._request("GET", "/models")
        if not isinstance(response, Mapping) or not isinstance(response.get("data"), list):
            raise LMStudioError("LM Studio /models response has an unexpected shape")
        models = []
        for item in response["data"]:
            if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                models.append({"id": item["id"], "owned_by": item.get("owned_by")})
        return models

    def response_completion(self, messages: Sequence[Mapping[str, str]], model: str,
                            temperature: float = 0.2, max_tokens: int = 4096,
                            response_format: Optional[Mapping[str, Any]] = None,
                            reasoning_effort: Optional[str] = None,
                            previous_response_id: Optional[str] = None,
                            ) -> LMStudioCompletion:
        """Create or continue a stored LM Studio Responses API conversation."""
        if not isinstance(model, str) or not model.strip():
            raise LMStudioError("select a loaded LM Studio model first")
        payload: Dict[str, Any] = {
            "model": model,
            "input": [dict(message) for message in messages],
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "stream": False,
            "store": True,
        }
        if previous_response_id is not None:
            payload["previous_response_id"] = previous_response_id
        if response_format is not None:
            json_schema = response_format.get("json_schema")
            if response_format.get("type") == "json_schema" and isinstance(json_schema, Mapping):
                payload["text"] = {"format": dict(json_schema, type="json_schema")}
            else:
                payload["text"] = {"format": dict(response_format)}
        if reasoning_effort is not None:
            payload["reasoning"] = {"effort": reasoning_effort}

        response = self._request("POST", "/responses", payload)
        if not isinstance(response, Mapping):
            raise LMStudioError("LM Studio response has an unexpected shape")

        text_parts: List[str] = []
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, Mapping) or item.get("type") != "message":
                    continue
                content = item.get("content")
                if isinstance(content, str):
                    text_parts.append(content)
                    continue
                if not isinstance(content, list):
                    continue
                for part in content:
                    if (isinstance(part, Mapping) and
                            part.get("type") in ("output_text", "text") and
                            isinstance(part.get("text"), str)):
                        text_parts.append(part["text"])
        if not text_parts and isinstance(response.get("output_text"), str):
            text_parts.append(response["output_text"])
        if not text_parts:
            raise LMStudioError("LM Studio response content is not text")

        response_id = response.get("id")
        if response_id is None:
            response_id = response.get("response_id")
        if response_id is not None and not isinstance(response_id, str):
            raise LMStudioError("LM Studio response ID is not text")
        return LMStudioCompletion("".join(text_parts), response_id)

    def chat_completion(self, messages: Sequence[Mapping[str, str]], model: str,
                        temperature: float = 0.2, max_tokens: int = 4096,
                        response_format: Optional[Mapping[str, Any]] = None,
                        reasoning_effort: Optional[str] = None) -> str:
        if not isinstance(model, str) or not model.strip():
            raise LMStudioError("select a loaded LM Studio model first")
        payload: Dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        response = self._request("POST", "/chat/completions", payload)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LMStudioError("LM Studio chat response has an unexpected shape") from exc
        if not isinstance(content, str):
            raise LMStudioError("LM Studio chat response content is not text")
        return content
