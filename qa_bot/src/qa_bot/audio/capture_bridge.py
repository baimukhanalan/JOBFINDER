"""Loopback-only bridge for moving captured browser audio into local storage."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def make_server(output_dir: Path, *, token: str, allowed_origin: str,
                port: int = 0, max_bytes: int = 20_000_000) -> ThreadingHTTPServer:
    if len(token) < 24:
        raise ValueError("capture token must contain at least 24 characters")
    if not allowed_origin.startswith("https://"):
        raise ValueError("an explicit HTTPS origin is required")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type,X-Capture-Token,X-Capture-Name")
            self.send_header("Access-Control-Allow-Methods", "POST,OPTIONS")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")

        def do_OPTIONS(self):
            if self.headers.get("Origin") != allowed_origin:
                self.send_response(403)
            else:
                self.send_response(204)
                self._cors()
            self.end_headers()

        def do_POST(self):
            if self.path != "/capture" or self.headers.get("Origin") != allowed_origin:
                return self._error(403, "origin_or_path_rejected")
            if self.headers.get("X-Capture-Token") != token:
                return self._error(403, "token_rejected")
            name = self.headers.get("X-Capture-Name", "")
            if not _SAFE_NAME.fullmatch(name):
                return self._error(400, "invalid_name")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._error(411, "content_length_required")
            if not 1 <= length <= max_bytes:
                return self._error(413, "invalid_size")
            body = self.rfile.read(length)
            if len(body) != length:
                return self._error(400, "truncated_body")
            target = output_dir / name
            temporary = output_dir / ("." + name + ".partial")
            temporary.write_bytes(body)
            os.replace(temporary, target)
            report = {"status": "ok", "name": name, "bytes": len(body),
                      "sha256": hashlib.sha256(body).hexdigest()}
            payload = json.dumps(report, separators=(",", ":")).encode()
            self.send_response(201)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: int, reason: str):
            payload = json.dumps({"status": "error", "reason": reason}).encode()
            self.send_response(status)
            if self.headers.get("Origin") == allowed_origin:
                self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Receive authorized audio on localhost")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args(argv)
    token = os.environ.get("QA_CAPTURE_TOKEN", "")
    try:
        server = make_server(args.output, token=token, allowed_origin=args.origin, port=args.port)
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error)[:180]}))
        return 2
    print(json.dumps({"status": "ready", "host": "127.0.0.1",
                      "port": server.server_address[1]}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
