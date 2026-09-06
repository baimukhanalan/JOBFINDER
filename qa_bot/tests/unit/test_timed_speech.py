import asyncio
import io
import json
import tempfile
import time
import unittest
import wave
from pathlib import Path

from qa_bot.audio.replay_bank import SpeechReplayBank
from qa_bot.orchestration.timed_speech import (
    TimedSpeechPhase,
    TimedSpeechPipeline,
    TimedSpeechPolicy,
    TimedSpeechRequest,
)


def tone_wav():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 1600)
    return stream.getvalue()


class FakeConverter:
    def mp3_to_wav(self, _mp3_path, wav_path):
        Path(wav_path).write_bytes(tone_wav())


class FakeBank(SpeechReplayBank):
    def __init__(self, database, artifacts):
        super().__init__(database, artifacts, converter=FakeConverter())


class SlowSpeech:
    generation_requests = 0

    async def synthesize_mp3(self, *_args, **_kwargs):
        type(self).generation_requests += 1
        await asyncio.sleep(0.18)
        return b"ID3" + bytes(200)


class ArmedSink:
    def __init__(self):
        self.armed = []
        self.played = []

    def arm(self, session_id, path):
        self.armed.append((session_id, Path(path)))

    def play(self, session_id):
        if session_id not in [value[0] for value in self.armed]:
            raise AssertionError("play occurred before arm")
        self.played.append(session_id)
        return "browser-audio-buffer-1"


def request(profile="profile-1", question="Read this sentence."):
    return TimedSpeechRequest(
        question=question,
        answer="The prepared answer is spoken immediately.",
        source_profile=profile,
        source_test="TP-015",
        source_question="SVAR-READ-001",
    )


class TimedSpeechPipelineTests(unittest.TestCase):
    def setUp(self):
        SlowSpeech.generation_requests = 0

    def pipeline(self, root, sink):
        return TimedSpeechPipeline(
            root / "speech.sqlite3",
            root / "audio",
            root / "transitions.jsonl",
            speech_factory=SlowSpeech,
            bank_factory=FakeBank,
            sink=sink,
            policy=TimedSpeechPolicy(
                generation_timeout_seconds=2,
                maximum_start_latency_ms=100,
            ),
        )

    def test_prepare_returns_immediately_while_generation_runs_in_background(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.pipeline(root, ArmedSink()) as pipeline:
                started = time.monotonic()
                session_id = pipeline.prepare(request())
                returned_ms = (time.monotonic() - started) * 1000
                self.assertLess(returned_ms, 50)
                self.assertEqual(pipeline.snapshot(session_id).phase,
                                 TimedSpeechPhase.PREPARING)
                result = pipeline.wait_until_armed(session_id, 1)
                self.assertEqual(result.phase, TimedSpeechPhase.ARMED)
                self.assertGreaterEqual(result.preparation_ms, 150)

    def test_speak_now_only_triggers_prearmed_audio_and_journals_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sink = ArmedSink()
            with self.pipeline(root, sink) as pipeline:
                session_id = pipeline.prepare(request())
                self.assertEqual(pipeline.wait_until_armed(session_id, 1).phase,
                                 TimedSpeechPhase.ARMED)
                started = time.monotonic()
                result = pipeline.speak_now(session_id)
                elapsed_ms = (time.monotonic() - started) * 1000
                self.assertLess(elapsed_ms, 50)
                self.assertEqual(result.phase, TimedSpeechPhase.PLAYING)
                self.assertEqual(result.player_id, "browser-audio-buffer-1")
                self.assertEqual(sink.played, [session_id])
            events = [json.loads(line) for line in
                      (root / "transitions.jsonl").read_text().splitlines()]
            self.assertEqual(
                [event["transition"] for event in events],
                ["listen_carefully", "audio_armed", "speak_now_started"],
            )
            self.assertIn("start_latency_ms", events[-1])
            self.assertTrue(events[-1]["audio_sha256"])

    def test_exact_repeat_arms_without_calling_speech_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with FakeBank(root / "speech.sqlite3", root / "audio") as bank:
                bank.record(
                    request().question,
                    request().answer,
                    b"ID3" + bytes(200),
                    voice="Samantha",
                    model="macos-say",
                    source_profile="earlier-profile",
                    source_test="TP-001",
                    source_question="q-1",
                )

            def provider_must_not_exist():
                raise AssertionError("replay must not initialize TTS")

            sink = ArmedSink()
            with TimedSpeechPipeline(
                root / "speech.sqlite3", root / "audio", root / "events.jsonl",
                speech_factory=provider_must_not_exist,
                bank_factory=FakeBank,
                sink=sink,
            ) as pipeline:
                session_id = pipeline.prepare(request(profile="profile-2"))
                armed = pipeline.wait_until_armed(session_id, 1)
                self.assertEqual(armed.phase, TimedSpeechPhase.ARMED)
                self.assertEqual(armed.source, "replay")
                pipeline.speak_now(session_id)

    def test_speak_now_refuses_to_wait_for_unfinished_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.pipeline(root, ArmedSink()) as pipeline:
                session_id = pipeline.prepare(request())
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "not armed"):
                    pipeline.speak_now(session_id)
                self.assertLess((time.monotonic() - started) * 1000, 50)
                self.assertEqual(pipeline.wait_until_armed(session_id, 1).phase,
                                 TimedSpeechPhase.ARMED)

    def test_same_source_question_is_idempotent_but_changed_payload_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.pipeline(root, ArmedSink()) as pipeline:
                first = pipeline.prepare(request())
                second = pipeline.prepare(request())
                self.assertEqual(first, second)
                changed = TimedSpeechRequest(
                    question="Changed", answer=request().answer,
                    source_profile=request().source_profile,
                    source_test=request().source_test,
                    source_question=request().source_question,
                )
                with self.assertRaisesRegex(ValueError, "collision"):
                    pipeline.prepare(changed)
