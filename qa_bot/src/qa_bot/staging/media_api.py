import base64
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from qa_bot.audio.service import wav_duration


def make_server(port=0):
    responses, lock = {}, Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, data):
            payload = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            if self.path != "/v1/responses":
                return self.reply(404, {"error": "not_found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8_000_000:
                    raise ValueError("body size")
                data = json.loads(self.rfile.read(length))
                if set(data) != {"question_id", "audio_hash", "idempotency_key", "wav_base64", "synthetic"} or data["synthetic"] is not True:
                    raise ValueError("schema")
                key, qid = data["idempotency_key"], data["question_id"]
                if not isinstance(key, str) or not 1 <= len(key) <= 100 or not isinstance(qid, str) or not 1 <= len(qid) <= 200:
                    raise ValueError("identity")
                audio = base64.b64decode(data["wav_base64"], validate=True)
                duration = wav_duration(audio)
                digest = hashlib.sha256(audio).hexdigest()
                if digest != data["audio_hash"]:
                    raise ValueError("hash")
                with lock:
                    existing = responses.get(key)
                    receipt = {"question_id": qid, "audio_hash": digest,
                               "duration": duration, "status": "accepted_mock"}
                    if existing and existing != receipt:
                        return self.reply(409, {"error": "idempotency_conflict"})
                    responses[key] = receipt
                return self.reply(200, receipt)
            except Exception:
                return self.reply(400, {"error": "invalid_synthetic_audio"})

        def do_GET(self):
            if not self.path.startswith("/v1/responses/"):
                return self.reply(404, {"error": "not_found"})
            with lock:
                result = responses.get(self.path.removeprefix("/v1/responses/"))
            self.reply(200 if result else 404, result or {"error": "not_found"})
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    server = make_server(8766)
    try:
        server.serve_forever()
    finally:
        server.server_close()
