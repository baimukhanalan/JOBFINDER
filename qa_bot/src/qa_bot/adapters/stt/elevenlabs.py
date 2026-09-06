"""ElevenLabs request mapping through an injected, explicitly enabled transport."""
import json
import re
from uuid import uuid4


class ElevenLabsProvider:
    def __init__(self, transport):
        self.transport = transport

    async def transcribe(self, audio, *, model, language, timeout):
        boundary = "qa-" + uuid4().hex
        fields = {"model_id": model, "timestamps_granularity": "word"}
        if language:
            fields["language_code"] = language
        chunks = []
        for key, value in fields.items():
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise ValueError("invalid multipart value")
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="synthetic.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
        chunks.extend((audio, f"\r\n--{boundary}--\r\n".encode()))
        result = await self.transport.post("/v1/speech-to-text", b"".join(chunks),
                                           "multipart/form-data; boundary=" + boundary, timeout=timeout)
        return json.loads(result)

    async def synthesize(self, text, *, voice, model, settings, timeout):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", voice):
            raise ValueError("invalid voice ID")
        return await self.transport.post(
            f"/v1/text-to-speech/{voice}?output_format=pcm_16000",
            json.dumps({"text": text, "model_id": model, "voice_settings": settings}).encode(),
            "application/json", timeout=timeout)

    async def synthesize_mp3(self, text, *, voice, model, settings, timeout):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", voice):
            raise ValueError("invalid voice ID")
        return await self.transport.post(
            f"/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
            json.dumps({"text": text, "model_id": model, "voice_settings": settings}).encode(),
            "application/json", timeout=timeout)
