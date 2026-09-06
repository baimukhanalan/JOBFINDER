"""Serve one immutable browser injection script over an authenticated loopback endpoint."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{1,160}$")


def _validate_origin(origin: str) -> str:
    if not isinstance(origin, str):
        raise ValueError("allowed_origin must be an explicit HTTPS origin")
    parsed = urlsplit(origin)
    try:
        parsed_port = parsed.port
    except ValueError as error:
        raise ValueError("allowed_origin must be an explicit HTTPS origin") from error
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ("", "/") or parsed.query
            or parsed.fragment):
        raise ValueError("allowed_origin must be an explicit HTTPS origin")
    authority = parsed.hostname
    if ":" in authority:
        authority = f"[{authority}]"
    if parsed_port is not None:
        authority += f":{parsed_port}"
    canonical = f"https://{authority}"
    if origin.rstrip("/") != canonical:
        raise ValueError("allowed_origin must be a canonical HTTPS origin")
    return canonical


def make_server(script: str | bytes, *, token: str, allowed_origin: str,
                path: str = "/injection.js", port: int = 0,
                max_script_bytes: int = 1_000_000) -> ThreadingHTTPServer:
    """Build a localhost-only server for one prebuilt JavaScript payload.

    The token is accepted only in ``X-Injection-Token``. It is never placed in a
    URL, response body, exception, or request log.
    """
    if not isinstance(token, str) or len(token) < 24:
        raise ValueError("injection token must contain at least 24 characters")
    origin = _validate_origin(allowed_origin)
    if (not isinstance(path, str) or not _SAFE_PATH.fullmatch(path)
            or "//" in path or "/../" in path or path.endswith("/..")):
        raise ValueError("injection path must be an exact safe absolute path")
    if isinstance(script, str):
        payload = script.encode("utf-8")
    elif isinstance(script, bytes):
        payload = bytes(script)
    else:
        raise ValueError("script must be text or bytes")
    if not payload or len(payload) > max_script_bytes:
        raise ValueError("script size is invalid")
    digest = hashlib.sha256(payload).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            # The default handler log includes the raw request target. Keeping
            # this endpoint silent prevents future URL/header changes from
            # accidentally exposing credentials.
            return

        def _expected_host(self) -> str:
            return f"127.0.0.1:{self.server.server_port}"

        def _target_allowed(self) -> bool:
            return (self.path == path
                    and self.headers.get("Host") == self._expected_host()
                    and self.headers.get("Origin") == origin)

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "X-Injection-Token")
            self.send_header("Access-Control-Allow-Methods", "GET,OPTIONS")
            self.send_header("Access-Control-Expose-Headers", "X-Injection-SHA256")

        def _json_error(self, status: int, reason: str) -> None:
            body = json.dumps(
                {"status": "error", "reason": reason}, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:
            requested_method = self.headers.get("Access-Control-Request-Method")
            requested_headers = {
                item.strip().lower()
                for item in self.headers.get(
                    "Access-Control-Request-Headers", ""
                ).split(",") if item.strip()
            }
            if (not self._target_allowed() or requested_method != "GET"
                    or requested_headers != {"x-injection-token"}):
                return self._json_error(403, "preflight_rejected")
            self.send_response(204)
            self._cors()
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def do_GET(self) -> None:
            if not self._target_allowed():
                return self._json_error(403, "request_rejected")
            supplied = self.headers.get("X-Injection-Token", "")
            if not hmac.compare_digest(supplied, token):
                return self._json_error(403, "request_rejected")
            self.send_response(200)
            self._cors()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Injection-SHA256", digest)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_HEAD(self) -> None:
            self._json_error(405, "method_rejected")

        def do_POST(self) -> None:
            self._json_error(405, "method_rejected")

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve a prepared browser injection from localhost"
    )
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--path", default="/injection.js")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args(argv)
    server = None
    try:
        payload = args.script.read_bytes()
        server = make_server(
            payload, token=os.environ.get("QA_INJECTION_TOKEN", ""),
            allowed_origin=args.origin, path=args.path, port=args.port,
        )
    except Exception:
        # Keep startup output deliberately generic: arguments and environment
        # values may contain deployment credentials.
        print(json.dumps({"status": "error", "reason": "startup_failed"}))
        return 2
    print(json.dumps({
        "status": "ready", "host": "127.0.0.1",
        "port": server.server_address[1],
    }), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
