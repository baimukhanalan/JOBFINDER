import asyncio
import hashlib
import io
import json
import math
import re
import sqlite3
import wave
from dataclasses import dataclass, asdict, replace
from qa_bot.perception.extractor import rehash_spec


def normalize_language(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[a-z]{2,3}", value):
        raise ValueError("ISO language code required")
    return {"eng": "en", "rus": "ru", "spa": "es", "fra": "fr", "deu": "de", "kaz": "kk"}.get(value, value)


def wav_duration(data):
    if not isinstance(data, bytes) or not data or len(data) > 10_000_000:
        raise ValueError("invalid audio size")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
                raise ValueError("only mono PCM16 16000Hz WAV accepted")
            frames = audio.getnframes()
            if len(audio.readframes(frames)) != frames * 2:
                raise ValueError("truncated audio")
            duration = frames / 16000
            if not 0 < duration <= 180:
                raise ValueError("audio duration outside policy")
            return duration
    except (wave.Error, EOFError) as error:
        raise ValueError("invalid WAV") from error


def wav_signal_metrics(data):
    """Return normalized peak/RMS/activity after validating the WAV container."""
    duration = wav_duration(data)
    with wave.open(io.BytesIO(data), "rb") as audio:
        pcm = audio.readframes(audio.getnframes())
    samples = [int.from_bytes(pcm[i:i + 2], "little", signed=True)
               for i in range(0, len(pcm), 2)]
    peak = max(abs(value) for value in samples) / 32768
    rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768
    active_fraction = sum(abs(value) >= 164 for value in samples) / len(samples)
    return {"duration": duration, "peak": peak, "rms": rms,
            "active_fraction": active_fraction}


def validate_mp3(data):
    # Imported lazily in replay_bank too; kept here for the provider-facing API.
    if not isinstance(data, bytes) or not 64 <= len(data) <= 10_000_000:
        raise ValueError("invalid MP3 size")
    if data[:3] == b"ID3":
        return
    if len(data) >= 4 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0 and data[1] & 0x1E:
        return
    raise ValueError("invalid MP3 header")


@dataclass(frozen=True)
class SpeechTranscript:
    audio_hash: str
    text: str
    language: str
    language_confidence: float
    words: tuple[dict, ...]
    confidence: float | None


class SpeechReviewRequired(ValueError):
    def __init__(self, transcript):
        super().__init__("transcription requires review")
        self.confidence = transcript.confidence
        self.language_confidence = transcript.language_confidence
        self.word_count = len(transcript.words)


def attach_transcript(question, transcript):
    """Bind a verified transcript to the exact audio bytes of this question."""
    if transcript.confidence is None or transcript.confidence < 0.9:
        raise ValueError("unreliable transcript")
    if not any(a.media_type.startswith("audio/") and a.sha256 == transcript.audio_hash
               for a in question.assets):
        raise ValueError("transcript does not belong to question audio")
    return rehash_spec(replace(
        question, context=question.context + (transcript.text,),
        provenance=question.provenance + ("stt:" + transcript.audio_hash,),
        extraction_confidence=min(question.extraction_confidence, transcript.confidence)))


def parse_transcript(data, audio):
    duration = wav_duration(audio)
    text, language = data["text"], data["language_code"]
    probability = data["language_probability"]
    words = tuple(w for w in data["words"] if w.get("type") == "word")
    if not isinstance(text, str) or not text.strip() or not isinstance(language, str) or not words:
        raise ValueError("incomplete transcript")
    if isinstance(probability, bool) or not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("invalid language confidence")
    confidences, last = [], 0
    for word in words:
        start, end = word["start"], word["end"]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   for v in (start, end)):
            raise ValueError("invalid timestamps")
        if not 0 <= start <= end <= duration + 0.05 or start < last:
            raise ValueError("invalid timestamp order")
        last = end
        logprob = word.get("logprob")
        if logprob is None:
            confidences.append(None)
        elif isinstance(logprob, bool) or not math.isfinite(logprob) or logprob > 0:
            raise ValueError("invalid word logprob")
        else:
            confidences.append(math.exp(logprob))
    if "".join(text.split()) != "".join("".join(w["text"].split()) for w in words):
        raise ValueError("transcript text and word segments conflict")
    certainty = None if None in confidences else min(confidences)
    return SpeechTranscript(hashlib.sha256(audio).hexdigest(), text, normalize_language(language),
                            probability, words, certainty)


class SpeechService:
    def __init__(self, provider, database, *, allowed_voices=(), retries=1):
        if retries not in (0, 1):
            raise ValueError("at most one retry")
        self.provider, self.voices, self.retries = provider, frozenset(allowed_voices), retries
        self.db = sqlite3.connect(database)
        self.db.execute("CREATE TABLE IF NOT EXISTS audio_cache(key TEXT PRIMARY KEY, payload BLOB NOT NULL)")

    def close(self):
        self.db.close()

    def _key(self, operation, settings):
        return hashlib.sha256(json.dumps([operation, settings], sort_keys=True).encode()).hexdigest()

    def _get(self, key):
        row = self.db.execute("SELECT payload FROM audio_cache WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _put(self, key, payload):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO audio_cache VALUES(?,?)", (key, payload))

    async def transcribe(self, audio, *, model="scribe_v2", language=None, synthetic=False, timeout=30):
        if not synthetic:
            raise ValueError("only authorized synthetic audio enabled")
        wav_duration(audio)
        language = normalize_language(language)
        key = self._key("stt", [hashlib.sha256(audio).hexdigest(), model, language])
        cached = self._get(key)
        if cached:
            data = json.loads(cached)
            if data["audio_hash"] != hashlib.sha256(audio).hexdigest():
                raise ValueError("cached transcript hash mismatch")
            result = parse_transcript({"text": data["text"], "language_code": data["language"],
                                       "language_probability": data["language_confidence"],
                                       "words": data["words"]}, audio)
            if (result.confidence is None or result.confidence < 0.9 or result.language_confidence < 0.9
                or language and result.language != language):
                raise ValueError("cached transcript invalid")
            return result
        async with asyncio.timeout(timeout):
            for attempt in range(self.retries + 1):
                data = await self.provider.transcribe(audio, model=model, language=language, timeout=timeout)
                transcript = parse_transcript(data, audio)
                if (transcript.confidence is not None and transcript.confidence >= 0.9
                    and transcript.language_confidence >= 0.9
                    and (not language or transcript.language == language)):
                    self._put(key, json.dumps(asdict(transcript)).encode())
                    return transcript
        raise SpeechReviewRequired(transcript)

    async def synthesize(self, text, *, voice, model="eleven_multilingual_v2",
                         settings=None, synthetic=False, timeout=30):
        if not synthetic or voice not in self.voices or not text.strip() or len(text) > 5000:
            raise ValueError("synthetic text and approved voice required")
        settings = settings or {"stability": 0.5, "similarity_boost": 0.75}
        if set(settings) != {"stability", "similarity_boost"} or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
            for v in settings.values()
        ):
            raise ValueError("invalid voice settings")
        key = self._key("tts", [text, voice, model, settings, "pcm_16000"])
        cached = self._get(key)
        if cached:
            wav_duration(cached)
            return cached
        async with asyncio.timeout(timeout):
            pcm = await self.provider.synthesize(text, voice=voice, model=model,
                                                 settings=settings, timeout=timeout)
        if not isinstance(pcm, bytes) or len(pcm) % 2 or not 0 < len(pcm) <= 5_760_000:
            raise ValueError("invalid PCM result")
        stream = io.BytesIO()
        with wave.open(stream, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(pcm)
        result = stream.getvalue()
        wav_duration(result)
        self._put(key, result)
        return result

    async def synthesize_mp3(self, text, *, voice, model="eleven_multilingual_v2",
                             settings=None, synthetic=False, timeout=30):
        if not synthetic or voice not in self.voices or not text.strip() or len(text) > 5000:
            raise ValueError("synthetic text and approved voice required")
        settings = settings or {"stability": 0.5, "similarity_boost": 0.75}
        if set(settings) != {"stability", "similarity_boost"} or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
            for v in settings.values()
        ):
            raise ValueError("invalid voice settings")
        key = self._key("tts", [text, voice, model, settings, "mp3_44100_128"])
        cached = self._get(key)
        if cached:
            validate_mp3(cached)
            return cached
        async with asyncio.timeout(timeout):
            result = await self.provider.synthesize_mp3(
                text, voice=voice, model=model, settings=settings, timeout=timeout)
        validate_mp3(result)
        self._put(key, result)
        return result
