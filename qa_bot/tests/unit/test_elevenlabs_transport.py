import io
import json
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

from qa_bot.adapters.stt.transport import ElevenLabsHTTP, ProviderError


FREE = {"tier": "free", "can_extend_character_limit": False,
        "allowed_to_extend_character_limit": False,
        "character_count": 100, "character_limit": 10000}


def response(data, mime="application/json"):
    result = MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = json.dumps(data).encode() if isinstance(data, dict) else data
    result.headers.get_content_type.return_value = mime
    return result


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        return ElevenLabsHTTP(enabled=True, max_requests=2, api_key="test-only")

    async def test_preflight_get_only_filters_premium_voices(self):
        voices = {"voices": [
            {"voice_id": "freevoice", "category": "premade", "available_for_tiers": ["free"]},
            {"voice_id": "defaultvoice", "category": "premade", "available_for_tiers": []},
            {"voice_id": "paidvoice", "category": "premade", "available_for_tiers": ["creator"]},
            {"voice_id": "clone", "category": "cloned", "available_for_tiers": ["free"]}]}
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = [response(FREE), response(voices)]
            client = self.make()
            result = await client.preflight(timeout=1)
            self.assertEqual(result["voices"], ["defaultvoice", "freevoice"])
            self.assertEqual(result["remaining_characters"], 9900)
            self.assertTrue(all(c.args[0].get_method() == "GET" for c in build.return_value.open.call_args_list))
            self.assertEqual(client.remaining, 2)

    async def test_paid_account_stops_before_voice_or_generation(self):
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value = response({**FREE, "tier": "creator"})
            with self.assertRaises(ValueError):
                await self.make().preflight(timeout=1)
            self.assertEqual(build.return_value.open.call_count, 1)

    async def test_http_error_does_not_echo_credentials(self):
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = urllib.error.HTTPError(
                "https://test.invalid/private", 401, "test-only", {}, io.BytesIO(b"private"))
            with self.assertRaises(ProviderError) as raised:
                await self.make().preflight(timeout=1)
            self.assertEqual(raised.exception.code, "http_401")
            self.assertNotIn("test-only", str(raised.exception))

    async def test_exact_endpoint_allowlist_before_network(self):
        with patch("urllib.request.build_opener") as build:
            for path in ("/v1/speech-to-text/delete", "/v1/text-to-speech/../../user", "https://bad.invalid"):
                with self.assertRaises(ValueError):
                    await self.make().post(path, b"{}", "application/json", timeout=1)
            build.assert_not_called()

    async def test_tts_rejects_json_as_audio(self):
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = [response(FREE), response({"error": "oops"})]
            with self.assertRaises(ProviderError):
                await self.make().post("/v1/text-to-speech/voice?output_format=pcm_16000",
                                       b'{"text":"synthetic"}', "application/json", timeout=1)

    async def test_mp3_endpoint_accepts_audio_mpeg(self):
        payload = b"ID3" + bytes(200)
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = [response(FREE), response(payload, "audio/mpeg")]
            result = await self.make().post(
                "/v1/text-to-speech/voice?output_format=mp3_44100_128",
                b'{"text":"synthetic"}', "application/json", timeout=1,
            )
            self.assertEqual(result, payload)

    def test_free_gate_rejects_trial_and_bad_reserve(self):
        for data, reserve in (({**FREE, "tier": "trial"}, 1), (FREE, -1), (FREE, True)):
            with self.assertRaises(ValueError):
                ElevenLabsHTTP.check_free_subscription(data, reserve)
