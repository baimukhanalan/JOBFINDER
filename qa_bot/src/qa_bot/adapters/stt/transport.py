"""Explicit opt-in ElevenLabs HTTP transport. Disabled by default."""
import asyncio
import os
import json
import math
import re
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ProviderError(ValueError):
    """Only allowlisted error codes; never include provider bodies/URLs/keys."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ElevenLabsHTTP:
    def __init__(self, *, enabled=False, max_requests=0, api_key=None):
        if not enabled or type(max_requests) is not int or not 1 <= max_requests <= 10:
            raise ValueError("explicit bounded external-call permission required")
        self.remaining = max_requests
        self._key = api_key if api_key is not None else os.environ.get("ELEVENLABS_API_KEY")
        if not isinstance(self._key, str) or not self._key or re.search(r"\s", self._key):
            raise ValueError("ElevenLabs credentials unavailable")

    def close(self):
        # Release our reference. Python does not guarantee memory zeroization.
        self._key = None

    @staticmethod
    def check_free_subscription(data, reserve):
        if type(reserve) is not int or reserve < 0:
            raise ValueError("invalid quota reservation")
        if (not isinstance(data, dict) or data.get("tier") != "free" or
            data.get("can_extend_character_limit") is not False or
            data.get("allowed_to_extend_character_limit") is not False):
            raise ValueError("verified free tier without overage required")
        used, limit = data.get("character_count"), data.get("character_limit")
        if type(used) is not int or type(limit) is not int or used < 0 or limit - used < reserve:
            raise ValueError("free quota unavailable or insufficient")

    def _request(self, path, *, timeout, body=None, content_type=None, audio_format=None):
        if not self._key:
            raise ProviderError("client_closed")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("timeout must be between zero and sixty seconds")
        headers = {"xi-api-key": self._key}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            opener = urllib.request.build_opener(NoRedirect)
            req = urllib.request.Request("https://api.elevenlabs.io" + path, body, headers)
            with opener.open(req, timeout=timeout) as response:
                mime = response.headers.get_content_type()
                if audio_format == "pcm":
                    accepted = ("audio/pcm", "audio/x-pcm", "application/octet-stream")
                elif audio_format == "mp3":
                    accepted = ("audio/mpeg", "audio/mp3", "application/octet-stream")
                else:
                    accepted = ("application/json",)
                if mime not in accepted:
                    raise ProviderError("unexpected_content_type")
                maximum = 10_000_000 if audio_format else 1_000_000
                data = response.read(maximum + 1)
                if len(data) > maximum:
                    raise ProviderError("response_too_large")
                return data if audio_format else json.loads(data)
        except ProviderError:
            raise
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise ProviderError(f"http_{code}") from None
        except Exception:
            raise ProviderError("request_failed") from None

    async def preflight(self, *, timeout=20):
        """Two read-only calls. Does not generate speech or change billing."""
        account = await asyncio.to_thread(self._request, "/v1/user/subscription", timeout=timeout)
        self.check_free_subscription(account, 0)
        data = await asyncio.to_thread(self._request,
            "/v2/voices?page_size=100&include_custom_rates=false",
            timeout=timeout)
        if not isinstance(data, dict) or not isinstance(data.get("voices"), list):
            raise ProviderError("invalid_voice_response")
        voices = sorted({v["voice_id"] for v in data["voices"] if isinstance(v, dict)
            and v.get("category") in ("premade", "high_quality", "professional")
            and ("free" in (v.get("available_for_tiers") or [])
                 or (v.get("category") == "premade" and not v.get("available_for_tiers") and not v.get("sharing")))
            and (not v.get("sharing") or (v["sharing"].get("free_users_allowed") is True
                 and v["sharing"].get("rate") in (None, 0)))
            and isinstance(v.get("voice_id"), str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", v["voice_id"])})
        return {"tier": "free", "remaining_characters": account["character_limit"] - account["character_count"],
                "overage_enabled": False, "voices": voices,
                "returned_voice_count": len(data["voices"]),
                "premade_voice_count": sum(v.get("category") == "premade" for v in data["voices"] if isinstance(v, dict)),
                "unclassified_voice_tiers": sum(not v.get("available_for_tiers") for v in data["voices"]
                                                   if isinstance(v, dict))}

    async def post(self, path, body, content_type, *, timeout):
        if self.remaining <= 0:
            raise ValueError("request budget exhausted")
        match = re.fullmatch(
            r"/v1/text-to-speech/[A-Za-z0-9_-]{1,100}\?output_format=(pcm_16000|mp3_44100_128)",
            path,
        )
        is_tts = bool(match)
        if path != "/v1/speech-to-text" and not is_tts:
            raise ValueError("unsupported endpoint")
        if not isinstance(body, bytes) or not 0 < len(body) <= 10_000_000:
            raise ValueError("invalid request body")
        if is_tts:
            if content_type != "application/json":
                raise ValueError("invalid TTS content type")
            data = json.loads(body)
            if not isinstance(data.get("text"), str) or not 0 < len(data["text"]) <= 5000:
                raise ValueError("invalid TTS text")
            reserve = len(data["text"])
        else:
            if not re.fullmatch(r"multipart/form-data; boundary=qa-[a-f0-9]{32}", content_type):
                raise ValueError("invalid STT content type")
            reserve = 3000
        self.remaining -= 1
        subscription = await asyncio.to_thread(self._request, "/v1/user/subscription", timeout=timeout)
        self.check_free_subscription(subscription, reserve)
        audio_format = None if not is_tts else ("pcm" if match.group(1) == "pcm_16000" else "mp3")
        result = await asyncio.to_thread(self._request, path, timeout=timeout, body=body,
                                         content_type=content_type, audio_format=audio_format)
        return result if is_tts else json.dumps(result).encode()
