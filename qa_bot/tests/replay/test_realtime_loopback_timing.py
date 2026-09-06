"""Local end-to-end timing contract for prompt-audio loopback.

The fixture models the shortest useful browser path: an audio URL appears while
the page says ``Listen Carefully``, the source is decoded, and only then does
the page switch to ``Speak Now``.  Persistence is deliberately slow so the
test proves that saving evidence is outside the microphone start path.
"""
from __future__ import annotations

import io
import json
import math
import os
import statistics
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

from playwright.async_api import async_playwright

from qa_bot.audio.direct_loopback import DirectAudioLoopback
from qa_bot.audio.prompt_capture import PromptAudioBank, make_server


def _tone_wav() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        frames = bytearray()
        for index in range(8_000):
            sample = int(11_000 * math.sin(2 * math.pi * 523.25 * index / 16_000))
            frames.extend(sample.to_bytes(2, "little", signed=True))
        audio.writeframes(frames)
    return stream.getvalue()


class _SlowBank(PromptAudioBank):
    def record(self, **kwargs):
        time.sleep(0.35)
        return super().record(**kwargs)


class RealtimeLoopbackTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefetch_fast_start_and_parallel_persistence(self):
        origin = "https://assessment.example"
        token = "t" * 32
        tone = _tone_wav()
        with tempfile.TemporaryDirectory() as directory:
            bank = _SlowBank(
                directory,
                allowed_hosts=("assessment.example",),
                path_markers=("/SpeechAssessmentBank/",),
            )
            server = make_server(
                directory,
                token=token,
                allowed_origin=origin,
                allowed_hosts=("assessment.example",),
                path_markers=("/SpeechAssessmentBank/",),
                bank=bank,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            engine = await async_playwright().start()
            browser = await engine.chromium.launch(headless=True)
            context = await browser.new_context()
            await context.grant_permissions(["local-network-access"], origin=origin)
            loopback = DirectAudioLoopback(
                allowed_hosts=("assessment.example",),
                maximum_replays=1,
                maximum_prepare_attempts=3,
                retry_base_ms=20,
                capture_bridge_url=f"http://127.0.0.1:{server.server_port}",
                capture_token=token,
                source_profile="synthetic-profile",
                source_test="synthetic-test",
            )
            await context.add_init_script(script=loopback.init_script())
            page = await context.new_page()
            try:
                async def route(request):
                    if "/prompt.wav?" in request.request.url:
                        await request.fulfill(
                            status=200, content_type="audio/wav", body=tone)
                    else:
                        await request.fulfill(
                            status=200,
                            content_type="text/html",
                            body=(
                                "<main id='phase'>Listen Carefully</main>"
                                "<audio id='prompt' preload='none'></audio>"
                                "<script>setTimeout(() => document.getElementById('prompt').src = "
                                "'/SpeechAssessmentBank/prompt.wav?signed=private', 20)</script>"
                            ),
                        )

                await page.route(origin + "/**", route)
                await page.goto(origin + "/assessment")
                await page.wait_for_function(
                    "__qaDirectAudioLoopback.status === 'prepared'", timeout=3_000)

                result = await page.evaluate("""async () => {
                  const qa = globalThis.__qaDirectAudioLoopback;
                  const media = await navigator.mediaDevices.getUserMedia({audio: true});
                  const context = new AudioContext();
                  const analyser = context.createAnalyser();
                  analyser.fftSize = 2048;
                  context.createMediaStreamSource(media).connect(analyser);
                  const phaseChangedAt = performance.now();
                  document.querySelector('#phase').textContent = 'Speak Now';
                  const samples = new Float32Array(analyser.fftSize);
                  let peak = 0;
                  const deadline = phaseChangedAt + 180;
                  while (performance.now() < deadline) {
                    await new Promise(resolve => setTimeout(resolve, 5));
                    analyser.getFloatTimeDomainData(samples);
                    for (const value of samples) peak = Math.max(peak, Math.abs(value));
                  }
                  return {
                    peak,
                    replayCount: qa.replayCount,
                    prepareAttempts: qa.prepareAttempts,
                    pendingRetries: qa.retries.size,
                    failures: qa.failures,
                    captureCountAtDeadline: qa.captureCount,
                    detectionDelay: qa.recordingSeenAt - phaseChangedAt,
                    wallStartDelay: qa.replayStartedAt - phaseChangedAt,
                  };
                }""")

                self.assertGreater(result["peak"], 0.05)
                self.assertEqual(result["replayCount"], 1)
                self.assertEqual(result["prepareAttempts"], 1)
                self.assertEqual(result["pendingRetries"], 0)
                self.assertEqual(result["failures"], [])
                self.assertLess(result["detectionDelay"], 100)
                self.assertLess(result["wallStartDelay"], 100)
                self.assertEqual(result["captureCountAtDeadline"], 0)

                await page.wait_for_function(
                    "__qaDirectAudioLoopback.captureCount === 1", timeout=3_000)
                manifest = bank.manifest.read_text(encoding="utf-8")
                self.assertEqual(len(manifest.splitlines()), 1)
                self.assertIn('"source_path":"/SpeechAssessmentBank/prompt.wav"', manifest)
                self.assertNotIn("signed", manifest)
                self.assertNotIn("private", manifest)
            finally:
                await context.close()
                await browser.close()
                await engine.stop()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    async def test_fifteen_question_soak_keeps_every_window_below_100_ms(self):
        origin = "https://assessment.example"
        token = "s" * 32
        tone = _tone_wav()
        with tempfile.TemporaryDirectory() as directory:
            bank = PromptAudioBank(
                directory,
                allowed_hosts=("assessment.example",),
                path_markers=("/SpeechAssessmentBank/",),
            )
            server = make_server(
                directory,
                token=token,
                allowed_origin=origin,
                allowed_hosts=("assessment.example",),
                path_markers=("/SpeechAssessmentBank/",),
                bank=bank,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            engine = await async_playwright().start()
            browser = await engine.chromium.launch(headless=True)
            context = await browser.new_context()
            await context.grant_permissions(["local-network-access"], origin=origin)
            loopback = DirectAudioLoopback(
                allowed_hosts=("assessment.example",),
                maximum_replays=15,
                maximum_prepare_attempts=3,
                retry_base_ms=20,
                capture_bridge_url=f"http://127.0.0.1:{server.server_port}",
                capture_token=token,
                source_profile="synthetic-profile",
                source_test="synthetic-soak-15",
            )
            await context.add_init_script(script=loopback.init_script())
            page = await context.new_page()
            try:
                async def route(request):
                    if "/SpeechAssessmentBank/" in request.request.url:
                        if request.request.resource_type == "xhr":
                            await request.fulfill(
                                status=200, content_type="audio/wav", body=tone)
                        else:
                            # Model the live failure mode: the element/fetch
                            # paths fail, while same-URL XHR succeeds.
                            await request.abort("failed")
                    else:
                        await request.fulfill(
                            status=200,
                            content_type="text/html",
                            body=(
                                "<style>body{margin:0;background:#f3f6fb;font:28px system-ui;color:#172033}"
                                "main{max-width:900px;margin:100px auto;padding:48px;background:white;"
                                "border-radius:24px;box-shadow:0 12px 40px #18233b22}"
                                ".tag{font-size:18px;color:#3157b7;font-weight:700}"
                                "#question{font-size:44px;font-weight:750;margin:24px 0}"
                                "#metrics{font-size:22px;color:#3d4a66;margin-top:34px}</style>"
                                "<main><div class='tag'>LOCAL SYNTHETIC TIMING QA</div>"
                                "<div id='question'>Question 0 of 15</div>"
                                "<div id='phase'>Listen Carefully</div>"
                                "<div id='metrics'>Waiting for prefetch</div></main>"
                                "<audio id='prompt' preload='none'></audio>"
                            ),
                        )

                await page.route(origin + "/**", route)
                await page.goto(origin + "/assessment")
                await page.evaluate("""async () => {
                  const media = await navigator.mediaDevices.getUserMedia({audio: true});
                  const context = new AudioContext();
                  const analyser = context.createAnalyser();
                  analyser.fftSize = 2048;
                  context.createMediaStreamSource(media).connect(analyser);
                  globalThis.__qaSoakAnalyser = analyser;
                }""")

                delays = []
                frames_value = os.environ.get("QA_TIMING_FRAMES_DIR")
                frames = Path(frames_value) if frames_value else None
                if frames:
                    frames.mkdir(parents=True, exist_ok=True)
                for number in range(1, 16):
                    await page.evaluate("""number => {
                      document.querySelector('#phase').textContent = 'Listen Carefully';
                      document.querySelector('#question').textContent = `Question ${number} of 15`;
                      document.querySelector('#metrics').textContent =
                        'Prompt detected; prefetch and decode running';
                      document.querySelector('#prompt').src =
                        `/SpeechAssessmentBank/q-${number}.wav?signed=private-${number}`;
                    }""", number)
                    await page.wait_for_function(
                        "expected => __qaDirectAudioLoopback.prepared.size === expected",
                        arg=number, timeout=3_000)
                    # Let the polling loop observe that the prior recording
                    # window has closed before opening the next one.
                    await page.wait_for_timeout(70)
                    await page.evaluate("""() => {
                      document.querySelector('#metrics').textContent =
                        'Audio armed before recording window';
                    }""")
                    if frames:
                        await page.screenshot(
                            path=str(frames / f"frame-{number:02d}-prepared.jpg"),
                            type="jpeg", quality=88,
                        )
                    result = await page.evaluate("""async expected => {
                      const qa = globalThis.__qaDirectAudioLoopback;
                      const phaseChangedAt = performance.now();
                      document.querySelector('#phase').textContent = 'Speak Now';
                      const samples = new Float32Array(__qaSoakAnalyser.fftSize);
                      let peak = 0;
                      const deadline = phaseChangedAt + 180;
                      while (performance.now() < deadline && qa.replayCount < expected) {
                        await new Promise(resolve => setTimeout(resolve, 5));
                        __qaSoakAnalyser.getFloatTimeDomainData(samples);
                        for (const value of samples) peak = Math.max(peak, Math.abs(value));
                      }
                      // Sample a little longer after replay starts so a
                      // non-zero microphone signal is part of every result.
                      const signalDeadline = performance.now() + 40;
                      while (performance.now() < signalDeadline) {
                        await new Promise(resolve => setTimeout(resolve, 5));
                        __qaSoakAnalyser.getFloatTimeDomainData(samples);
                        for (const value of samples) peak = Math.max(peak, Math.abs(value));
                      }
                      return {
                        peak,
                        replayCount: qa.replayCount,
                        detectionDelay: qa.recordingSeenAt - phaseChangedAt,
                        wallStartDelay: qa.replayStartedAt - phaseChangedAt,
                      };
                    }""", number)
                    self.assertEqual(result["replayCount"], number)
                    self.assertGreater(result["peak"], 0.05)
                    self.assertGreaterEqual(result["detectionDelay"], 0)
                    self.assertLess(result["detectionDelay"], 100)
                    self.assertGreaterEqual(result["wallStartDelay"], 0)
                    self.assertLess(result["wallStartDelay"], 100)
                    delays.append(result["wallStartDelay"])
                    await page.evaluate("""result => {
                      document.querySelector('#metrics').textContent =
                        `Replay ${result.replayCount} of 15; start delay ` +
                        `${result.wallStartDelay.toFixed(3)} ms; microphone signal OK`;
                    }""", result)
                    if frames:
                        await page.screenshot(
                            path=str(frames / f"frame-{number:02d}-replayed.jpg"),
                            type="jpeg", quality=88,
                        )
                    await page.wait_for_timeout(520)

                final = await page.evaluate("""() => ({
                  replayCount: __qaDirectAudioLoopback.replayCount,
                  prepareAttempts: __qaDirectAudioLoopback.prepareAttempts,
                  pendingRetries: __qaDirectAudioLoopback.retries.size,
                  failures: __qaDirectAudioLoopback.failures,
                  xhrFallbackCount: __qaDirectAudioLoopback.xhrFallbackCount,
                })""")
                self.assertEqual(final, {
                    "replayCount": 15,
                    "prepareAttempts": 15,
                    "pendingRetries": 0,
                    "failures": [],
                    "xhrFallbackCount": 15,
                })
                self.assertLess(max(delays), 100)
                await page.wait_for_function(
                    "__qaDirectAudioLoopback.captureCount === 15", timeout=5_000)
                manifest = bank.manifest.read_text(encoding="utf-8")
                self.assertEqual(len(manifest.splitlines()), 15)
                self.assertNotIn("?", manifest)
                self.assertNotIn("private", manifest)
                report_path = os.environ.get("QA_TIMING_REPORT")
                if report_path:
                    report = {
                        "schema_version": 1,
                        "kind": "local_synthetic_audio_timing_soak",
                        "questions": 15,
                        "successful_replays": final["replayCount"],
                        "prepare_attempts": final["prepareAttempts"],
                        "failures": final["failures"],
                        "start_delay_ms": {
                            "minimum": round(min(delays), 3),
                            "maximum": round(max(delays), 3),
                            "mean": round(statistics.fmean(delays), 3),
                            "samples": [round(value, 3) for value in delays],
                        },
                        "limit_ms": 100,
                        "capture_count": 15,
                        "xhr_fallback_count": final["xhrFallbackCount"],
                        "signed_query_persisted": False,
                    }
                    destination = Path(report_path)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(
                        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            finally:
                await context.close()
                await browser.close()
                await engine.stop()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
