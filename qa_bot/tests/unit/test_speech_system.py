import base64
import asyncio
import hashlib
import io
import json
import threading
import unittest
import urllib.request
from unittest.mock import AsyncMock
import wave
from qa_bot.audio.service import SpeechService, wav_duration, parse_transcript
from qa_bot.adapters.stt.elevenlabs import ElevenLabsProvider
from qa_bot.staging.media_api import make_server
from qa_bot.adapters.stt.transport import ElevenLabsHTTP
from qa_bot.adapters.staging_audio.client import MockMediaClient


def wav():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(bytes(32000))
    return stream.getvalue()


def transcription(logprob=-0.01):
    return {"text": "HELLO", "language_code": "en", "language_probability": 0.99,
            "words": [{"text": "HELLO", "start": 0, "end": 0.8,
                       "type": "word", "logprob": logprob}]}


class SpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_language_hint_and_iso_alias_cache(self):
        provider = AsyncMock()
        provider.transcribe.return_value = {**transcription(), "language_code": "eng"}
        service = SpeechService(provider, ":memory:", retries=0)
        try:
            result = await service.transcribe(wav(), language="en", synthetic=True)
            self.assertEqual(result.language, "en")
            self.assertEqual(provider.transcribe.call_args.kwargs["language"], "en")
            await service.transcribe(wav(), language="eng", synthetic=True)
            self.assertEqual(provider.transcribe.await_count, 1)
        finally:
            service.close()

    async def test_known_language_does_not_bypass_quality_gate(self):
        provider = AsyncMock()
        provider.transcribe.return_value = {**transcription(), "language_probability": 0.8}
        service = SpeechService(provider, ":memory:", retries=0)
        try:
            with self.assertRaises(ValueError):
                await service.transcribe(wav(), language="en", synthetic=True)
        finally:
            service.close()

    async def test_tts_to_our_media_client(self):
        server = make_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        provider = AsyncMock()
        provider.synthesize.return_value = bytes(32000)
        service = SpeechService(provider, ":memory:", allowed_voices=("test",))
        try:
            audio = await service.synthesize("HELLO", voice="test", synthetic=True)
            client = MockMediaClient(f"http://127.0.0.1:{server.server_port}")
            result = await client.put_response("q", audio, idempotency_key="speech-1")
            self.assertEqual(result["status"], "accepted_mock")
        finally:
            service.close()
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()

    async def test_free_plan_gate(self):
        data = {"tier": "free", "can_extend_character_limit": False,
                "allowed_to_extend_character_limit": False,
                "character_count": 100, "character_limit": 10000}
        ElevenLabsHTTP.check_free_subscription(data, 500)
        for changed in ({**data, "tier": "creator"},
                        {**data, "allowed_to_extend_character_limit": True},
                        {**data, "character_count": 9999}, {}):
            with self.assertRaises(ValueError):
                ElevenLabsHTTP.check_free_subscription(changed, 500)

    async def test_stt_cache_and_quality_retry(self):
        provider = AsyncMock()
        provider.transcribe.side_effect = [transcription(-2), transcription()]
        service = SpeechService(provider, ":memory:")
        try:
            result = await service.transcribe(wav(), synthetic=True)
            self.assertEqual(result.text, "HELLO")
            self.assertEqual(result.audio_hash, hashlib.sha256(wav()).hexdigest())
            self.assertEqual(provider.transcribe.await_count, 2)
            await service.transcribe(wav(), synthetic=True)
            self.assertEqual(provider.transcribe.await_count, 2)
        finally:
            service.close()

    async def test_missing_confidence_never_invented(self):
        self.assertIsNone(parse_transcript(transcription(None), wav()).confidence)
        provider = AsyncMock()
        provider.transcribe.return_value = transcription(None)
        service = SpeechService(provider, ":memory:", retries=0)
        try:
            with self.assertRaises(ValueError):
                await service.transcribe(wav(), synthetic=True)
        finally:
            service.close()

    async def test_bad_timestamps(self):
        data = transcription()
        data["words"][0]["end"] = 999
        with self.assertRaises(ValueError):
            parse_transcript(data, wav())

    async def test_tts_voice_cache_duration(self):
        provider = AsyncMock()
        provider.synthesize.return_value = bytes(32000)
        service = SpeechService(provider, ":memory:", allowed_voices=("mock-voice",))
        try:
            data = await service.synthesize("HELLO", voice="mock-voice", synthetic=True)
            self.assertEqual(wav_duration(data), 1)
            self.assertEqual(data, await service.synthesize("HELLO", voice="mock-voice", synthetic=True))
            self.assertEqual(provider.synthesize.await_count, 1)
            with self.assertRaises(ValueError):
                await service.synthesize("HELLO", voice="not-allowed", synthetic=True)
            with self.assertRaises(ValueError):
                await service.synthesize("HELLO", voice="mock-voice")
        finally:
            service.close()

    async def test_elevenlabs_request_contracts(self):
        transport = AsyncMock()
        transport.post.return_value = json.dumps(transcription()).encode()
        provider = ElevenLabsProvider(transport)
        result = await provider.transcribe(wav(), model="scribe_v2", language="en", timeout=1)
        self.assertEqual(result["text"], "HELLO")
        call = transport.post.call_args
        self.assertEqual(call.args[0], "/v1/speech-to-text")
        self.assertIn(b"synthetic.wav", call.args[1])
        transport.post.return_value = bytes(32000)
        await provider.synthesize("HELLO", voice="mock-voice", model="eleven_multilingual_v2",
                                  settings={"stability": 0.5, "similarity_boost": 0.75}, timeout=1)
        self.assertIn("output_format=pcm_16000", transport.post.call_args.args[0])
        transport.post.return_value = b"ID3" + bytes(200)
        await provider.synthesize_mp3("HELLO", voice="mock-voice", model="eleven_multilingual_v2",
                                      settings={"stability": 0.5, "similarity_boost": 0.75}, timeout=1)
        self.assertIn("output_format=mp3_44100_128", transport.post.call_args.args[0])

    async def test_mp3_cache(self):
        provider = AsyncMock()
        provider.synthesize_mp3.return_value = b"ID3" + bytes(200)
        service = SpeechService(provider, ":memory:", allowed_voices=("mock-voice",))
        try:
            first = await service.synthesize_mp3("HELLO", voice="mock-voice", synthetic=True)
            second = await service.synthesize_mp3("HELLO", voice="mock-voice", synthetic=True)
            self.assertEqual(first, second)
            self.assertEqual(provider.synthesize_mp3.await_count, 1)
        finally:
            service.close()

    async def test_invalid_wave(self):
        for data in (b"", b"mp3", wav()[:-20]):
            with self.assertRaises(ValueError):
                wav_duration(data)


class MediaAPITests(unittest.TestCase):
    def test_our_mock_api_idempotency_and_conflict(self):
        server = make_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/v1/responses"
            data = {"question_id": "q", "idempotency_key": "request-1", "synthetic": True,
                    "audio_hash": hashlib.sha256(wav()).hexdigest(),
                    "wav_base64": base64.b64encode(wav()).decode()}
            for _ in range(2):
                req = urllib.request.Request(url, json.dumps(data).encode(),
                                             {"Content-Type": "application/json"})
                with urllib.request.urlopen(req) as response:
                    self.assertEqual(json.load(response)["status"], "accepted_mock")
            data["question_id"] = "changed"
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(urllib.request.Request(url, json.dumps(data).encode()))
            self.assertEqual(raised.exception.code, 409)
            raised.exception.close()
            with urllib.request.urlopen(url + "/request-1") as response:
                self.assertEqual(json.load(response)["question_id"], "q")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
