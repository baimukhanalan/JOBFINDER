"""Prepare a read-aloud answer and stage it for an authorized browser run."""
import inspect
from dataclasses import dataclass
from pathlib import Path

from qa_bot.audio.replay_bank import SpeechReplay, normalize_prompt, read_aloud_answer


@dataclass(frozen=True)
class PreparedSpeechAnswer:
    replay: SpeechReplay
    microphone_path: Path
    answer_text: str = ""
    transcript: str | None = None
    strategy: str = "read_aloud"


class SpeechAnswerFlow:
    def __init__(self, replay_bank, speech, microphone, *, voice: str,
                 model: str = "eleven_multilingual_v2", settings=None):
        self.replay_bank = replay_bank
        self.speech = speech
        self.microphone = microphone
        self.voice = voice
        self.model = model
        self.settings = settings

    async def prepare_read_aloud(
        self,
        question,
        *,
        source_profile: str,
        source_test: str,
        source_question: str,
        timeout: float = 30,
    ) -> PreparedSpeechAnswer:
        answer_text = read_aloud_answer(question)
        replay = await self.replay_bank.get_or_create(
            question.question_text,
            answer_text,
            speech=self.speech,
            voice=self.voice,
            model=self.model,
            settings=self.settings,
            source_profile=source_profile,
            source_test=source_test,
            source_question=source_question,
            timeout=timeout,
        )
        path = self.microphone.stage(replay.wav_path.read_bytes())
        return PreparedSpeechAnswer(replay, path, answer_text, None, "read_aloud")

    async def prepare_listen_repeat(
        self,
        prompt_mp3: Path,
        prompt_wav: Path,
        *,
        local_stt,
        source_profile: str,
        source_test: str,
        source_question: str,
        timeout: float = 120,
    ) -> PreparedSpeechAnswer:
        """Replay the original prompt bytes; STT is metadata, never the audio source."""
        transcript = normalize_prompt(await local_stt.transcribe_wav(prompt_wav, timeout=timeout))
        replay = self.replay_bank.import_existing(
            transcript, transcript, prompt_mp3,
            source_profile=source_profile, source_test=source_test,
            source_question=source_question,
        )
        path = self.microphone.stage(replay.wav_path.read_bytes())
        return PreparedSpeechAnswer(replay, path, transcript, transcript, "exact_audio_repeat")

    async def prepare_spoken_answer(
        self,
        question_text: str,
        *,
        answer_solver,
        source_profile: str,
        source_test: str,
        source_question: str,
        prompt_wav: Path | None = None,
        local_stt=None,
        timeout: float = 120,
    ) -> PreparedSpeechAnswer:
        """Transcribe optional prompt, solve for text, then use exact replay/TTS bank."""
        question_text = normalize_prompt(question_text)
        transcript = None
        if prompt_wav is not None:
            if local_stt is None:
                raise ValueError("local STT required for an audio prompt")
            transcript = normalize_prompt(
                await local_stt.transcribe_wav(prompt_wav, timeout=timeout)
            )
        solved = answer_solver(question_text, transcript)
        if inspect.isawaitable(solved):
            solved = await solved
        answer_text = normalize_prompt(solved)
        prompt_key = question_text if transcript is None else (
            question_text + " [AUDIO TRANSCRIPT] " + transcript
        )
        replay = await self.replay_bank.get_or_create(
            prompt_key, answer_text, speech=self.speech, voice=self.voice,
            model=self.model, settings=self.settings, source_profile=source_profile,
            source_test=source_test, source_question=source_question, timeout=timeout,
        )
        path = self.microphone.stage(replay.wav_path.read_bytes())
        return PreparedSpeechAnswer(
            replay, path, answer_text, transcript,
            "listen_answer" if transcript is not None else "speak_topic",
        )
