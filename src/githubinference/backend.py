from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from typing import Any, Protocol

from .schema import extract_json_object


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


class ChatBackend(Protocol):
    def chat_json(
        self, messages: Sequence[dict[str, str]], *, max_tokens: int = 2048
    ) -> dict[str, Any]: ...


class LlamaCppClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        *,
        model: str = "caretaker",
        timeout_seconds: int = 240,
        temperature: float = 0.1,
        top_p: float = 0.95,
        top_k: int = 50,
        repeat_penalty: float = 1.05,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("caretaker backend must be a loopback HTTP endpoint")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.repeat_penalty = repeat_penalty
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def health(self) -> bool:
        try:
            # The scheme and loopback host were validated in __init__.
            with self._opener.open(  # noqa: S310
                f"{self.base_url}/health", timeout=min(10, self.timeout_seconds)
            ) as response:
                return 200 <= response.status < 300
        except (OSError, http.client.HTTPException):
            return False

    def wait_until_ready(
        self,
        *,
        timeout_seconds: int = 180,
        interval_seconds: float = 2.0,
        process_id: int | None = None,
    ) -> None:
        if process_id is not None and process_id < 1:
            raise ValueError("process_id must be positive")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.health():
                return
            if process_id is not None and not _process_exists(process_id):
                raise RuntimeError(
                    f"llama.cpp process {process_id} exited before becoming ready"
                )
            time.sleep(interval_seconds)
        raise TimeoutError(
            f"llama.cpp did not become ready within {timeout_seconds} seconds"
        )

    def chat_json(
        self,
        messages: Sequence[dict[str, str]],
        *,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": list(messages),
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "repeat_penalty": self.repeat_penalty,
            "max_tokens": max_tokens,
            "stream": False,
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_object"},
        }
        # The scheme and loopback host were validated in __init__; path is fixed.
        request = urllib.request.Request(  # noqa: S310
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "githubinference/0.1",
            },
            method="POST",
        )
        try:
            with self._opener.open(  # noqa: S310
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise RuntimeError(f"llama.cpp returned HTTP {exc.code}: {detail}") from exc
        if len(raw) > 4 * 1024 * 1024:
            raise RuntimeError("llama.cpp response exceeded 4 MiB")
        envelope = json.loads(raw)
        try:
            content = envelope["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                "llama.cpp response did not contain assistant content"
            ) from exc
        if isinstance(content, dict):
            return content
        if not isinstance(content, str):
            raise RuntimeError("llama.cpp assistant content was not text or JSON")
        return extract_json_object(content)


class MockBackend:
    def __init__(self, responses: Sequence[dict[str, Any]]) -> None:
        if not responses:
            raise ValueError("MockBackend requires at least one response")
        self._responses = list(responses)
        self._lock = threading.Lock()
        self.calls: list[list[dict[str, str]]] = []

    def chat_json(
        self,
        messages: Sequence[dict[str, str]],
        *,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        del max_tokens
        with self._lock:
            self.calls.append(list(messages))
            index = min(len(self.calls) - 1, len(self._responses) - 1)
            return json.loads(json.dumps(self._responses[index]))


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
