"""Offline macOS speech synthesis with a persistent MP3-compatible result."""
from __future__ import annotations

import asyncio
import io
import subprocess
import tempfile
import wave
from pathlib import Path

import lameenc

from qa_bot.audio.service import validate_mp3, wav_duration


class MacOSLocalTTS:
    """Generate speech locally with ``say`` and encode it with bundled LAME."""

    def __init__(
        self,
        *,
        allowed_voices=("Samantha",),
        say_executable="/usr/bin/say",
        converter_executable="/usr/bin/afconvert",
        bit_rate=96,
    ):
        self.voices = frozenset(allowed_voices)
        self.say_executable = Path(say_executable)
        self.converter_executable = Path(converter_executable)
        if not self.voices:
            raise ValueError("at least one local voice required")
        if not 32 <= bit_rate <= 320:
            raise ValueError("MP3 bit rate must be between 32 and 320 kbps")
        self.bit_rate = bit_rate
        self.generation_requests = 0

    def _validate(self, text, voice, model, settings, synthetic):
        if not synthetic or voice not in self.voices:
            raise ValueError("synthetic text and approved local voice required")
        if not isinstance(text, str) or not text.strip() or len(text) > 5000:
            raise ValueError("text must contain between 1 and 5000 characters")
        if model not in ("macos-say", "local"):
            raise ValueError("unsupported local speech model")
        if settings not in (None, {}):
            raise ValueError("local speech settings are fixed")
        if not self.say_executable.is_file() or not self.converter_executable.is_file():
            raise ValueError("macOS speech tools unavailable")

    def _render_wav(self, text, voice, root):
        aiff_path, wav_path = root / "speech.aiff", root / "speech.wav"
        subprocess.run(
            [str(self.say_executable), "-v", voice, "-o", str(aiff_path), "-f", "-"],
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=True,
        )
        subprocess.run(
            [str(self.converter_executable), str(aiff_path), str(wav_path),
             "-f", "WAVE", "-d", "LEI16@16000", "-c", "1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=True,
        )
        wav_bytes = wav_path.read_bytes()
        wav_duration(wav_bytes)
        return wav_bytes

    @staticmethod
    def _encode_mp3(wav_bytes, bit_rate):
        with wave.open(io.BytesIO(wav_bytes), "rb") as audio:
            pcm = audio.readframes(audio.getnframes())
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(bit_rate)
        encoder.set_in_sample_rate(16000)
        encoder.set_channels(1)
        encoder.set_quality(2)
        result = bytes(encoder.encode(pcm) + encoder.flush())
        validate_mp3(result)
        return result

    async def synthesize_mp3(
        self,
        text,
        *,
        voice,
        model="macos-say",
        settings=None,
        synthetic=False,
        timeout=30,
    ):
        self._validate(text, voice, model, settings, synthetic)
        self.generation_requests += 1

        def generate():
            with tempfile.TemporaryDirectory(prefix="qa-local-tts-") as directory:
                wav_bytes = self._render_wav(text, voice, Path(directory))
                return self._encode_mp3(wav_bytes, self.bit_rate)

        async with asyncio.timeout(timeout):
            return await asyncio.to_thread(generate)
