import tempfile
import unittest
from pathlib import Path

from qa_bot.adapters.tts.macos_local import MacOSLocalTTS
from qa_bot.audio.replay_bank import SpeechReplayBank
from qa_bot.audio.service import validate_mp3, wav_signal_metrics


class MacOSLocalTTSTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_local_generation_then_provider_free_exact_replay(self):
        speech = MacOSLocalTTS(allowed_voices=("Samantha",))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with SpeechReplayBank(root / "speech.sqlite3", root / "audio") as bank:
                args = dict(
                    speech=speech,
                    voice="Samantha",
                    model="macos-say",
                    settings=None,
                    source_profile="TP-015-profile",
                    source_test="TP-015",
                    source_question="SVAR-READ-001",
                )
                first = await bank.get_or_create(
                    "Read the sentence.", "The local speech path is ready.", **args
                )
                second = await bank.get_or_create(
                    "Read the sentence.", "The local speech path is ready.", **args
                )
                validate_mp3(first.mp3_path.read_bytes())
                metrics = wav_signal_metrics(first.wav_path.read_bytes())
                self.assertGreater(metrics["rms"], 0.01)
                self.assertEqual(first.source, "generated")
                self.assertEqual(second.source, "replay")
                self.assertEqual(speech.generation_requests, 1)
                self.assertEqual(first.audio_sha256, second.audio_sha256)

    async def test_rejects_non_local_model_and_unapproved_voice(self):
        speech = MacOSLocalTTS(allowed_voices=("Samantha",))
        with self.assertRaises(ValueError):
            await speech.synthesize_mp3(
                "Hello", voice="Alex", model="macos-say", synthetic=True
            )
        with self.assertRaises(ValueError):
            await speech.synthesize_mp3(
                "Hello", voice="Samantha", model="eleven_multilingual_v2", synthetic=True
            )
