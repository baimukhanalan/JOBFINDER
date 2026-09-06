import io
import json
import math
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock

from qa_bot.audio.fake_microphone import ChromiumFakeMicrophone
from qa_bot.audio.replay_bank import SpeechReplayBank, normalize_prompt, read_aloud_answer
from qa_bot.audio.service import SpeechService, wav_duration
from qa_bot.domain.observation import BrowserSnapshot
from qa_bot.perception.extractor import QuestionExtractor
from qa_bot.orchestration.speech_flow import SpeechAnswerFlow
from qa_bot.speech_runtime import main as speech_runtime_main


def tone_wav(seconds=1):
    frames = bytearray()
    for index in range(16000 * seconds):
        sample = int(4000 * math.sin(2 * math.pi * 440 * index / 16000))
        frames.extend(sample.to_bytes(2, "little", signed=True))
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(frames)
    return stream.getvalue()


class FakeConverter:
    def __init__(self):
        self.calls = 0

    def mp3_to_wav(self, mp3_path, wav_path):
        self.calls += 1
        self.last_mp3 = Path(mp3_path).read_bytes()
        Path(wav_path).write_bytes(tone_wav())


class SpeechReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_question_generates_then_exact_repeat_replays(self):
        provider = AsyncMock()
        provider.synthesize_mp3.return_value = b"ID3" + bytes(200)
        speech = SpeechService(provider, ":memory:", allowed_voices=("voice",))
        converter = FakeConverter()
        try:
            with tempfile.TemporaryDirectory() as directory:
                with SpeechReplayBank(
                    Path(directory) / "speech.sqlite3", Path(directory) / "audio",
                    converter=converter,
                ) as bank:
                    args = dict(
                        speech=speech, voice="voice", source_profile="profile-01",
                        source_test="TP-001", source_question="TP-001-SVAR-READ-001",
                    )
                    first = await bank.get_or_create("Read this sentence.", "Read this sentence.", **args)
                    second = await bank.get_or_create("Read this sentence.", "Read this sentence.", **args)
                    self.assertEqual(first.source, "generated")
                    self.assertEqual(second.source, "replay")
                    self.assertEqual(first.mp3_path, second.mp3_path)
                    self.assertEqual(first.wav_path, second.wav_path)
                    self.assertEqual(provider.synthesize_mp3.await_count, 1)
                    self.assertEqual(converter.calls, 1)
                    self.assertEqual(bank.stats(), {"answers": 1, "reuses": 1})
                    self.assertEqual(second.source_profile, "profile-01")
                    self.assertEqual(second.source_test, "TP-001")
                    self.assertEqual(
                        [(row["source_profile"], row["delivery"]) for row in bank.usage("Read this sentence.")],
                        [("profile-01", "generated"), ("profile-01", "replay")],
                    )
        finally:
            speech.close()

    async def test_changed_wording_is_not_an_exact_hit(self):
        provider = AsyncMock()
        provider.synthesize_mp3.return_value = b"ID3" + bytes(200)
        speech = SpeechService(provider, ":memory:", allowed_voices=("voice",))
        try:
            with tempfile.TemporaryDirectory() as directory:
                with SpeechReplayBank(
                    Path(directory) / "speech.sqlite3", Path(directory) / "audio",
                    converter=FakeConverter(),
                ) as bank:
                    args = dict(
                        speech=speech, voice="voice", source_profile="p",
                        source_test="t", source_question="q",
                    )
                    await bank.get_or_create("The world is small.", "The world is small.", **args)
                    await bank.get_or_create("The world is such a small place.",
                                             "The world is such a small place.", **args)
                    self.assertEqual(provider.synthesize_mp3.await_count, 2)
                    self.assertEqual(bank.stats()["answers"], 2)
        finally:
            speech.close()

    async def test_answer_conflict_and_changed_artifact_stop_replay(self):
        provider = AsyncMock()
        provider.synthesize_mp3.return_value = b"ID3" + bytes(200)
        speech = SpeechService(provider, ":memory:", allowed_voices=("voice",))
        try:
            with tempfile.TemporaryDirectory() as directory:
                with SpeechReplayBank(
                    Path(directory) / "speech.sqlite3", Path(directory) / "audio",
                    converter=FakeConverter(),
                ) as bank:
                    args = dict(
                        speech=speech, voice="voice", source_profile="p",
                        source_test="t", source_question="q",
                    )
                    result = await bank.get_or_create("Question", "Answer one", **args)
                    with self.assertRaises(ValueError):
                        await bank.get_or_create("Question", "Answer two", **args)
                    result.mp3_path.write_bytes(b"ID3" + bytes(201))
                    with self.assertRaises(ValueError):
                        bank.lookup("Question", "Answer one")
        finally:
            speech.close()

    def test_read_aloud_and_fake_microphone(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures/questions/SVAR-01.html"
        question = QuestionExtractor().extract(BrowserSnapshot(
            "obs", "http://127.0.0.1/SVAR-01.html",
            question_html=fixture.read_text(encoding="utf-8"),
        )).spec
        self.assertEqual(read_aloud_answer(question), normalize_prompt(question.question_text))
        with tempfile.TemporaryDirectory() as directory:
            microphone = ChromiumFakeMicrophone(Path(directory) / "answer.wav")
            path = microphone.stage(tone_wav())
            self.assertEqual(wav_duration(path.read_bytes()), 1)
            self.assertIn(str(path), microphone.launch_args()[-1])

    async def test_autonomous_read_aloud_flow_reuses_previous_audio(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures/questions/SVAR-01.html"
        question = QuestionExtractor().extract(BrowserSnapshot(
            "obs", "http://127.0.0.1/SVAR-01.html",
            question_html=fixture.read_text(encoding="utf-8"),
        )).spec
        provider = AsyncMock()
        provider.synthesize_mp3.return_value = b"ID3" + bytes(200)
        speech = SpeechService(provider, ":memory:", allowed_voices=("voice",))
        try:
            with tempfile.TemporaryDirectory() as directory:
                microphone = ChromiumFakeMicrophone(Path(directory) / "browser-input.wav")
                with SpeechReplayBank(
                    Path(directory) / "speech.sqlite3", Path(directory) / "answers",
                    converter=FakeConverter(),
                ) as bank:
                    flow = SpeechAnswerFlow(bank, speech, microphone, voice="voice")
                    first = await flow.prepare_read_aloud(
                        question, source_profile="profile-01", source_test="TP-001",
                        source_question="TP-001-SVAR-READ-001",
                    )
                    second = await flow.prepare_read_aloud(
                        question, source_profile="profile-02", source_test="TP-002",
                        source_question="TP-002-SVAR-READ-001",
                    )
                    self.assertEqual(first.replay.source, "generated")
                    self.assertEqual(second.replay.source, "replay")
                    self.assertEqual(first.microphone_path, second.microphone_path)
                    self.assertEqual(provider.synthesize_mp3.await_count, 1)
                    self.assertEqual(bank.stats(), {"answers": 1, "reuses": 1})
                    self.assertEqual(
                        [(row["source_profile"], row["source_test"]) for row in bank.usage()],
                        [("profile-01", "TP-001"), ("profile-02", "TP-002")],
                    )
        finally:
            speech.close()

    async def test_listen_repeat_uses_original_audio_without_tts(self):
        provider = AsyncMock()
        speech = SpeechService(provider, ":memory:", allowed_voices=("voice",))
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prompt_mp3 = root / "prompt.mp3"
                prompt_mp3.write_bytes(b"ID3" + bytes(200))
                prompt_wav = root / "prompt.wav"
                prompt_wav.write_bytes(tone_wav())
                local_stt = AsyncMock()
                local_stt.transcribe_wav.return_value = "Repeat these exact words."
                microphone = ChromiumFakeMicrophone(root / "microphone.wav")
                with SpeechReplayBank(root / "speech.sqlite3", root / "answers",
                                      converter=FakeConverter()) as bank:
                    flow = SpeechAnswerFlow(bank, speech, microphone, voice="voice")
                    result = await flow.prepare_listen_repeat(
                        prompt_mp3, prompt_wav, local_stt=local_stt,
                        source_profile="profile", source_test="TP-015",
                        source_question="voice-1",
                    )
                    self.assertEqual(result.strategy, "exact_audio_repeat")
                    self.assertEqual(result.replay.mp3_path.read_bytes(), prompt_mp3.read_bytes())
                    self.assertEqual(result.transcript, "Repeat these exact words.")
                    provider.synthesize_mp3.assert_not_called()
        finally:
            speech.close()

    async def test_listen_answer_transcribes_solves_then_replays_on_repeat(self):
        provider = AsyncMock()
        provider.synthesize_mp3.return_value = b"ID3" + bytes(200)
        speech = SpeechService(provider, ":memory:", allowed_voices=("voice",))
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prompt_wav = root / "prompt.wav"
                prompt_wav.write_bytes(tone_wav())
                local_stt = AsyncMock()
                local_stt.transcribe_wav.return_value = "What should the customer do?"
                solver = AsyncMock(return_value="The customer should contact support.")
                microphone = ChromiumFakeMicrophone(root / "microphone.wav")
                with SpeechReplayBank(root / "speech.sqlite3", root / "answers",
                                      converter=FakeConverter()) as bank:
                    flow = SpeechAnswerFlow(bank, speech, microphone, voice="voice")
                    args = dict(
                        answer_solver=solver, prompt_wav=prompt_wav, local_stt=local_stt,
                        source_profile="profile", source_test="TP-015",
                        source_question="voice-2",
                    )
                    first = await flow.prepare_spoken_answer("Answer the question.", **args)
                    solver.side_effect = AssertionError("cached answer must bypass solver")
                    second = await flow.prepare_spoken_answer("Answer the question.", **args)
                    self.assertEqual(first.strategy, "listen_answer")
                    self.assertEqual(first.answer_text, "The customer should contact support.")
                    self.assertEqual(second.replay.source, "replay")
                    self.assertEqual(provider.synthesize_mp3.await_count, 1)
                    solver.assert_awaited_once_with(
                        "Answer the question.", "What should the customer do?"
                    )
                    self.assertEqual(first.replay.audio_sha256, second.replay.audio_sha256)
                    self.assertEqual(first.answer_text, second.answer_text)
                    self.assertEqual(bank.stats(), {"answers": 1, "reuses": 1})
        finally:
            speech.close()

    async def test_spoken_topic_replays_after_restart_without_solver_or_tts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, artifacts = root / "speech.sqlite3", root / "answers"
            with SpeechReplayBank(database, artifacts, converter=FakeConverter()) as bank:
                first = bank.record(
                    "Describe good customer service.", "Listen carefully and resolve the problem.",
                    b"ID3" + bytes(200), voice="voice", model="model",
                    source_profile="profile-01", source_test="TP-001", source_question="q1",
                )
            with SpeechReplayBank(database, artifacts, converter=FakeConverter()) as bank:
                flow = SpeechAnswerFlow(
                    bank, None, ChromiumFakeMicrophone(root / "mic.wav"), voice="voice",
                )
                second = await flow.prepare_spoken_answer(
                    "Describe good customer service.", answer_solver=None,
                    source_profile="profile-02", source_test="TP-002", source_question="q2",
                )
                self.assertEqual(second.replay.source, "replay")
                self.assertEqual(second.answer_text, first.answer_text)
                self.assertEqual(second.replay.mp3_path.read_bytes(), first.mp3_path.read_bytes())
                self.assertEqual(second.microphone_path.read_bytes(), first.wav_path.read_bytes())
                self.assertEqual(bank.usage()[-1]["source_test"], "TP-002")
                self.assertEqual(bank.stats(), {"answers": 1, "reuses": 1})

    async def test_changed_listening_transcript_requires_new_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.wav"
            prompt.write_bytes(tone_wav())
            speech = AsyncMock()
            speech.synthesize_mp3.return_value = b"ID3" + bytes(200)
            stt = AsyncMock()
            stt.transcribe_wav.side_effect = ["When is delivery?", "Where is delivery?"]
            solver = AsyncMock(side_effect=["Delivery is tomorrow.", "Delivery is at reception."])
            with SpeechReplayBank(root / "bank.sqlite3", root / "audio",
                                  converter=FakeConverter()) as bank:
                flow = SpeechAnswerFlow(
                    bank, speech, ChromiumFakeMicrophone(root / "mic.wav"), voice="voice",
                )
                args = dict(answer_solver=solver, local_stt=stt, prompt_wav=prompt,
                            source_profile="p", source_test="t", source_question="q")
                first = await flow.prepare_spoken_answer("Answer the question.", **args)
                second = await flow.prepare_spoken_answer("Answer the question.", **args)
                self.assertNotEqual(first.replay.question_key, second.replay.question_key)
                self.assertEqual(second.answer_text, "Delivery is at reception.")
                self.assertEqual(solver.await_count, 2)
                self.assertEqual(speech.synthesize_mp3.await_count, 2)

    async def test_corrupt_spoken_cache_stops_before_solver_and_microphone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            solver, speech = AsyncMock(), AsyncMock()
            with SpeechReplayBank(root / "bank.sqlite3", root / "audio",
                                  converter=FakeConverter()) as bank:
                cached = bank.record(
                    "Describe customer service.", "Listen carefully to the customer.",
                    b"ID3" + bytes(200), voice="voice", model="model",
                    source_profile="p", source_test="t", source_question="q",
                )
                cached.mp3_path.write_bytes(b"ID3" + bytes(201))
                flow = SpeechAnswerFlow(
                    bank, speech, ChromiumFakeMicrophone(root / "mic.wav"), voice="voice",
                )
                with self.assertRaisesRegex(ValueError, "artifact missing or changed"):
                    await flow.prepare_spoken_answer(
                        "Describe customer service.", answer_solver=solver,
                        source_profile="p2", source_test="t2", source_question="q2",
                    )
                solver.assert_not_called()
                speech.synthesize_mp3.assert_not_called()
                self.assertFalse((root / "mic.wav").exists())

    def test_runtime_replay_needs_no_provider_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, artifacts = root / "speech.sqlite3", root / "answers"
            with SpeechReplayBank(database, artifacts, converter=FakeConverter()) as bank:
                bank.record(
                    "Exact question", "Exact answer", b"ID3" + bytes(200),
                    voice="voice", model="model", source_profile="profile-01",
                    source_test="TP-001", source_question="q-1",
                )
            output = io.StringIO()
            with redirect_stdout(output):
                code = speech_runtime_main([
                    "--question", "Exact question", "--answer", "Exact answer",
                    "--source-profile", "profile-02", "--source-test", "TP-002",
                    "--source-question", "q-2", "--database", str(database),
                    "--artifacts", str(artifacts), "--microphone-wav", str(root / "mic.wav"),
                ])
            report = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(report["source"], "replay")
            self.assertEqual(report["provider_generation_requests"], 0)
            self.assertEqual(report["local_generation_requests"], 0)
            self.assertTrue((root / "mic.wav").is_file())

    def test_imported_capture_is_reused_byte_for_byte_without_tts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = root / "captured.mp3"
            original = b"ID3" + bytes(range(200))
            captured.write_bytes(original)
            converter = FakeConverter()
            with SpeechReplayBank(root / "speech.sqlite3", root / "answers",
                                  converter=converter) as bank:
                first = bank.import_existing(
                    "Exact spoken prompt", "Exact spoken answer", captured,
                    source_profile="profile-01", source_test="TP-001",
                    source_question="q-voice-1",
                )
                second = bank.replay(
                    "Exact spoken prompt", "Exact spoken answer",
                    source_profile="profile-02", source_test="TP-002",
                    source_question="q-voice-2",
                )
                self.assertEqual(first.source, "captured")
                self.assertEqual(first.mp3_path.read_bytes(), original)
                self.assertEqual(second.mp3_path.read_bytes(), original)
                self.assertEqual(second.source, "replay")
                self.assertEqual(converter.calls, 1)
                self.assertEqual(bank.stats(), {"answers": 1, "reuses": 1})
