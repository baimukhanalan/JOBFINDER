"""Client for OUR synthetic media API; no microphone emulation or assessment upload."""
import asyncio
import base64
import hashlib
import json
from urllib.parse import urlsplit
import urllib.request
from qa_bot.audio.service import wav_duration
from qa_bot.adapters.stt.transport import NoRedirect


class MockMediaClient:
    def __init__(self, base_url):
        p = urlsplit(base_url)
        if p.scheme != "http" or p.hostname != "127.0.0.1" or p.username or p.password or p.query or p.fragment:
            raise ValueError("our unauthenticated mock API is loopback only")
        self.base = base_url.rstrip("/")

    async def put_response(self, question_id, audio, *, idempotency_key, timeout=10):
        wav_duration(audio)
        digest = hashlib.sha256(audio).hexdigest()
        body = json.dumps({"question_id": question_id, "audio_hash": digest,
                           "idempotency_key": idempotency_key, "synthetic": True,
                           "wav_base64": base64.b64encode(audio).decode()}).encode()
        def send():
            req = urllib.request.Request(self.base + "/v1/responses", body,
                                         {"Content-Type": "application/json"})
            with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
                result = json.loads(response.read(8192))
                if result.get("question_id") != question_id or result.get("audio_hash") != digest:
                    raise ValueError("stale media receipt")
                if result.get("status") != "accepted_mock":
                    raise ValueError("media not accepted")
                return result
        return await asyncio.to_thread(send)
