"""Bounded, opt-in diagnostics. No assessment answer actions or persistent secrets."""
import argparse
import asyncio
import getpass
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4

from qa_bot.domain.screen import ScreenType
from qa_bot.domain.state import RunState
from qa_bot.orchestration.observe import observe_once
from qa_bot.reporting.observation_log import state_event


def validate_budget(seconds):
    if type(seconds) is not int or seconds < 1:
        raise ValueError("diagnostic budget must be a positive number of seconds")
    return seconds


def read_secrets(stream):
    try:
        line = stream.readline(16385)
        if len(line) > 16384:
            raise ValueError
        data = json.loads(line)
        if not isinstance(data, dict) or set(data) - {"url", "api_key"}:
            raise ValueError
        if any(not isinstance(v, str) or not v.strip() or len(v) > 8192 for v in data.values()):
            raise ValueError
        return data
    except Exception:
        raise ValueError("invalid secret input; content not logged") from None


async def inspect_page(url, origins, screenshot_path=None):
    try:
        async with asyncio.timeout(60):
            state = await observe_once(url, origins, RunState(str(uuid4())), timeout=20,
                                       route=True, wait_for_content=True, screenshot_path=screenshot_path)
        report = state_event(state)
        body = state.observation.dom_text.casefold() if state.observation else ""
        consent = "data protection notice" in body or "privacy notice" in body
        authentication = state.observation and any(e.input_type == "password" for e in state.observation.elements)
        if authentication:
            status = "blocked_authentication"
        elif consent:
            status = "blocked_consent"
        elif state.screen_type == ScreenType.UNKNOWN or state.extraction_errors:
            status = "blocked_unknown_format"
        else:
            status = "observation_ok"
        return {**report, "status": status, "assessment_passed": False,
                "consent_required": consent, "answers_sent": 0}
    except Exception:
        return {"status": "observation_failed", "actions_executed": 0,
                "assessment_passed": False, "answers_sent": 0}


async def inspect_speech(key, *, generate=False):
    from qa_bot.adapters.stt.transport import ElevenLabsHTTP, ProviderError
    from qa_bot.adapters.stt.elevenlabs import ElevenLabsProvider
    from qa_bot.audio.service import SpeechService, SpeechReviewRequired, wav_duration
    client = ElevenLabsHTTP(enabled=True, max_requests=2, api_key=key)
    report = {"status": "preflight_failed", "generation_attempts": 0}
    try:
        preflight = await client.preflight(timeout=15)
        report.update({**preflight, "status": "preflight_ok"})
        if not generate:
            return report
        if not preflight["voices"]:
            return {**report, "status": "blocked_no_verified_free_voice"}
        voice = preflight["voices"][0]
        service = SpeechService(ElevenLabsProvider(client), ":memory:", allowed_voices=(voice,), retries=0)
        try:
            text = "This recording is a synthetic quality check in English. Please listen carefully and verify that the spoken words match the original sentence."
            report["generation_attempts"] = 1
            audio = await service.synthesize(text, voice=voice, synthetic=True, timeout=40)
            report.update(tts_ok=True, duration_seconds=wav_duration(audio), voice_id=voice)
            report["generation_attempts"] = 2
            transcript = await service.transcribe(audio, language="en", synthetic=True, timeout=40)
            matches = re.findall(r"\w+", transcript.text.casefold()) == re.findall(r"\w+", text.casefold())
            report.update(stt_ok=True, confidence=transcript.confidence,
                          language=transcript.language, language_confidence=transcript.language_confidence,
                          word_count=len(transcript.words), transcript_matches_expected=matches,
                          status="speech_ok" if matches else "transcript_mismatch")
        finally:
            service.close()
    except SpeechReviewRequired as error:
        report.update(status="stt_review_required", stt_response_received=True,
                      confidence=error.confidence, language_confidence=error.language_confidence,
                      word_count=error.word_count)
    except ProviderError as error:
        report.update(status="provider_blocked", error_code=error.code)
    except ValueError:
        report.update(status="policy_or_quality_blocked")
    except Exception:
        report.update(status="diagnostic_failed")
    finally:
        client.close()
    return report


def _stop_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.kill()
    process.communicate(timeout=5)


def run_bounded(args, secrets):
    """Watchdog covers slow HTTP/Playwright; kill only our child process tree."""
    start = time.monotonic()
    command = [sys.executable, "-m", "qa_bot.diagnostics", args.kind, "--worker"]
    for value in args.allow_origin:
        command.extend(("--allow-origin", value))
    if args.generate:
        command.append("--generate")
    if args.screenshot:
        command.extend(("--screenshot", str(args.screenshot.resolve())))
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        start_new_session=os.name != "nt")
    try:
        output, _ = process.communicate(json.dumps(secrets), timeout=max(0.1, args.seconds - 10))
        result = json.loads(output) if process.returncode == 0 else {"status": "worker_failed"}
    except subprocess.TimeoutExpired:
        _stop_tree(process)
        result = {"status": "timeout", "answers_sent": 0}
    except Exception:
        if process.poll() is None:
            _stop_tree(process)
        result = {"status": "worker_failed"}
    return {**result, "elapsed_seconds": round(time.monotonic() - start, 3),
            "budget_seconds": args.seconds}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only page / synthetic speech diagnostics; no assessment actions")
    parser.add_argument("kind", choices=("page", "speech"))
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--allow-origin", action="append", default=[])
    parser.add_argument("--generate", action="store_true", help="Speech only: up to two free-quota requests")
    parser.add_argument("--stdin-secrets", action="store_true", help="Private pipe only; JSON never logged")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--screenshot", type=Path, help="Explicit local PNG; may contain personal page data")
    args = parser.parse_args(argv)
    try:
        validate_budget(args.seconds)
        if args.kind == "page" and (not args.allow_origin or args.generate):
            raise ValueError
        if args.worker or args.stdin_secrets:
            secrets = read_secrets(sys.stdin)
        else:
            key = "url" if args.kind == "page" else "api_key"
            # No shell history / command-line credential; input is held in memory only.
            if not sys.stdin.isatty():
                raise ValueError
            secrets = {key: getpass.getpass("Page URL: " if key == "url" else "ElevenLabs API key: ")}
        required = "url" if args.kind == "page" else "api_key"
        if required not in secrets:
            raise ValueError
        if args.worker:
            result = asyncio.run(inspect_page(secrets["url"], tuple(args.allow_origin), args.screenshot) if args.kind == "page"
                                 else inspect_speech(secrets["api_key"], generate=args.generate))
        else:
            result = run_bounded(args, secrets)
        secrets.clear()
    except Exception:
        print('{"status":"invalid_input_or_failed","secrets_logged":false}')
        return 2
    if args.report and not args.worker:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    if args.worker:
        return 0
    return 0 if result["status"] in ("observation_ok", "preflight_ok", "speech_ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
