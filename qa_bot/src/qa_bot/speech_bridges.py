"""Launch the authenticated localhost speech services for one QA session."""
import argparse
import os
from pathlib import Path
import threading

from qa_bot.audio.shared_microphone import SharedMicrophoneBridge
from qa_bot.audio.dynamic_read_aloud import DynamicReadAloudBridge
from qa_bot.audio.played_prompt_loopback import PlayedPromptLoopback
from qa_bot.audio.live_speech_bridge import LiveSpeechController, make_server as speech_server
from qa_bot.audio.injection_server import make_server as injection_server
from qa_bot.audio.prompt_capture import make_server as capture_server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--test", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--speech-bank-dir", type=Path,
                        help="Shared durable bank directory; defaults to this run output")
    parser.add_argument("--replay-only", action="store_true",
                        help="Reject unknown speech questions without generating audio")
    parser.add_argument("--counter-selector", default="")
    parser.add_argument("--suspension-selector", default="")
    parser.add_argument("--repeat-section", required=True)
    parser.add_argument("--speech-port", type=int, default=18769)
    parser.add_argument("--script-port", type=int, default=18770)
    parser.add_argument("--capture-port", type=int, default=18771)
    args = parser.parse_args(argv)
    if args.repeat_section != 'Section B: Listen and Repeat':
        parser.error('this live adapter requires Section B: Listen and Repeat')
    token = os.environ.get("QA_LOCAL_BRIDGE_TOKEN", "")
    if len(token) < 24:
        parser.error("QA_LOCAL_BRIDGE_TOKEN must contain at least 24 characters")
    shared = SharedMicrophoneBridge(args.origin,idle_floor=.001)
    args.output.mkdir(parents=True, exist_ok=True)
    bank_dir = args.speech_bank_dir or args.output
    bank_dir.mkdir(parents=True, exist_ok=True)
    read = DynamicReadAloudBridge(
        f"http://127.0.0.1:{args.speech_port}", token, args.origin, "/",
        args.profile, args.test, auto_detect=True,
        question_counter_selector=args.counter_selector,
        suspension_selector=args.suspension_selector,
        section_heading='Section A: Read and Speak',
    )
    loop = PlayedPromptLoopback(args.origin, token, args.profile, args.test,
        capture_url=f'http://127.0.0.1:{args.capture_port}')
    bundle = shared.init_script() + "\n" + loop.init_script() + "\n" + read.init_script()
    servers = []
    try:
        servers.append(speech_server(
            LiveSpeechController(bank_dir / "speech.sqlite3", bank_dir / "answers",
                                 replay_only=args.replay_only),
            token=token, allowed_origin=args.origin, port=args.speech_port))
        servers.append(injection_server(
            bundle, token=token, allowed_origin=args.origin, port=args.script_port))
        servers.append(capture_server(
            args.output / "prompts", token=token, allowed_origin=args.origin,
            allowed_hosts=('qbdata-amcat.s3.amazonaws.com',), path_markers=('/SpeechAssessmentBank/',),
            port=args.capture_port))
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        print("Local speech services ready; install the bundle before the page opens its microphone.", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        for server in servers:
            server.shutdown()
    finally:
        for server in servers:
            server.server_close()


if __name__ == "__main__":
    main()
