import io
import math
import tempfile
import threading
import time
import unittest
import wave

from playwright.async_api import async_playwright

from qa_bot.audio.direct_loopback import DirectAudioLoopback
from qa_bot.audio.prompt_capture import PromptAudioBank, make_server


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


class _SlowBank(PromptAudioBank):
    def record(self, **kwargs):
        time.sleep(0.4)
        return super().record(**kwargs)


class PromptCaptureLoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_manifest_write_does_not_delay_replay(self):
        origin = "https://assessment.example"
        token = "c" * 32
        tone = _tone_wav()
        with tempfile.TemporaryDirectory() as directory:
            bank = _SlowBank(
                directory, allowed_hosts=("assessment.example",),
                path_markers=("/SpeechAssessmentBank/",))
            server = make_server(
                directory, token=token, allowed_origin=origin,
                allowed_hosts=("assessment.example",),
                path_markers=("/SpeechAssessmentBank/",), bank=bank)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            engine = await async_playwright().start()
            browser = await engine.chromium.launch(headless=True)
            context = await browser.new_context()
            await context.grant_permissions(["local-network-access"], origin=origin)
            loopback = DirectAudioLoopback(
                allowed_hosts=("assessment.example",), maximum_replays=1,
                capture_bridge_url=f"http://127.0.0.1:{server.server_port}",
                capture_token=token, source_profile="profile-16", source_test="TP-016")
            await context.add_init_script(script=loopback.init_script())
            page = await context.new_page()
            try:
                async def route(request):
                    if "/prompt.wav?signature=private" in request.request.url:
                        await request.fulfill(status=200, content_type="audio/wav", body=tone)
                    else:
                        await request.fulfill(
                            status=200, content_type="text/html",
                            body="<main>Listen Carefully</main>"
                                 "<audio src='/SpeechAssessmentBank/prompt.wav?signature=private'></audio>")

                await page.route(origin + "/**", route)
                await page.goto(origin + "/assessment")
                await page.wait_for_function(
                    "__qaDirectAudioLoopback.status === 'prepared'", timeout=3000)
                result = await page.evaluate("""async () => {
                  const media = await navigator.mediaDevices.getUserMedia({audio:true});
                  const context = new AudioContext();
                  const analyser = context.createAnalyser();
                  analyser.fftSize = 2048;
                  context.createMediaStreamSource(media).connect(analyser);
                  document.querySelector('main').textContent = 'Speak Now';
                  const samples = new Float32Array(analyser.fftSize);
                  let peak = 0;
                  const deadline = performance.now() + 180;
                  while (performance.now() < deadline) {
                    await new Promise(resolve => setTimeout(resolve, 5));
                    analyser.getFloatTimeDomainData(samples);
                    for (const value of samples) peak = Math.max(peak, Math.abs(value));
                  }
                  return {peak, replayCount: __qaDirectAudioLoopback.replayCount,
                    captureCount: __qaDirectAudioLoopback.captureCount,
                    startDelay: __qaDirectAudioLoopback.replayStartedAt -
                                __qaDirectAudioLoopback.recordingSeenAt};
                }""")
                self.assertGreater(result["peak"], 0.05)
                self.assertEqual(result["replayCount"], 1)
                self.assertEqual(result["captureCount"], 0)
                self.assertLess(result["startDelay"], 100)
                await page.wait_for_function(
                    "__qaDirectAudioLoopback.captureCount === 1", timeout=3000)
                manifest_text = bank.manifest.read_text()
                self.assertEqual(len(manifest_text.splitlines()), 1)
                self.assertIn('"source_path":"/SpeechAssessmentBank/prompt.wav"', manifest_text)
                self.assertNotIn("signature", manifest_text)
                self.assertNotIn("private", manifest_text)
            finally:
                await context.close()
                await browser.close()
                await engine.stop()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
