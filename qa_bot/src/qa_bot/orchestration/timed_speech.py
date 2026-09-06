"""Warm speech answers before a timed page enters its recording phase.

The critical rule is deliberately small: ``speak_now`` never performs TTS,
MP3 conversion, SQLite lookup, or file validation.  Those operations happen in
the background while the page displays its preparation phase.  At recording
time the method only triggers an already armed audio sink and records latency.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from qa_bot.adapters.tts.macos_local import MacOSLocalTTS
from qa_bot.audio.replay_bank import SpeechReplay, SpeechReplayBank, normalize_prompt
from qa_bot.audio.service import wav_duration


class TimedSpeechPhase(str, Enum):
    PREPARING = "preparing"
    ARMED = "armed"
    PLAYING = "playing"
    FAILED = "failed"


@dataclass(frozen=True)
class TimedSpeechRequest:
    question: str
    answer: str
    source_profile: str
    source_test: str
    source_question: str

    def normalized(self) -> "TimedSpeechRequest":
        values = {
            name: normalize_prompt(value)
            for name, value in {
                "question": self.question,
                "answer": self.answer,
                "source_profile": self.source_profile,
                "source_test": self.source_test,
                "source_question": self.source_question,
            }.items()
        }
        if any(len(value) > 5000 for value in values.values()):
            raise ValueError("timed speech field is too long")
        return TimedSpeechRequest(**values)


@dataclass(frozen=True)
class TimedSpeechPolicy:
    generation_timeout_seconds: float = 8.0
    maximum_start_latency_ms: float = 250.0

    def __post_init__(self):
        if not 0.1 <= self.generation_timeout_seconds <= 60:
            raise ValueError("generation timeout must be between 0.1 and 60 seconds")
        if not 10 <= self.maximum_start_latency_ms <= 1000:
            raise ValueError("start latency limit must be between 10 and 1000 ms")


@dataclass(frozen=True)
class TimedSpeechSnapshot:
    session_id: str
    phase: TimedSpeechPhase
    question_key: str | None
    audio_sha256: str | None
    source: str | None
    preparation_ms: float | None
    player_id: int | str | None
    error: str | None


@dataclass
class _Session:
    request: TimedSpeechRequest
    started_ns: int
    phase: TimedSpeechPhase = TimedSpeechPhase.PREPARING
    replay: SpeechReplay | None = None
    preparation_ms: float | None = None
    player_id: int | str | None = None
    error: str | None = None
    future: Future | None = None


class PreloadedAfplaySink:
    """Warm a WAV in the filesystem cache, then start it with minimal work."""

    def __init__(self, executable: str = "/usr/bin/afplay"):
        self.executable = Path(executable)
        self._armed: dict[str, tuple[Path, str, tuple[int, int]]] = {}
        self._lock = threading.Lock()

    def arm(self, session_id: str, wav_path: Path) -> None:
        if not self.executable.is_file():
            raise ValueError("local audio player unavailable")
        payload = wav_path.read_bytes()
        wav_duration(payload)
        digest = hashlib.sha256(payload).hexdigest()
        stat = wav_path.stat()
        with self._lock:
            self._armed[session_id] = (
                wav_path, digest, (stat.st_size, stat.st_mtime_ns),
            )

    def play(self, session_id: str) -> int:
        with self._lock:
            armed = self._armed.get(session_id)
        if armed is None:
            raise ValueError("audio session is not armed")
        path, expected_sha, expected_stat = armed
        # Full integrity and disk reads are paid during arm().  The cheap stat
        # guard catches a missing/replaced artifact without hashing at Speak Now.
        if not path.is_file():
            raise ValueError("armed audio disappeared")
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != expected_stat:
            raise ValueError("armed audio changed after preparation")
        process = subprocess.Popen(
            [str(self.executable), str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # The hash is kept for audit/debug consumers without re-reading here.
        if not expected_sha:
            process.terminate()
            raise ValueError("armed audio lacks integrity evidence")
        return process.pid


class AsyncTransitionJournal:
    """Append JSONL events off the answer-start critical path."""

    _STOP = object()

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue = queue.Queue()
        self._sequence = 0
        self._lock = threading.Lock()
        self._closed = False
        self._writer = threading.Thread(target=self._write, daemon=True)
        self._writer.start()

    def emit(self, event: dict) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("transition journal is closed")
            self._sequence += 1
            sequence = self._sequence
        self._queue.put({
            "sequence": sequence,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            **event,
        })

    def _write(self) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            while True:
                item = self._queue.get()
                try:
                    if item is self._STOP:
                        return
                    stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
                    stream.flush()
                finally:
                    self._queue.task_done()

    def flush(self) -> None:
        self._queue.join()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(self._STOP)
        self._queue.join()
        self._writer.join(timeout=5)


class TimedSpeechPipeline:
    """Prepare during Listen Carefully and trigger only armed audio at Speak Now."""

    def __init__(
        self,
        database: str | Path,
        artifacts: str | Path,
        journal: str | Path,
        *,
        voice: str = "Samantha",
        speech_factory=None,
        bank_factory=SpeechReplayBank,
        sink=None,
        policy: TimedSpeechPolicy | None = None,
        monotonic_ns=time.monotonic_ns,
    ):
        self.database = Path(database).resolve()
        self.artifacts = Path(artifacts).resolve()
        self.voice = voice
        self.speech_factory = speech_factory or (
            lambda: MacOSLocalTTS(allowed_voices=(voice,))
        )
        self.bank_factory = bank_factory
        self.sink = sink or PreloadedAfplaySink()
        self.policy = policy or TimedSpeechPolicy()
        self.monotonic_ns = monotonic_ns
        self.journal = AsyncTransitionJournal(journal)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qa-speech-warm")
        self._condition = threading.Condition()
        self._sessions: dict[str, _Session] = {}
        self._closed = False

    @staticmethod
    def _session_id(request: TimedSpeechRequest) -> str:
        canonical = json.dumps({
            "profile": request.source_profile,
            "test": request.source_test,
            "question": request.source_question,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]

    def prepare(self, raw_request: TimedSpeechRequest) -> str:
        """Queue all expensive work and return immediately during preparation."""
        request = raw_request.normalized()
        session_id = self._session_id(request)
        now = self.monotonic_ns()
        with self._condition:
            if self._closed:
                raise RuntimeError("timed speech pipeline is closed")
            existing = self._sessions.get(session_id)
            if existing is not None:
                if existing.request != request:
                    raise ValueError("speech session id collision")
                return session_id
            session = _Session(request=request, started_ns=now)
            self._sessions[session_id] = session
            self.journal.emit(self._event(session_id, session, "listen_carefully"))
            session.future = self._executor.submit(self._warm, session_id)
        return session_id

    def _warm(self, session_id: str) -> None:
        with self._condition:
            session = self._sessions[session_id]
            request = session.request
        try:
            with self.bank_factory(self.database, self.artifacts) as bank:
                replay = bank.replay(
                    request.question,
                    request.answer,
                    source_profile=request.source_profile,
                    source_test=request.source_test,
                    source_question=request.source_question,
                )
                if replay is None:
                    speech = self.speech_factory()
                    replay = asyncio.run(bank.get_or_create(
                        request.question,
                        request.answer,
                        speech=speech,
                        voice=self.voice,
                        model="macos-say",
                        settings=None,
                        source_profile=request.source_profile,
                        source_test=request.source_test,
                        source_question=request.source_question,
                        timeout=self.policy.generation_timeout_seconds,
                    ))
            self.sink.arm(session_id, replay.wav_path)
            elapsed = (self.monotonic_ns() - session.started_ns) / 1_000_000
            with self._condition:
                session.replay = replay
                session.preparation_ms = elapsed
                session.phase = TimedSpeechPhase.ARMED
                self.journal.emit(self._event(session_id, session, "audio_armed"))
                self._condition.notify_all()
        except Exception as error:
            with self._condition:
                session.error = str(error)[:240]
                session.phase = TimedSpeechPhase.FAILED
                self.journal.emit(self._event(session_id, session, "preparation_failed"))
                self._condition.notify_all()

    def wait_until_armed(self, session_id: str, timeout: float) -> TimedSpeechSnapshot:
        """Wait in the preparation phase, never after Speak Now is observed."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._session(session_id).phase == TimedSpeechPhase.PREPARING:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return self.snapshot(session_id)

    def speak_now(self, session_id: str) -> TimedSpeechSnapshot:
        """Trigger an armed sink without synthesis, conversion, DB work, or waiting."""
        observed_ns = self.monotonic_ns()
        with self._condition:
            session = self._session(session_id)
            if session.phase != TimedSpeechPhase.ARMED:
                self.journal.emit(self._event(session_id, session, "speak_now_not_armed"))
                raise RuntimeError(f"audio is not armed: {session.phase.value}")
        try:
            player_id = self.sink.play(session_id)
            latency_ms = (self.monotonic_ns() - observed_ns) / 1_000_000
            if latency_ms > self.policy.maximum_start_latency_ms:
                raise TimeoutError(
                    f"audio start latency {latency_ms:.1f} ms exceeds "
                    f"{self.policy.maximum_start_latency_ms:.1f} ms"
                )
            with self._condition:
                session.player_id = player_id
                session.phase = TimedSpeechPhase.PLAYING
                event = self._event(session_id, session, "speak_now_started")
                event["start_latency_ms"] = round(latency_ms, 3)
                self.journal.emit(event)
                return self.snapshot(session_id)
        except Exception as error:
            with self._condition:
                session.error = str(error)[:240]
                session.phase = TimedSpeechPhase.FAILED
                self.journal.emit(self._event(session_id, session, "playback_failed"))
            raise

    def _session(self, session_id: str) -> _Session:
        try:
            return self._sessions[session_id]
        except KeyError as error:
            raise KeyError("unknown timed speech session") from error

    def snapshot(self, session_id: str) -> TimedSpeechSnapshot:
        with self._condition:
            session = self._session(session_id)
            replay = session.replay
            return TimedSpeechSnapshot(
                session_id=session_id,
                phase=session.phase,
                question_key=replay.question_key if replay else None,
                audio_sha256=replay.audio_sha256 if replay else None,
                source=replay.source if replay else None,
                preparation_ms=session.preparation_ms,
                player_id=session.player_id,
                error=session.error,
            )

    def _event(self, session_id: str, session: _Session, transition: str) -> dict:
        replay = session.replay
        return {
            "transition": transition,
            "session_id": session_id,
            "phase": session.phase.value,
            "source_profile": session.request.source_profile,
            "source_test": session.request.source_test,
            "source_question": session.request.source_question,
            "question_key": replay.question_key if replay else None,
            "audio_sha256": replay.audio_sha256 if replay else None,
            "source": replay.source if replay else None,
            "preparation_ms": (
                round(session.preparation_ms, 3)
                if session.preparation_ms is not None else None
            ),
            "error": session.error,
        }

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
        self.journal.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
