"""Loopback-only low-latency preparation and playback for timed speech questions."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from qa_bot.adapters.tts.macos_local import MacOSLocalTTS
from qa_bot.audio.replay_bank import SpeechReplayBank, normalize_prompt


class LiveSpeechController:
    def __init__(self, database, artifacts, *, voice="Samantha", speech_factory=None,
                 player=None, replay_only=False):
        self.database = Path(database)
        self.artifacts = Path(artifacts)
        self.voice = voice
        self.replay_only = replay_only
        self.speech_factory = speech_factory or (
            lambda: MacOSLocalTTS(allowed_voices=(voice,))
        )
        self.player = player or self._afplay
        self.lock = threading.Lock()

    @staticmethod
    def _afplay(path):
        return subprocess.Popen(
            ["/usr/bin/afplay", str(path)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        ).pid

    @staticmethod
    def _request(data):
        if not isinstance(data, dict) or set(data) != {
            "question", "answer", "source_profile", "source_test", "source_question"
        }:
            raise ValueError("exact speech request fields required")
        result = {}
        for name, value in data.items():
            if not isinstance(value, str) or not value.strip() or len(value) > 5000:
                raise ValueError(f"invalid {name}")
            result[name] = normalize_prompt(value)
        return result

    def prepare(self, raw):
        data = self._request(raw)
        with self.lock, SpeechReplayBank(self.database, self.artifacts) as bank:
            speech = self.speech_factory()
            replay = asyncio.run(bank.get_or_create(
                data["question"], data["answer"], speech=speech, voice=self.voice,
                model="macos-say", settings=None,
                source_profile=data["source_profile"], source_test=data["source_test"],
                source_question=data["source_question"], timeout=20, replay_only=self.replay_only,
            ))
            return {
                "status": "ready", "source": replay.source,
                "question_key": replay.question_key,
                "audio_sha256": replay.audio_sha256,
                "local_generation_requests": speech.generation_requests,
            }

    def play(self, raw):
        data = self._request(raw)
        with self.lock, SpeechReplayBank(self.database, self.artifacts) as bank:
            replay = bank.lookup(data["question"], data["answer"])
            if replay is None:
                raise ValueError("speech answer was not prepared")
            pid = self.player(replay.wav_path)
            return {
                "status": "playing", "question_key": replay.question_key,
                "audio_sha256": replay.audio_sha256, "player_pid": int(pid),
            }

    def audio(self, raw):
        """Return the prepared WAV for direct browser MediaStream injection."""
        data = self._request(raw)
        with self.lock, SpeechReplayBank(self.database, self.artifacts) as bank:
            replay = bank.lookup(data["question"], data["answer"])
            if replay is None:
                raise ValueError("speech answer was not prepared")
            return replay.wav_path.read_bytes(), replay.wav_sha256


def make_server(controller, *, token, allowed_origin, port=0, max_bytes=20_000):
    if not isinstance(token, str) or len(token) < 24:
        raise ValueError("bridge token must contain at least 24 characters")
    if not isinstance(allowed_origin, str) or not allowed_origin.startswith("https://"):
        raise ValueError("explicit HTTPS origin required")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Speech-Token")
            self.send_header("Access-Control-Expose-Headers", "X-Audio-SHA256")
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
            if self.path not in ("/prepare", "/play", "/audio"):
                return self._reply(404, {"status": "error", "reason": "path_rejected"})
            if (self.headers.get("Origin") != allowed_origin
                    or self.headers.get("X-Speech-Token") != token):
                return self._reply(403, {"status": "error", "reason": "request_rejected"})
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 1 <= length <= max_bytes:
                    raise ValueError("invalid body size")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("truncated body")
                data = json.loads(body)
                if self.path == "/prepare":
                    self._reply(200, controller.prepare(data))
                elif self.path == "/play":
                    self._reply(200, controller.play(data))
                else:
                    wav, digest = controller.audio(data)
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("X-Audio-SHA256", digest)
                    self.send_header("Content-Length", str(len(wav)))
                    self.end_headers()
                    self.wfile.write(wav)
            except Exception as error:
                self._reply(400, {"status": "error", "reason": str(error)[:160]})

        def _reply(self, status, data):
            payload = json.dumps(data, separators=(",", ":")).encode()
            self.send_response(status)
            if self.headers.get("Origin") == allowed_origin:
                self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare and play exact local speech on loopback")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--voice", default="Samantha")
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args(argv)
    try:
        controller = LiveSpeechController(
            args.database, args.artifacts, voice=args.voice,
        )
        server = make_server(
            controller, token=os.environ.get("QA_SPEECH_TOKEN", ""),
            allowed_origin=args.origin, port=args.port,
        )
    except Exception as error:
        print(json.dumps({"status": "error", "reason": str(error)[:160]}))
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
