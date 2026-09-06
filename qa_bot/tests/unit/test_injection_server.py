import http.client
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from qa_bot.audio.injection_server import main, make_server


class InjectionServerTests(unittest.TestCase):
    def setUp(self):
        self.origin = "https://assessment.example"
        self.token = "i" * 32
        self.script = "globalThis.__injectionLoaded = true;"
        self.server = make_server(
            self.script, token=self.token, allowed_origin=self.origin,
        )
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(self, method="GET", path="/injection.js", *, origin=None,
                 token=None, host=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        values = {
            "Host": host or f"127.0.0.1:{self.server.server_port}",
            "Origin": self.origin if origin is None else origin,
        }
        if token is not None:
            values["X-Injection-Token"] = token
        values.update(headers or {})
        connection.request(method, path, headers=values)
        response = connection.getresponse()
        body = response.read()
        result = (response.status, dict(response.getheaders()), body)
        connection.close()
        return result

    def test_serves_immutable_script_only_for_exact_request(self):
        status, headers, body = self._request(token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body, self.script.encode())
        self.assertEqual(headers["Access-Control-Allow-Origin"], self.origin)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(len(headers["X-Injection-SHA256"]), 64)

    def test_wrong_origin_host_path_or_token_fail_closed(self):
        attempts = [
            {"origin": "https://evil.example", "token": self.token},
            {"host": f"localhost:{self.server.server_port}", "token": self.token},
            {"path": "/injection.js?token=" + self.token, "token": self.token},
            {"path": "/other.js", "token": self.token},
            {"token": "x" * 32},
            {"token": None},
        ]
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                status, headers, body = self._request(**attempt)
                self.assertEqual(status, 403)
                self.assertNotIn("Access-Control-Allow-Origin", headers)
                self.assertNotIn(self.token.encode(), body)

    def test_preflight_requires_exact_target_method_and_header(self):
        valid = {
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-injection-token",
            "Access-Control-Request-Private-Network": "true",
        }
        status, headers, _ = self._request("OPTIONS", headers=valid)
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Private-Network"], "true")
        self.assertEqual(headers["Access-Control-Allow-Origin"], self.origin)

        for override in (
            {"Access-Control-Request-Method": "POST"},
            {"Access-Control-Request-Headers": "content-type"},
        ):
            with self.subTest(override=override):
                rejected = {**valid, **override}
                status, headers, _ = self._request("OPTIONS", headers=rejected)
                self.assertEqual(status, 403)
                self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_configuration_rejects_unsafe_values(self):
        cases = (
            {"token": "short", "allowed_origin": self.origin},
            {"token": self.token, "allowed_origin": "http://assessment.example"},
            {"token": self.token, "allowed_origin": "https://assessment.example/path"},
            {"token": self.token, "allowed_origin": self.origin, "path": "/../script.js"},
            {"token": self.token, "allowed_origin": self.origin, "path": "/script.js?x=1"},
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    make_server(self.script, **values)


class InjectionServerCliTests(unittest.TestCase):
    def test_cli_reads_token_from_environment_and_never_prints_it(self):
        secret = "environment-only-token-123456789"

        class FakeServer:
            server_address = ("127.0.0.1", 18770)

            def serve_forever(self):
                return None

            def server_close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "prepared.js"
            script.write_text("globalThis.__ready = true;", encoding="utf-8")
            output = io.StringIO()
            with patch.dict(os.environ, {"QA_INJECTION_TOKEN": secret}), \
                    patch("qa_bot.audio.injection_server.make_server",
                          return_value=FakeServer()) as factory, \
                    redirect_stdout(output):
                result = main([
                    "--script", str(script), "--origin",
                    "https://assessment.example", "--port", "0",
                ])
            self.assertEqual(result, 0)
            self.assertNotIn(secret, output.getvalue())
            self.assertEqual(factory.call_args.kwargs["token"], secret)
            self.assertEqual(factory.call_args.args[0], script.read_bytes())

    def test_cli_failure_is_sanitized(self):
        secret = "environment-only-token-987654321"
        output = io.StringIO()
        with patch.dict(os.environ, {"QA_INJECTION_TOKEN": secret}), \
                redirect_stdout(output):
            result = main([
                "--script", "/missing/" + secret + ".js",
                "--origin", "https://assessment.example",
            ])
        self.assertEqual(result, 2)
        self.assertEqual(
            output.getvalue().strip(),
            '{"status": "error", "reason": "startup_failed"}',
        )
        self.assertNotIn(secret, output.getvalue())
