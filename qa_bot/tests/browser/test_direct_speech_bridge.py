import hashlib
import io
import json
import math
import threading
import unittest
import wave

from playwright.async_api import async_playwright

from qa_bot.audio.direct_speech_bridge import DirectSpeechBridge
from qa_bot.audio.live_speech_bridge import make_server


def _tone_wav() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        frames = bytearray()
        for index in range(8000):
            sample = int(11000 * math.sin(2 * math.pi * 523.25 * index / 16000))
            frames.extend(sample.to_bytes(2, "little", signed=True))
        audio.writeframes(frames)
    return stream.getvalue()


class _Controller:
    def __init__(self, wav):
        self.wav = wav
        self.prepared = []
        self.audio_requests = []

    def prepare(self, request):
        self.prepared.append(request)
        return {"status": "ready"}

    def audio(self, request):
        self.audio_requests.append(request)
        return self.wav, hashlib.sha256(self.wav).hexdigest()


class DirectSpeechBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepared_wav_starts_inside_short_recording_window(self):
        origin = "https://assessment.example"
        token = "s" * 32
        request = {
            "question": "Read this exact sentence.",
            "answer": "Read this exact sentence.",
            "source_profile": "profile-15",
            "source_test": "TP-015",
            "source_question": "spoken-1",
        }
        controller = _Controller(_tone_wav())
        server = make_server(controller, token=token, allowed_origin=origin)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        bridge = DirectSpeechBridge(
            f"http://127.0.0.1:{server.server_port}", token, request)
        engine = await async_playwright().start()
        browser = await engine.chromium.launch(
            headless=True, args=["--autoplay-policy=user-gesture-required"])
        context = await browser.new_context()
        await context.grant_permissions(["local-network-access"], origin=origin)
        await context.add_init_script(script=bridge.init_script())
        page = await context.new_page()
        try:
            await page.route(origin + "/", lambda route: route.fulfill(
                status=200, content_type="text/html",
                body="<main>Listen Carefully</main>",
            ))
            await page.goto(origin + "/")
            result = await page.evaluate("""async () => {
              const qa = globalThis.__qaDirectSpeechBridge;
              const preparedDeadline = performance.now() + 3000;
              while (qa.status !== 'prepared' && performance.now() < preparedDeadline) {
                await new Promise(resolve => setTimeout(resolve, 10));
              }
              const media = await navigator.mediaDevices.getUserMedia({audio: true});
              const context = new AudioContext();
              const analyser = context.createAnalyser();
              analyser.fftSize = 2048;
              context.createMediaStreamSource(media).connect(analyser);
              document.querySelector('main').textContent = 'Speak Now';
              const samples = new Float32Array(analyser.fftSize);
              let peak = 0;
              const deadline = performance.now() + 200;
              while (performance.now() < deadline) {
                await new Promise(resolve => setTimeout(resolve, 5));
                analyser.getFloatTimeDomainData(samples);
                for (const value of samples) peak = Math.max(peak, Math.abs(value));
              }
              return {status: qa.status, failures: qa.failures, peak,
                      prepareCount: qa.prepareCount, audioCount: qa.audioCount,
                      replayCount: qa.replayCount,
                      startDelay: qa.replayStartedAt - qa.recordingSeenAt,
                      track: {kind: media.getAudioTracks()[0].kind,
                              state: media.getAudioTracks()[0].readyState}};
            }""")
            self.assertGreater(result["peak"], 0.05)
            self.assertEqual(result["failures"], [])
            self.assertEqual(result["prepareCount"], 1)
            self.assertEqual(result["audioCount"], 1)
            self.assertEqual(result["replayCount"], 1)
            self.assertLess(result["startDelay"], 75)
            self.assertEqual(result["track"], {"kind": "audio", "state": "live"})
            self.assertEqual(controller.prepared, [request])
            self.assertEqual(controller.audio_requests, [request])
        finally:
            await context.close()
            await browser.close()
            await engine.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_rejects_non_loopback_bridge_and_incomplete_payload(self):
        request = {
            "question": "q", "answer": "a", "source_profile": "p",
            "source_test": "t", "source_question": "sq",
        }
        with self.assertRaises(ValueError):
            DirectSpeechBridge("https://example.test:8769", "x" * 24, request)
        incomplete = dict(request)
        incomplete.pop("answer")
        with self.assertRaises(ValueError):
            DirectSpeechBridge("http://127.0.0.1:8769", "x" * 24, incomplete)
