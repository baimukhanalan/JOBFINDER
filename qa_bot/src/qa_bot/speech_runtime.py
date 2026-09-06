"""Create or replay one exact speech answer and stage the browser microphone file."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from qa_bot.adapters.stt.elevenlabs import ElevenLabsProvider
from qa_bot.adapters.stt.transport import ElevenLabsHTTP
from qa_bot.adapters.tts.macos_local import MacOSLocalTTS
from qa_bot.audio.fake_microphone import ChromiumFakeMicrophone
from qa_bot.audio.replay_bank import SpeechReplayBank, normalize_prompt
from qa_bot.audio.service import SpeechService


async def prepare(args):
    answer = args.answer if args.answer is not None else args.question
    microphone = ChromiumFakeMicrophone(args.microphone_wav)
    with SpeechReplayBank(args.database, args.artifacts) as bank:
        cached = bank.replay(
            args.question, answer, source_profile=args.source_profile,
            source_test=args.source_test, source_question=args.source_question,
        )
        if cached:
            path = microphone.stage(cached.wav_path.read_bytes())
            return cached, path, 0, 0, bank.stats()

        if args.tts_provider == "local":
            voice = args.voice or "Samantha"
            speech = MacOSLocalTTS(allowed_voices=(voice,))
            result = await bank.get_or_create(
                args.question, answer, speech=speech, voice=voice,
                model="macos-say", settings=None,
                source_profile=args.source_profile, source_test=args.source_test,
                source_question=args.source_question, timeout=args.timeout,
            )
            path = microphone.stage(result.wav_path.read_bytes())
            return result, path, 0, speech.generation_requests, bank.stats()

        key = os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            raise ValueError("ELEVENLABS_API_KEY is required for the first occurrence")
        client = ElevenLabsHTTP(enabled=True, max_requests=1, api_key=key)
        try:
            preflight = await client.preflight(timeout=min(args.timeout, 20))
            if args.voice:
                if args.voice not in preflight["voices"]:
                    raise ValueError("voice is not available on the verified free account")
                voice = args.voice
            elif preflight["voices"]:
                voice = preflight["voices"][0]
            else:
                raise ValueError("no verified free voice available")
            speech = SpeechService(
                ElevenLabsProvider(client), args.speech_cache,
                allowed_voices=(voice,), retries=0,
            )
            try:
                result = await bank.get_or_create(
                    args.question, answer, speech=speech, voice=voice,
                    source_profile=args.source_profile, source_test=args.source_test,
                    source_question=args.source_question, timeout=args.timeout,
                )
            finally:
                speech.close()
            path = microphone.stage(result.wav_path.read_bytes())
            return result, path, 1, 0, bank.stats()
        finally:
            client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare an exact speech answer for Chromium")
    parser.add_argument("--question", required=True)
    parser.add_argument("--answer")
    parser.add_argument("--source-profile", required=True)
    parser.add_argument("--source-test", required=True)
    parser.add_argument("--source-question", required=True)
    parser.add_argument("--voice")
    parser.add_argument("--tts-provider", choices=("local", "elevenlabs"), default="local")
    parser.add_argument("--database", type=Path, default=Path("runs/speech/answers.sqlite3"))
    parser.add_argument("--speech-cache", type=Path, default=Path("runs/speech/provider-cache.sqlite3"))
    parser.add_argument("--artifacts", type=Path, default=Path("runs/speech/answers"))
    parser.add_argument("--microphone-wav", type=Path, default=Path("runs/speech/browser-input.wav"))
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        for value in (args.question, args.answer or args.question, args.source_profile,
                      args.source_test, args.source_question):
            normalize_prompt(value)
        if not 0 < args.timeout <= 60:
            raise ValueError
        result, path, provider_calls, local_calls, stats = asyncio.run(prepare(args))
        report = {
            "status": "speech_ready",
            "source": result.source,
            "question_key": result.question_key,
            "mp3_path": str(result.mp3_path),
            "microphone_wav": str(path),
            "audio_sha256": result.audio_sha256,
            "provider_generation_requests": provider_calls,
            "local_generation_requests": local_calls,
            "reuse_count": stats["reuses"],
        }
        print(json.dumps(report, indent=2))
        return 0
    except Exception:
        print(json.dumps({"status": "speech_prepare_failed", "secrets_logged": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
