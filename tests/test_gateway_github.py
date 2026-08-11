from __future__ import annotations

import io
import json
import os
import threading
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from githubinference.cli import main as cli_main
from githubinference.gateway import InferenceGateway
from githubinference.github_api import (
    GitHubClient,
    _read_subagent_archive,
    _retry_after_seconds,
)


class _UpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length))
        body = json.dumps(
            {"received": request, "authorization": self.headers.get("Authorization")}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message_format: str, *args: object) -> None:
        del message_format, args


class _EndpointHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("Authorization") != "Bearer " + "k" * 32:
            self.send_response(401)
            self.end_headers()
            return
        body = b'{"object":"list","data":[{"id":"test-model"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, message_format: str, *args: object) -> None:
        del message_format, args


class GatewayGitHubTests(unittest.TestCase):
    def test_gateway_requires_long_key_and_constant_bearer_match(self) -> None:
        with self.assertRaisesRegex(ValueError, "32"):
            InferenceGateway(api_key="short")
        gateway = InferenceGateway(api_key="a" * 32)
        self.assertFalse(gateway.authorized(None))
        self.assertFalse(gateway.authorized("Bearer " + "b" * 32))
        self.assertTrue(gateway.authorized("Bearer " + "a" * 32))
        self.assertFalse(gateway.authorized("Bearer " + "é" * 32))

    def test_gateway_does_not_forward_bearer_key(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            gateway = InferenceGateway(
                api_key="a" * 32,
                upstream=f"http://127.0.0.1:{server.server_port}",
            )
            payload = b'{"stream":false,"max_tokens":10}'
            status, _, body = gateway.proxy("POST", "/v1/chat/completions", payload)
            self.assertEqual(status, 200)
            response = json.loads(body)
            self.assertIsNone(response["authorization"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_gateway_rejects_parallel_inference(self) -> None:
        gateway = InferenceGateway(api_key="a" * 32)
        self.assertTrue(gateway.inflight.acquire(blocking=False))
        try:
            status, _, _ = gateway.proxy("POST", "/v1/chat/completions", b"{}")
            self.assertEqual(status, 429)
        finally:
            gateway.inflight.release()

    def test_endpoint_smoke_uses_environment_key_without_printing_it(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _EndpointHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"INFERENCE_API_KEY": "k" * 32}, clear=False):
                status = cli_main(
                    [
                        "endpoint-smoke",
                        "--url",
                        f"http://127.0.0.1:{server.server_port}",
                    ]
                )
            self.assertEqual(status, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_generated_report_paths_cannot_escape_caretaker_directory(self) -> None:
        client = GitHubClient("owner/repo", "test-token")
        with self.assertRaisesRegex(ValueError, "unsafe generated"):
            client.create_report_pull_request(
                run_id="x",
                title="x",
                body="x",
                files={"../workflow.yml": "bad"},
            )

    def test_subagent_archive_accepts_one_bounded_root_json(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("result.json", '{"summary":"ok"}')
        self.assertEqual(_read_subagent_archive(buffer.getvalue()), {"summary": "ok"})

    def test_marker_scans_continue_beyond_first_page(self) -> None:
        client = GitHubClient("owner/repo", "test-token")
        calls: list[str] = []

        def request(method: str, path: str, *args: object, **kwargs: object) -> object:
            del args, kwargs
            calls.append(f"{method} {path}")
            if method != "GET":
                self.fail(
                    "marker scan should not write when page two contains the marker"
                )
            if path.endswith("page=1"):
                return [{"body": "ordinary"} for _ in range(100)]
            if path.endswith("page=2"):
                return [{"body": "hidden marker"}]
            self.fail(f"unexpected request: {path}")

        with patch.object(GitHubClient, "_request", side_effect=request):
            comment_result = client.comment_once(7, "body", "hidden marker")
            issue_result = client.create_issue("title", "body", "hidden marker")
        self.assertTrue(comment_result["skipped"])
        self.assertTrue(issue_result["skipped"])
        self.assertTrue(any("page=2" in call for call in calls))

    def test_retry_after_accepts_seconds_http_date_and_invalid_values(self) -> None:
        self.assertEqual(_retry_after_seconds("7"), 7)
        future = format_datetime(
            datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True
        )
        self.assertGreaterEqual(_retry_after_seconds(future), 0)
        self.assertLessEqual(_retry_after_seconds(future), 30)
        self.assertEqual(_retry_after_seconds("not-a-date"), 0)


if __name__ == "__main__":
    unittest.main()
