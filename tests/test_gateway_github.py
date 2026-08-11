from __future__ import annotations

import io
import json
import os
import socket
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from githubinference.backend import LlamaCppClient
from githubinference.cli import _analysis_request_timeout
from githubinference.cli import main as cli_main
from githubinference.config import CaretakerConfig
from githubinference.gateway import InferenceGateway, serve_gateway
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
    def test_analysis_request_timeout_tracks_the_bounded_work_window(self) -> None:
        config = CaretakerConfig.load()
        self.assertEqual(_analysis_request_timeout(config, 1), 45)
        self.assertEqual(_analysis_request_timeout(config, 15), 285)
        self.assertEqual(_analysis_request_timeout(config, 20), 585)
        self.assertEqual(_analysis_request_timeout(config, 45), 900)
        with self.assertRaisesRegex(ValueError, "positive"):
            _analysis_request_timeout(config, 0)

    def test_model_backend_loopback_opener_ignores_environment_proxies(self) -> None:
        with (
            patch("githubinference.backend.urllib.request.ProxyHandler") as proxy,
            patch("githubinference.backend.urllib.request.build_opener") as build,
        ):
            backend = LlamaCppClient()
        proxy.assert_called_once_with({})
        self.assertEqual(len(build.call_args.args), 2)
        self.assertIs(build.call_args.args[0], proxy.return_value)
        self.assertIs(backend._opener, build.return_value)

    def test_gateway_requires_long_key_and_constant_bearer_match(self) -> None:
        with self.assertRaisesRegex(ValueError, "32"):
            InferenceGateway(api_key="short")
        with self.assertRaisesRegex(ValueError, "timeout"):
            LlamaCppClient(timeout_seconds=0)
        gateway = InferenceGateway(api_key="a" * 32)
        self.assertFalse(gateway.authorized(None))
        self.assertFalse(gateway.authorized("Bearer " + "b" * 32))
        self.assertTrue(gateway.authorized("Bearer " + "a" * 32))
        self.assertFalse(gateway.authorized("Bearer " + "é" * 32))

    def test_gateway_loopback_opener_ignores_environment_proxies(self) -> None:
        with (
            patch("githubinference.gateway.urllib.request.ProxyHandler") as proxy,
            patch("githubinference.gateway.urllib.request.build_opener") as build,
        ):
            gateway = InferenceGateway(api_key="a" * 32)
        proxy.assert_called_once_with({})
        self.assertEqual(len(build.call_args.args), 2)
        self.assertIs(build.call_args.args[0], proxy.return_value)
        self.assertIs(gateway._opener, build.return_value)

    def test_gateway_returns_bad_request_for_invalid_utf8_json(self) -> None:
        servers: list[ThreadingHTTPServer] = []
        ready = threading.Event()

        def server_factory(
            address: tuple[str, int], handler: type[BaseHTTPRequestHandler]
        ) -> ThreadingHTTPServer:
            server = ThreadingHTTPServer(address, handler)
            servers.append(server)
            ready.set()
            return server

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        with patch(
            "githubinference.gateway.ThreadingHTTPServer",
            side_effect=server_factory,
        ):
            thread = threading.Thread(
                target=serve_gateway,
                kwargs={"api_key": "a" * 32, "port": port},
                daemon=True,
            )
            thread.start()
            self.assertTrue(ready.wait(timeout=2))
            server = servers[0]
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                data=b'{"prompt":"\xff"}',
                method="POST",
                headers={
                    "Authorization": "Bearer " + "a" * 32,
                    "Content-Type": "application/json",
                },
            )
            try:
                with self.assertRaises(urllib.error.HTTPError) as captured:
                    urllib.request.urlopen(request, timeout=2)  # noqa: S310
                try:
                    self.assertEqual(captured.exception.code, 400)
                    self.assertIn(b"not valid JSON", captured.exception.read())
                finally:
                    captured.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

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
