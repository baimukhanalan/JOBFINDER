"""Persistent exact-match speech answers with reusable MP3/WAV artifacts."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from qa_bot.audio.service import validate_mp3, wav_duration


def normalize_prompt(text: str) -> str:
    """Normalize representation without changing wording, punctuation, or case."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("question text required")
    return " ".join(unicodedata.normalize("NFC", text).split())


def prompt_fingerprint(text: str) -> tuple[str, str]:
    canonical = json.dumps(
        {"version": 1, "question_text": normalize_prompt(text)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest(), canonical


@dataclass(frozen=True)
class SpeechReplay:
    question_key: str
    question_text: str
    answer_text: str
    mp3_path: Path
    wav_path: Path
    audio_sha256: str
    wav_sha256: str
    source: str
    source_profile: str
    source_test: str
    source_question: str
    reuse_count: int


class MacOSAudioConverter:
    """Decode an MP3 to Chromium-compatible mono PCM16 16 kHz WAV."""

    def __init__(self, executable: str = "/usr/bin/afconvert"):
        self.executable = executable

    def mp3_to_wav(self, mp3_path: Path, wav_path: Path) -> None:
        if not Path(self.executable).is_file():
            raise ValueError("audio converter unavailable")
        process = subprocess.run(
            [self.executable, str(mp3_path), str(wav_path), "-f", "WAVE", "-d", "LEI16@16000", "-c", "1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if process.returncode != 0 or not wav_path.is_file():
            raise ValueError("MP3 to WAV conversion failed")
        wav_duration(wav_path.read_bytes())


class SpeechReplayBank:
    """Map an exact question to a reviewed answer and its persistent audio."""

    def __init__(self, database: str | Path, artifact_dir: str | Path, *, converter=None):
        self.artifact_dir = Path(artifact_dir).resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.converter = converter or MacOSAudioConverter()
        self.db = sqlite3.connect(database, timeout=30)
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS speech_answers(
                question_key TEXT PRIMARY KEY,
                canonical TEXT NOT NULL,
                question_text TEXT NOT NULL,
                answer_text TEXT NOT NULL,
                voice TEXT NOT NULL,
                model TEXT NOT NULL,
                mp3_path TEXT NOT NULL,
                wav_path TEXT NOT NULL,
                audio_sha256 TEXT NOT NULL,
                wav_sha256 TEXT NOT NULL,
                source_profile TEXT NOT NULL,
                source_test TEXT NOT NULL,
                source_question TEXT NOT NULL,
                reuse_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS speech_usage(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_key TEXT NOT NULL,
                source_profile TEXT NOT NULL,
                source_test TEXT NOT NULL,
                source_question TEXT NOT NULL,
                delivery TEXT NOT NULL CHECK(delivery IN ('generated','replay')),
                used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(question_key) REFERENCES speech_answers(question_key)
            );
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        self.db.close()

    def _paths(self, key: str) -> tuple[Path, Path]:
        directory = self.artifact_dir / key[:2]
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{key}.mp3", directory / f"{key}.wav"

    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _decode(self, row, *, source: str) -> SpeechReplay:
        mp3_path = (self.artifact_dir / row["mp3_path"]).resolve()
        wav_path = (self.artifact_dir / row["wav_path"]).resolve()
        if not mp3_path.is_relative_to(self.artifact_dir) or not wav_path.is_relative_to(self.artifact_dir):
            raise ValueError("speech artifact escapes bank directory")
        if (not mp3_path.is_file() or not wav_path.is_file()
                or self._sha(mp3_path) != row["audio_sha256"]
                or self._sha(wav_path) != row["wav_sha256"]):
            raise ValueError("speech artifact missing or changed")
        validate_mp3(mp3_path.read_bytes())
        wav_duration(wav_path.read_bytes())
        return SpeechReplay(
            row["question_key"], row["question_text"], row["answer_text"],
            mp3_path, wav_path, row["audio_sha256"], row["wav_sha256"], source,
            row["source_profile"], row["source_test"], row["source_question"],
            row["reuse_count"],
        )

    def lookup(self, question_text: str, answer_text: str | None = None) -> SpeechReplay | None:
        key, canonical = prompt_fingerprint(question_text)
        row = self.db.execute(
            "SELECT * FROM speech_answers WHERE question_key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["canonical"] != canonical:
            raise ValueError("speech question hash collision")
        if answer_text is not None and row["answer_text"] != normalize_prompt(answer_text):
            raise ValueError("exact question already has a different answer")
        return self._decode(row, source="replay")

    def record(
        self,
        question_text: str,
        answer_text: str,
        mp3: bytes,
        *,
        voice: str,
        model: str,
        source_profile: str,
        source_test: str,
        source_question: str,
    ) -> SpeechReplay:
        validate_mp3(mp3)
        for name, value in {
            "voice": voice,
            "model": model,
            "source_profile": source_profile,
            "source_test": source_test,
            "source_question": source_question,
        }.items():
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError(f"{name} required")
        key, canonical = prompt_fingerprint(question_text)
        question_text, answer_text = normalize_prompt(question_text), normalize_prompt(answer_text)
        found = self.lookup(question_text, answer_text)
        if found:
            return found
        mp3_path, wav_path = self._paths(key)
        with tempfile.NamedTemporaryFile(dir=mp3_path.parent, suffix=".mp3", delete=False) as stream:
            stream.write(mp3)
            staged_mp3 = Path(stream.name)
        staged_wav = Path(str(staged_mp3) + ".wav")
        try:
            self.converter.mp3_to_wav(staged_mp3, staged_wav)
            audio_sha, wav_sha = self._sha(staged_mp3), self._sha(staged_wav)
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                # Recheck under the writer lock: another process may have won.
                found = self.lookup(question_text, answer_text)
                if found:
                    return found
                os.replace(staged_mp3, mp3_path)
                os.replace(staged_wav, wav_path)
                self.db.execute(
                    """INSERT INTO speech_answers(
                        question_key,canonical,question_text,answer_text,voice,model,
                        mp3_path,wav_path,audio_sha256,wav_sha256,
                        source_profile,source_test,source_question
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        key, canonical, question_text, answer_text, voice, model,
                        str(mp3_path.relative_to(self.artifact_dir)),
                        str(wav_path.relative_to(self.artifact_dir)),
                        audio_sha, wav_sha, source_profile, source_test, source_question,
                    ),
                )
                self.db.execute(
                    """INSERT INTO speech_usage(
                        question_key,source_profile,source_test,source_question,delivery
                    ) VALUES(?,?,?,?, 'generated')""",
                    (key, source_profile, source_test, source_question),
                )
        finally:
            # Never delete a published artifact: a competing writer may own it.
            # An interrupted insertion leaves harmless unreferenced files only.
            staged_mp3.unlink(missing_ok=True)
            staged_wav.unlink(missing_ok=True)
        row = self.db.execute("SELECT * FROM speech_answers WHERE question_key=?", (key,)).fetchone()
        return self._decode(row, source="generated")

    def import_existing(
        self,
        question_text: str,
        answer_text: str,
        mp3_path: str | Path,
        *,
        source_profile: str,
        source_test: str,
        source_question: str,
    ) -> SpeechReplay:
        """Persist a captured prior answer without invoking TTS."""
        source_path = Path(mp3_path)
        if not source_path.is_file():
            raise FileNotFoundError("captured MP3 is missing")
        existing = self.lookup(question_text, answer_text)
        if existing:
            return existing
        replay = self.record(
            question_text,
            answer_text,
            source_path.read_bytes(),
            voice="captured-original",
            model="captured-original",
            source_profile=source_profile,
            source_test=source_test,
            source_question=source_question,
        )
        return replace(replay, source="captured")

    def replay(
        self,
        question_text: str,
        answer_text: str | None = None,
        *,
        source_profile: str,
        source_test: str,
        source_question: str,
    ) -> SpeechReplay | None:
        found = self.lookup(question_text, answer_text)
        if found is None:
            return None
        for name, value in {
            "source_profile": source_profile,
            "source_test": source_test,
            "source_question": source_question,
        }.items():
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError(f"{name} required")
        with self.db:
            self.db.execute(
                """UPDATE speech_answers
                   SET reuse_count=reuse_count+1,last_used_at=CURRENT_TIMESTAMP
                   WHERE question_key=?""",
                (found.question_key,),
            )
            self.db.execute(
                """INSERT INTO speech_usage(
                    question_key,source_profile,source_test,source_question,delivery
                ) VALUES(?,?,?,?, 'replay')""",
                (found.question_key, source_profile, source_test, source_question),
            )
        row = self.db.execute(
            "SELECT * FROM speech_answers WHERE question_key=?", (found.question_key,)
        ).fetchone()
        return self._decode(row, source="replay")

    @asynccontextmanager
    async def generation_lock(self, question_text: str, *, timeout: float = 60):
        """Coordinate generation across independent bridge processes, not just threads."""
        key, _ = prompt_fingerprint(question_text)
        locks = self.artifact_dir / ".locks"
        locks.mkdir(exist_ok=True)
        with (locks / (key + ".lock")).open("a+b") as stream:
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError("speech generation lock timed out")
                    await asyncio.sleep(.05)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    async def get_or_create(
        self, question_text: str, answer_text: str, *, speech, voice: str,
        model: str = "eleven_multilingual_v2", settings=None,
        source_profile: str, source_test: str, source_question: str,
        timeout: float = 30, replay_only: bool = False,
    ) -> SpeechReplay:
        async with self.generation_lock(question_text, timeout=timeout + 30):
            found = self.replay(
                question_text, answer_text, source_profile=source_profile,
                source_test=source_test, source_question=source_question,
            )
            if found:
                return found
            if replay_only:
                raise ValueError("exact speech answer missing from replay-only bank")
            mp3 = await speech.synthesize_mp3(
                normalize_prompt(answer_text), voice=voice, model=model,
                settings=settings, synthetic=True, timeout=timeout,
            )
            return self.record(
                question_text, answer_text, mp3, voice=voice, model=model,
                source_profile=source_profile, source_test=source_test,
                source_question=source_question,
            )

    async def get_or_create_spoken(
        self, question_text: str, *, answer_factory, speech, voice: str,
        model: str = "macos-say", settings=None,
        source_profile: str, source_test: str, source_question: str,
        timeout: float = 30, replay_only: bool = False,
    ) -> SpeechReplay:
        """One exact lookup, answer callback and synthesis across concurrent runs.

        The callback is invoked only by the process that owns the missing-question
        lock. Repeats skip both answer generation and synthesis entirely.
        """
        async with self.generation_lock(question_text, timeout=timeout + 60):
            found = self.replay(
                question_text, source_profile=source_profile,
                source_test=source_test, source_question=source_question,
            )
            if found:
                return found
            if replay_only:
                raise ValueError("exact speech answer missing from replay-only bank")
            answer_text = normalize_prompt(await answer_factory())
            mp3 = await speech.synthesize_mp3(
                answer_text, voice=voice, model=model,
                settings=settings, synthetic=True, timeout=timeout,
            )
            return self.record(
                question_text, answer_text, mp3, voice=voice, model=model,
                source_profile=source_profile, source_test=source_test,
                source_question=source_question,
            )

    def stats(self) -> dict[str, int]:
        row = self.db.execute(
            "SELECT count(*) AS answers,coalesce(sum(reuse_count),0) AS reuses FROM speech_answers"
        ).fetchone()
        return {"answers": row["answers"], "reuses": row["reuses"]}

    def usage(self, question_text: str | None = None) -> tuple[dict, ...]:
        if question_text is None:
            rows = self.db.execute(
                """SELECT source_profile,source_test,source_question,delivery,used_at
                   FROM speech_usage ORDER BY id"""
            ).fetchall()
        else:
            key, _ = prompt_fingerprint(question_text)
            rows = self.db.execute(
                """SELECT source_profile,source_test,source_question,delivery,used_at
                   FROM speech_usage WHERE question_key=? ORDER BY id""",
                (key,),
            ).fetchall()
        return tuple(dict(row) for row in rows)


def read_aloud_answer(question) -> str:
    """SVAR read-aloud tasks are deterministic: speak the displayed sentence verbatim."""
    if question.type_id != "SVAR-01" or question.response_contract.kind.value != "audio":
        raise ValueError("not a supported read-aloud question")
    return normalize_prompt(question.question_text)
