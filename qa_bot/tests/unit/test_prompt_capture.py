import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from qa_bot.audio.prompt_capture import PromptAudioBank, make_server


class PromptCaptureTests(unittest.TestCase):
    def test_exact_origin_host_and_path_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            origin = "https://assessment.example"
            token = "p" * 32
            server = make_server(
                directory, token=token, allowed_origin=origin,
                allowed_hosts=("media.example",), path_markers=("/SpeechAssessmentBank/",))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def post(*, request_origin=origin, source="https://media.example/SpeechAssessmentBank/q.mp3",
                         path="/capture-prompt", host=None):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                    connection.request("POST", path, b"ID3-prompt", {
                        "Host": host or f"127.0.0.1:{server.server_port}",
                        "Origin": request_origin, "X-Prompt-Token": token,
                        "X-Prompt-Source": source, "X-Source-Profile": "profile-16",
                        "X-Source-Test": "TP-016", "X-Source-Question": "listen-repeat-1",
                        "Content-Type": "audio/mpeg",
                    })
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    connection.close()
                    return response.status, body

                status, report = post()
                self.assertEqual(status, 201)
                self.assertEqual(report["status"], "stored")
                self.assertNotIn("token", json.dumps(report).lower())
                manifest = [json.loads(line) for line in
                            (Path(directory) / "prompt_manifest.jsonl").read_text().splitlines()]
                self.assertEqual(len(manifest), 1)
                self.assertEqual(manifest[0]["source_host"], "media.example")
                self.assertEqual(manifest[0]["source_path"], "/SpeechAssessmentBank/q.mp3")

                self.assertEqual(post(request_origin="https://evil.example")[0], 403)
                self.assertEqual(post(source="https://evil.example/SpeechAssessmentBank/q.mp3")[0], 400)
                self.assertEqual(post(source="https://media.example/other/q.mp3")[0], 400)
                self.assertEqual(post(path="/wrong")[0], 403)
                self.assertEqual(post(host=f"localhost:{server.server_port}")[0], 403)
                self.assertEqual(len((Path(directory) / "prompt_manifest.jsonl").read_text().splitlines()), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_manifest_deduplicates_same_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            bank = PromptAudioBank(
                directory, allowed_hosts=("media.example",),
                path_markers=("/SpeechAssessmentBank/",))
            args = dict(
                source_url="https://media.example/SpeechAssessmentBank/q.wav",
                source_profile="profile-16", source_test="TP-016",
                source_question="listen-repeat-1", content_type="audio/wav",
                audio=b"RIFF-prompt",
            )
            self.assertEqual(bank.record(**args)["status"], "stored")
            self.assertEqual(bank.record(**args)["status"], "duplicate")
            self.assertEqual(len(bank.manifest.read_text().splitlines()), 1)

    def test_server_rejects_broad_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                make_server(directory, token="short", allowed_origin="https://assessment.example",
                            allowed_hosts=("media.example",), path_markers=("/Speech/",))
            with self.assertRaises(ValueError):
                make_server(directory, token="x" * 24, allowed_origin="http://assessment.example",
                            allowed_hosts=("media.example",), path_markers=("/Speech/",))
            with self.assertRaises(ValueError):
                PromptAudioBank(directory, allowed_hosts=("https://media.example",),
                                path_markers=("/Speech/",))
