"""Stage a verified WAV for Chromium's test microphone input."""
import os
import tempfile
from pathlib import Path

from qa_bot.audio.service import wav_duration


class ChromiumFakeMicrophone:
    def __init__(self, wav_path: str | Path):
        self.wav_path = Path(wav_path).resolve()
        self.wav_path.parent.mkdir(parents=True, exist_ok=True)

    def stage(self, audio: bytes) -> Path:
        wav_duration(audio)
        with tempfile.NamedTemporaryFile(dir=self.wav_path.parent, suffix=".wav", delete=False) as stream:
            stream.write(audio)
            staged = Path(stream.name)
        os.replace(staged, self.wav_path)
        return self.wav_path

    def launch_args(self) -> tuple[str, ...]:
        if not self.wav_path.is_file():
            raise ValueError("stage audio before launching Chromium")
        return (
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={self.wav_path}",
        )
