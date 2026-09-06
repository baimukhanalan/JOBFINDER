import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from qa_bot.audio.live_speech_bridge import LiveSpeechController, make_server
from qa_bot.audio.replay_bank import SpeechReplayBank
from tests.unit.test_speech_replay import FakeConverter


class FakeSpeech:
    def __init__(self):
        self.generation_requests = 0

    async def synthesize_mp3(self, *_args, **_kwargs):
        self.generation_requests += 1
        return b"ID3" + bytes(200)


class LiveSpeechBridgeTests(unittest.TestCase):
    def test_prepare_then_play_and_reject_wrong_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            played = []

            class TestBank(SpeechReplayBank):
                def __init__(self, database, artifacts):
                    super().__init__(database, artifacts, converter=FakeConverter())

            import qa_bot.audio.live_speech_bridge as module
            original = module.SpeechReplayBank
            module.SpeechReplayBank = TestBank
            try:
                controller = LiveSpeechController(
                    root / "speech.sqlite3", root / "audio",
                    speech_factory=FakeSpeech, player=lambda path: played.append(path) or 123,
                )
                server = make_server(
                    controller, token="t" * 24,
                    allowed_origin="https://assessment.example", port=0,
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                request = {
                    "question": "Exact question", "answer": "Exact answer",
                    "source_profile": "profile", "source_test": "TP-016",
                    "source_question": "q-1",
                }

                def post(path, token="t" * 24):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_address[1]}{path}",
                        data=json.dumps(request).encode(), method="POST",
                        headers={"Origin": "https://assessment.example",
                                 "X-Speech-Token": token,
                                 "Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(req) as response:
                        if path == "/audio":
                            return response.read()
                        return json.load(response)

                self.assertEqual(post("/prepare")["status"], "ready")
                self.assertEqual(post("/play")["status"], "playing")
                self.assertEqual(len(played), 1)
                self.assertEqual(post("/audio")[:4], b"RIFF")
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    post("/prepare", "x" * 24)
                self.assertEqual(raised.exception.code, 403)
            finally:
                if "server" in locals():
                    server.shutdown()
                    server.server_close()
                module.SpeechReplayBank = original
