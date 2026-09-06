import hashlib
import http.client
import tempfile
import threading
import unittest
from pathlib import Path

from qa_bot.audio.capture_bridge import make_server


class CaptureBridgeTests(unittest.TestCase):
    def test_captures_only_authorized_origin_and_token(self):
        with tempfile.TemporaryDirectory() as directory:
            token = "t" * 32
            origin = "https://assessment.example"
            server = make_server(Path(directory), token=token, allowed_origin=origin)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request("POST", "/capture", b"audio-bytes", {
                    "Origin": origin,
                    "X-Capture-Token": token,
                    "X-Capture-Name": "TP-015-q01.mp3",
                })
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 201)
                saved = Path(directory) / "TP-015-q01.mp3"
                self.assertEqual(saved.read_bytes(), b"audio-bytes")
                self.assertEqual(hashlib.sha256(saved.read_bytes()).hexdigest(),
                                 hashlib.sha256(b"audio-bytes").hexdigest())

                connection.request("POST", "/capture", b"x", {
                    "Origin": origin,
                    "X-Capture-Token": "wrong",
                    "X-Capture-Name": "rejected.mp3",
                })
                rejected = connection.getresponse()
                rejected.read()
                self.assertEqual(rejected.status, 403)
                self.assertFalse((Path(directory) / "rejected.mp3").exists())

                connection.request("OPTIONS", "/capture", headers={
                    "Origin": origin,
                    "Access-Control-Request-Private-Network": "true",
                })
                preflight = connection.getresponse()
                preflight.read()
                self.assertEqual(preflight.status, 204)
                self.assertEqual(preflight.getheader("Access-Control-Allow-Private-Network"),
                                 "true")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_requires_strong_token_and_explicit_https_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                make_server(Path(directory), token="short", allowed_origin="https://example.com")
            with self.assertRaises(ValueError):
                make_server(Path(directory), token="t" * 32,
                            allowed_origin="http://example.com")
