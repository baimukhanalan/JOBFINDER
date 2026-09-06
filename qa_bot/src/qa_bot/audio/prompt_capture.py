"""Local manifest bank for original listen-and-repeat prompt audio."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import threading
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_TYPES = {
    ".mp3": {"audio/mpeg", "audio/mp3", "application/octet-stream"},
    ".wav": {"audio/wav", "audio/x-wav", "application/octet-stream"},
    ".ogg": {"audio/ogg", "application/ogg", "application/octet-stream"},
    ".m4a": {"audio/mp4", "audio/x-m4a", "application/octet-stream"},
}


class PromptAudioBank:
    def __init__(self, output_dir: str | Path, *, allowed_hosts: tuple[str, ...],
                 path_markers: tuple[str, ...]):
        if not allowed_hosts or any(not host or "/" in host for host in allowed_hosts):
            raise ValueError("allowed_hosts must contain exact hostnames")
        if not path_markers or any(not marker.startswith("/") for marker in path_markers):
            raise ValueError("path_markers must be absolute path fragments")
        self.output_dir = Path(output_dir).resolve()
        self.audio_dir = self.output_dir / "audio"
        self.manifest = self.output_dir / "prompt_manifest.jsonl"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_hosts = frozenset(allowed_hosts)
        self.path_markers = tuple(path_markers)
        self.lock = threading.Lock()
        self._capture_ids = set()
        if self.manifest.is_file():
            for line in self.manifest.read_text(encoding="utf-8").splitlines():
                try:
                    self._capture_ids.add(json.loads(line)["capture_id"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    raise ValueError("prompt manifest is invalid") from None

    def _source(self, raw: str) -> tuple[str, str, str]:
        parsed = urlsplit(raw)
        if (parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not any(marker in parsed.path for marker in self.path_markers)):
            raise ValueError("prompt source rejected")
        extension = Path(parsed.path).suffix.lower()
        if extension not in _TYPES:
            raise ValueError("prompt audio extension rejected")
        return parsed.hostname, parsed.path, extension

    def record(self, *, source_url: str, source_profile: str, source_test: str,
               source_question: str, content_type: str, audio: bytes) -> dict:
        host, path, extension = self._source(source_url)
        for name, value in {
            "source_profile": source_profile,
            "source_test": source_test,
            "source_question": source_question,
        }.items():
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise ValueError(f"invalid {name}")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type not in _TYPES[extension]:
            raise ValueError("prompt content type rejected")
        if not isinstance(audio, bytes) or not audio:
            raise ValueError("prompt audio required")
        digest = hashlib.sha256(audio).hexdigest()
        identity = json.dumps({
            "sha256": digest, "source_host": host, "source_path": path,
            "source_profile": source_profile, "source_test": source_test,
            "source_question": source_question,
        }, sort_keys=True, separators=(",", ":"))
        capture_id = hashlib.sha256(identity.encode()).hexdigest()
        target = self.audio_dir / f"{digest}{extension}"
        record = {
            "version": 1, "capture_id": capture_id, "sha256": digest,
            "bytes": len(audio), "file": str(target.relative_to(self.output_dir)),
            "source_host": host, "source_path": path,
            "source_profile": source_profile, "source_test": source_test,
            "source_question": source_question,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.lock:
            if capture_id in self._capture_ids:
                return {**record, "status": "duplicate"}
            temporary = self.audio_dir / f".{digest}.{threading.get_ident()}.partial"
            temporary.write_bytes(audio)
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                temporary.unlink(missing_ok=True)
                raise ValueError("prompt artifact conflict")
            os.replace(temporary, target)
            with self.manifest.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._capture_ids.add(capture_id)
        return {**record, "status": "stored"}


def make_server(output_dir: str | Path, *, token: str, allowed_origin: str,
                allowed_hosts: tuple[str, ...], path_markers: tuple[str, ...],
                port: int = 0, max_bytes: int = 20_000_000,
                bank: PromptAudioBank | None = None) -> ThreadingHTTPServer:
    origin = urlsplit(allowed_origin)
    if (origin.scheme != "https" or not origin.hostname or origin.path not in ("", "/")
            or origin.query or origin.fragment or origin.username or origin.password):
        raise ValueError("an explicit HTTPS origin is required")
    if not isinstance(token, str) or len(token) < 24:
        raise ValueError("capture token must contain at least 24 characters")
    if not allowed_hosts or any(not host or "/" in host for host in allowed_hosts):
        raise ValueError("allowed_hosts must contain exact hostnames")
    if not path_markers or any(not marker.startswith("/") for marker in path_markers):
        raise ValueError("path_markers must be absolute path fragments")
    capture_bank = bank or PromptAudioBank(
        output_dir, allowed_hosts=allowed_hosts, path_markers=path_markers)
    if (capture_bank.allowed_hosts != frozenset(allowed_hosts)
            or capture_bank.path_markers != tuple(path_markers)):
        raise ValueError("prompt bank allowlist does not match server allowlist")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def _target_allowed(self):
            return (self.path == "/capture-prompt"
                    and self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
                    and self.headers.get("Origin") == allowed_origin)

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type,X-Prompt-Token,X-Prompt-Source,X-Source-Profile,"
                "X-Source-Test,X-Source-Question",
            )
            self.send_header("Access-Control-Allow-Methods", "POST,OPTIONS")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")

        def do_OPTIONS(self):
            if not self._target_allowed():
                self.send_response(403)
            else:
                self.send_response(204)
                self._cors()
            self.end_headers()

        def do_POST(self):
            if not self._target_allowed():
                return self._reply(403, {"status": "error", "reason": "origin_or_path_rejected"})
            if not hmac.compare_digest(self.headers.get("X-Prompt-Token", ""), token):
                return self._reply(403, {"status": "error", "reason": "token_rejected"})
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 1 <= length <= max_bytes:
                    raise ValueError("invalid prompt size")
                audio = self.rfile.read(length)
                if len(audio) != length:
                    raise ValueError("truncated prompt")
                report = capture_bank.record(
                    source_url=self.headers.get("X-Prompt-Source", ""),
                    source_profile=self.headers.get("X-Source-Profile", ""),
                    source_test=self.headers.get("X-Source-Test", ""),
                    source_question=self.headers.get("X-Source-Question", ""),
                    content_type=self.headers.get("Content-Type", ""), audio=audio,
                )
                self._reply(201 if report["status"] == "stored" else 200, report)
            except (ValueError, OSError) as error:
                self._reply(400, {"status": "error", "reason": str(error)[:120]})

        def _reply(self, status, data):
            payload = json.dumps(data, separators=(",", ":")).encode()
            self.send_response(status)
            if self._target_allowed():
                self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Store authorized prompt audio on localhost")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--allow-audio-host", action="append", required=True)
    parser.add_argument("--path-marker", action="append", required=True)
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args(argv)
    try:
        server = make_server(
            args.output, token=os.environ.get("QA_PROMPT_CAPTURE_TOKEN", ""),
            allowed_origin=args.origin, allowed_hosts=tuple(args.allow_audio_host),
            path_markers=tuple(args.path_marker), port=args.port,
        )
    except Exception as error:
        print(json.dumps({"status": "error", "reason": str(error)[:120]}))
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
