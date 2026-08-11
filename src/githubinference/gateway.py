from __future__ import annotations

import hmac
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_ALLOWED_POST_PATHS = {"/v1/chat/completions", "/v1/completions"}
_ALLOWED_GET_PATHS = {"/v1/models"}


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


class InferenceGateway:
    def __init__(
        self,
        *,
        api_key: str,
        upstream: str = "http://127.0.0.1:8080",
        request_timeout_seconds: int = 300,
    ) -> None:
        if len(api_key) < 32:
            raise ValueError("INFERENCE_API_KEY must contain at least 32 characters")
        parsed = urllib.parse.urlparse(upstream)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("gateway upstream must be a loopback HTTP endpoint")
        self.api_key = api_key
        self.upstream = upstream.rstrip("/")
        self.request_timeout_seconds = request_timeout_seconds
        self.inflight = threading.BoundedSemaphore(value=1)
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def authorized(self, header: str | None) -> bool:
        prefix = "Bearer "
        if not header or not header.startswith(prefix):
            return False
        try:
            supplied = header[len(prefix) :].encode("utf-8")
            expected = self.api_key.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(supplied, expected)

    def proxy(
        self, method: str, path: str, payload: bytes | None
    ) -> tuple[int, str, bytes]:
        if not self.inflight.acquire(blocking=False):
            return _json_response(HTTPStatus.TOO_MANY_REQUESTS, "the CPU model is busy")
        try:
            # self.upstream is validated as loopback HTTP during initialization.
            request = urllib.request.Request(  # noqa: S310
                f"{self.upstream}{path}",
                data=payload,
                method=method,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "githubinference-gateway/0.1",
                },
            )
            try:
                with self._opener.open(
                    request, timeout=self.request_timeout_seconds
                ) as response:
                    body = response.read(_MAX_RESPONSE_BYTES + 1)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        return _json_response(
                            HTTPStatus.BAD_GATEWAY, "upstream response was too large"
                        )
                    content_type = response.headers.get(
                        "Content-Type", "application/json"
                    )
                    return response.status, content_type, body
            except urllib.error.HTTPError as exc:
                body = exc.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    body = (
                        b'{"error":{"message":"upstream error response was too large"}}'
                    )
                return exc.code, "application/json", body
            except (OSError, urllib.error.URLError):
                return _json_response(
                    HTTPStatus.BAD_GATEWAY, "model backend is unavailable"
                )
        finally:
            self.inflight.release()


def serve_gateway(
    *,
    api_key: str,
    host: str = "127.0.0.1",
    port: int = 8787,
    upstream: str = "http://127.0.0.1:8080",
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the authentication gateway must bind to loopback")
    if not 1 <= port <= 65535:
        raise ValueError("gateway port is invalid")
    gateway = InferenceGateway(api_key=api_key, upstream=upstream)

    # BaseHTTPRequestHandler's default logger records the request line and status,
    # but never request headers or bodies that could contain keys or prompts.
    class Handler(BaseHTTPRequestHandler):
        server_version = "GithubinferenceGateway/0.1"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            if path == "/healthz":
                self._send(
                    *_json_response(HTTPStatus.OK, "gateway ready", key="status")
                )
                return
            if path not in _ALLOWED_GET_PATHS:
                self._send(*_json_response(HTTPStatus.NOT_FOUND, "not found"))
                return
            if not gateway.authorized(self.headers.get("Authorization")):
                self._unauthorized()
                return
            self._send(*gateway.proxy("GET", path, None))

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            if path not in _ALLOWED_POST_PATHS:
                self._send(*_json_response(HTTPStatus.NOT_FOUND, "not found"))
                return
            if not gateway.authorized(self.headers.get("Authorization")):
                self._unauthorized()
                return
            if self.headers.get("Transfer-Encoding"):
                self._send(
                    *_json_response(
                        HTTPStatus.BAD_REQUEST, "chunked requests are not accepted"
                    )
                )
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length < 2 or length > _MAX_REQUEST_BYTES:
                self._send(
                    *_json_response(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request size is invalid"
                    )
                )
                return
            payload = self.rfile.read(length)
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError:
                self._send(
                    *_json_response(
                        HTTPStatus.BAD_REQUEST, "request body is not valid JSON"
                    )
                )
                return
            if not isinstance(decoded, dict):
                self._send(
                    *_json_response(
                        HTTPStatus.BAD_REQUEST, "request body must be an object"
                    )
                )
                return
            if decoded.get("stream") is True:
                self._send(
                    *_json_response(HTTPStatus.BAD_REQUEST, "streaming is disabled")
                )
                return
            max_tokens = decoded.get("max_tokens", 1024)
            if (
                isinstance(max_tokens, bool)
                or not isinstance(max_tokens, int)
                or not 1 <= max_tokens <= 4096
            ):
                self._send(
                    *_json_response(
                        HTTPStatus.BAD_REQUEST, "max_tokens must be between 1 and 4096"
                    )
                )
                return
            self._send(*gateway.proxy("POST", path, payload))

        def _unauthorized(self) -> None:
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Type", "application/json")
            self.send_header("WWW-Authenticate", "Bearer")
            body = b'{"error":{"message":"authentication required"}}'
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type.split(";", 1)[0])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=0.5)


def _json_response(
    status: int, message: str, *, key: str = "message"
) -> tuple[int, str, bytes]:
    if status >= 400:
        envelope: dict[str, Any] = {"error": {"message": message}}
    else:
        envelope = {key: message}
    body = json.dumps(envelope).encode("utf-8")
    return int(status), "application/json", body
