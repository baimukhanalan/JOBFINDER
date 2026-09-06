import tempfile
import unittest
import io
import json
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from qa_bot.adapters.stt.natively_local import NativelyLocalSTT, main


class NativelyLocalSTTTests(unittest.TestCase):
    def test_command_uses_local_runtime_and_keeps_input_out_of_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "qa"
            app = Path(directory) / "Natively.app"
            electron = app / "Contents/MacOS/Natively"
            transformers = (app / "Contents/Resources/app.asar.unpacked/node_modules"
                            / "@huggingface/transformers")
            driver = root / "tools/natively_local_stt.mjs"
            wav = root / "answer.wav"
            for path in (electron, driver, wav):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
            transformers.mkdir(parents=True)
            service = NativelyLocalSTT(root, app_path=app)
            command = service.command(wav)
            self.assertEqual(command[0], str(electron))
            self.assertEqual(command[2], str(wav))
            self.assertNotIn("ELEVENLABS_API_KEY", " ".join(command))
            self.assertEqual(command[-1], "distil-whisper/distil-small.en")

    def test_missing_runtime_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            service = NativelyLocalSTT(Path(directory), app_path=Path(directory) / "missing")
            with self.assertRaises(FileNotFoundError):
                service.command(Path(directory) / "missing.wav")

    def test_cli_emits_sanitized_json(self):
        output = io.StringIO()
        with patch.object(NativelyLocalSTT, "transcribe_wav",
                          new=AsyncMock(return_value="Exact words.")):
            with redirect_stdout(output):
                code = main(["input.wav", "--project-root", "."])
        record = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(record["text"], "Exact words.")
        self.assertEqual(record["model"], "distil-whisper/distil-small.en")

    def test_silent_wav_fails_before_runtime_is_started(self):
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / "silence.wav"
            with wave.open(str(wav), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(bytes(32000))
            service = NativelyLocalSTT(Path(directory), app_path=Path(directory) / "missing")
            with self.assertRaisesRegex(ValueError, "silent"):
                import asyncio
                asyncio.run(service.transcribe_wav(wav))
