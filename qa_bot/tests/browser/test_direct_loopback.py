import asyncio
import io
import math
import unittest
import wave
from unittest.mock import patch

from qa_bot.adapters.browser.controller import BrowserController
from qa_bot.audio.direct_loopback import DirectAudioLoopback


def _tone_wav() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        frames = bytearray()
        for index in range(16000):
            sample = int(11000 * math.sin(2 * math.pi * 523.25 * index / 16000))
            frames.extend(sample.to_bytes(2, "little", signed=True))
        audio.writeframes(frames)
    return stream.getvalue()


class DirectLoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_prompt_reaches_microphone_without_tts(self):
        tone = _tone_wav()
        html = """<!doctype html><title>loopback</title><main>Listen Carefully</main>
        <script>fetch('/SpeechAssessmentBank/q/question1.wav').then(r=>r.arrayBuffer())</script>"""

        async def route(controller, request):
            if request.request.url.endswith("question1.wav"):
                await request.fulfill(status=200, content_type="audio/wav", body=tone)
            else:
                await request.fulfill(status=200, content_type="text/html", body=html)

        loopback = DirectAudioLoopback(
            allowed_hosts=("staging.example.invalid",), maximum_replays=1)
        with patch.object(BrowserController, "_route", route):
            browser = BrowserController(
                ("https://staging.example.invalid",),
                launch_args=("--autoplay-policy=user-gesture-required",),
                init_scripts=(loopback.init_script(),),
            )
            try:
                await browser.open("https://staging.example.invalid")
                await browser._page.wait_for_function(
                    "__qaDirectAudioLoopback.status === 'prepared'", timeout=3000)
                result = await browser._page.evaluate("""async () => {
                  const media = await navigator.mediaDevices.getUserMedia({audio: true});
                  document.querySelector('main').textContent = 'Speak Now';
                  const context = new AudioContext();
                  const analyser = context.createAnalyser();
                  analyser.fftSize = 2048;
                  context.createMediaStreamSource(media).connect(analyser);
                  const samples = new Float32Array(analyser.fftSize);
                  let peak = 0;
                  for (let pass = 0; pass < 50; pass++) {
                    await new Promise(resolve => setTimeout(resolve, 40));
                    analyser.getFloatTimeDomainData(samples);
                    for (const value of samples) peak = Math.max(peak, Math.abs(value));
                  }
                  return {peak, count: globalThis.__qaDirectAudioLoopback.replayCount,
                          status: globalThis.__qaDirectAudioLoopback.status,
                          failures: globalThis.__qaDirectAudioLoopback.failures};
                }""")
                self.assertGreater(result["peak"], 0.05)
                self.assertEqual(result["count"], 1)
                self.assertEqual(result["failures"], [])
            finally:
                await browser.close()

    async def test_recording_window_never_waits_for_unprepared_audio(self):
        tone = _tone_wav()
        html = """<!doctype html><main>Speak Now</main>
        <audio preload='none' src='/SpeechAssessmentBank/q/slow.wav'></audio>"""

        async def route(controller, request):
            if request.request.url.endswith("slow.wav"):
                await asyncio.sleep(0.18)
                await request.fulfill(status=200, content_type="audio/wav", body=tone)
            else:
                await request.fulfill(status=200, content_type="text/html", body=html)

        loopback = DirectAudioLoopback(
            allowed_hosts=("staging.example.invalid",), maximum_replays=1)
        with patch.object(BrowserController, "_route", route):
            browser = BrowserController(
                ("https://staging.example.invalid",),
                init_scripts=(loopback.init_script(),),
            )
            try:
                await browser.open("https://staging.example.invalid")
                result = await browser._page.evaluate("""async () => {
                  await navigator.mediaDevices.getUserMedia({audio: true});
                  await new Promise(resolve => setTimeout(resolve, 60));
                  return {
                    status: __qaDirectAudioLoopback.status,
                    missed: __qaDirectAudioLoopback.missed.size,
                    unprepared: __qaDirectAudioLoopback.recordingUnpreparedCount,
                    replayCount: __qaDirectAudioLoopback.replayCount,
                    failures: __qaDirectAudioLoopback.failures,
                  };
                }""")
                self.assertEqual(result, {
                    "status": "recording_unprepared",
                    "missed": 1,
                    "unprepared": 1,
                    "replayCount": 0,
                    "failures": ["recording_unprepared"],
                })
                await browser._page.wait_for_timeout(300)
                self.assertEqual(
                    await browser._page.evaluate("__qaDirectAudioLoopback.replayCount"), 0)
            finally:
                await browser.close()

    async def test_recording_word_inside_help_text_does_not_open_window(self):
        tone = _tone_wav()
        html = """<!doctype html><main id='phase'>Listen Carefully</main>
        <p>Recording instructions are shown before each question.</p>
        <audio src='/SpeechAssessmentBank/q/help-text.wav'></audio>"""

        async def route(controller, request):
            if request.request.url.endswith("help-text.wav"):
                await request.fulfill(status=200, content_type="audio/wav", body=tone)
            else:
                await request.fulfill(status=200, content_type="text/html", body=html)

        loopback = DirectAudioLoopback(
            allowed_hosts=("staging.example.invalid",), maximum_replays=1)
        with patch.object(BrowserController, "_route", route):
            browser = BrowserController(
                ("https://staging.example.invalid",),
                init_scripts=(loopback.init_script(),),
            )
            try:
                await browser.open("https://staging.example.invalid")
                await browser._page.wait_for_function(
                    "__qaDirectAudioLoopback.status === 'prepared'", timeout=3000)
                await browser._page.evaluate(
                    "navigator.mediaDevices.getUserMedia({audio:true})")
                await browser._page.wait_for_timeout(100)
                self.assertEqual(
                    await browser._page.evaluate("__qaDirectAudioLoopback.replayCount"), 0)
                await browser._page.locator("#phase").evaluate(
                    "node => node.textContent = 'Speak Now'")
                await browser._page.wait_for_function(
                    "__qaDirectAudioLoopback.replayCount === 1", timeout=1000)
            finally:
                await browser.close()

    async def test_prompt_is_decoded_before_short_recording_window(self):
        tone = _tone_wav()
        html = """<!doctype html><title>loopback</title><main>Listen Carefully</main>
        <audio src='/SpeechAssessmentBank/q/question2.wav'></audio>"""

        async def route(controller, request):
            if request.request.url.endswith("question2.wav"):
                await request.fulfill(status=200, content_type="audio/wav", body=tone)
            else:
                await request.fulfill(status=200, content_type="text/html", body=html)

        loopback = DirectAudioLoopback(
            allowed_hosts=("staging.example.invalid",), maximum_replays=1)
        with patch.object(BrowserController, "_route", route):
            browser = BrowserController(
                ("https://staging.example.invalid",),
                init_scripts=(loopback.init_script(),),
            )
            try:
                await browser.open("https://staging.example.invalid")
                result = await browser._page.evaluate("""async () => {
                  const qa = globalThis.__qaDirectAudioLoopback;
                  const deadline = performance.now() + 2000;
                  while (qa.status !== 'prepared' && performance.now() < deadline) {
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
                  const recordUntil = performance.now() + 250;
                  while (performance.now() < recordUntil) {
                    await new Promise(resolve => setTimeout(resolve, 10));
                    analyser.getFloatTimeDomainData(samples);
                    for (const value of samples) peak = Math.max(peak, Math.abs(value));
                  }
                  return {peak, count: qa.replayCount,
                          startDelay: qa.replayStartedAt - qa.recordingSeenAt,
                          failures: qa.failures};
                }""")
                self.assertGreater(result["peak"], 0.05)
                self.assertEqual(result["count"], 1)
                self.assertLess(result["startDelay"], 100)
                self.assertEqual(result["failures"], [])
            finally:
                await browser.close()

    async def test_prepare_failures_use_bounded_backoff_without_retry_storm(self):
        html = """<!doctype html><main>Listen Carefully</main>
        <audio preload='none' src='/SpeechAssessmentBank/q/unavailable.wav'></audio>"""
        calls = 0

        async def route(controller, request):
            nonlocal calls
            if request.request.url.endswith("unavailable.wav"):
                calls += 1
                await request.fulfill(status=503, content_type="text/plain", body="unavailable")
            else:
                await request.fulfill(status=200, content_type="text/html", body=html)

        loopback = DirectAudioLoopback(
            allowed_hosts=("staging.example.invalid",), maximum_replays=1,
            maximum_prepare_attempts=4, retry_base_ms=20, failure_history_limit=2)
        with patch.object(BrowserController, "_route", route):
            browser = BrowserController(
                ("https://staging.example.invalid",),
                init_scripts=(loopback.init_script(),),
            )
            try:
                await browser.open("https://staging.example.invalid")
                await browser._page.wait_for_function(
                    "__qaDirectAudioLoopback.status === 'failed'", timeout=3000)
                await browser._page.wait_for_timeout(300)
                result = await browser._page.evaluate("""() => ({
                  status: __qaDirectAudioLoopback.status,
                  attempts: __qaDirectAudioLoopback.prepareAttempts,
                  failures: __qaDirectAudioLoopback.failures,
                  replayCount: __qaDirectAudioLoopback.replayCount
                })""")
                # Each bounded preparation attempt uses fetch once and XHR once.
                self.assertEqual(calls, 8)
                self.assertEqual(result["attempts"], 4)
                self.assertEqual(len(result["failures"]), 2)
                self.assertTrue(all(value == "audio_transport_failed:xhr_http_503"
                                    for value in result["failures"]))
                self.assertEqual(result["replayCount"], 0)
            finally:
                await browser.close()

    async def test_transient_prepare_failure_recovers_and_keeps_fast_replay(self):
        tone = _tone_wav()
        html = """<!doctype html><main>Listen Carefully</main>
        <audio preload='none' src='/SpeechAssessmentBank/q/retry.wav'></audio>"""
        calls = 0

        async def route(controller, request):
            nonlocal calls
            if request.request.url.endswith("retry.wav"):
                calls += 1
                if calls < 5:
                    await request.fulfill(status=503, content_type="text/plain", body="retry")
                else:
                    await request.fulfill(status=200, content_type="audio/wav", body=tone)
            else:
                await request.fulfill(status=200, content_type="text/html", body=html)

        loopback = DirectAudioLoopback(
            allowed_hosts=("staging.example.invalid",), maximum_replays=1,
            maximum_prepare_attempts=3, retry_base_ms=20)
        with patch.object(BrowserController, "_route", route):
            browser = BrowserController(
                ("https://staging.example.invalid",),
                init_scripts=(loopback.init_script(),),
            )
            try:
                await browser.open("https://staging.example.invalid")
                await browser._page.wait_for_function(
                    "__qaDirectAudioLoopback.status === 'prepared'", timeout=3000)
                result = await browser._page.evaluate("""async () => {
                  const media = await navigator.mediaDevices.getUserMedia({audio: true});
                  document.querySelector('main').textContent = 'Speak Now';
                  const context = new AudioContext();
                  const analyser = context.createAnalyser();
                  analyser.fftSize = 2048;
                  context.createMediaStreamSource(media).connect(analyser);
                  const samples = new Float32Array(analyser.fftSize);
                  let peak = 0;
                  const deadline = performance.now() + 250;
                  while (performance.now() < deadline) {
                    await new Promise(resolve => setTimeout(resolve, 5));
                    analyser.getFloatTimeDomainData(samples);
                    for (const value of samples) peak = Math.max(peak, Math.abs(value));
                  }
                  return {peak, attempts: __qaDirectAudioLoopback.prepareAttempts,
                    failures: __qaDirectAudioLoopback.failures,
                    replayCount: __qaDirectAudioLoopback.replayCount,
                    startDelay: __qaDirectAudioLoopback.replayStartedAt -
                                __qaDirectAudioLoopback.recordingSeenAt};
                }""")
                self.assertEqual(calls, 5)
                self.assertEqual(result["attempts"], 3)
                self.assertEqual(result["failures"], [
                    "audio_transport_failed:xhr_http_503",
                    "audio_transport_failed:xhr_http_503",
                ])
                self.assertEqual(result["replayCount"], 1)
                self.assertGreater(result["peak"], 0.05)
                self.assertLess(result["startDelay"], 100)
            finally:
                await browser.close()

    async def test_fetch_failure_falls_back_to_xhr_and_replays_fast(self):
        tone = _tone_wav()
        html = """<!doctype html><main>Listen Carefully</main>
        <audio preload='none' src='/SpeechAssessmentBank/q/fallback.wav?signature=secret'></audio>"""
        transports = []

        async def route(controller, request):
            if "/fallback.wav?" in request.request.url:
                transports.append(request.request.resource_type)
                if request.request.resource_type == "fetch":
                    await request.abort("failed")
                elif request.request.resource_type == "xhr":
                    await request.fulfill(status=200, content_type="audio/wav", body=tone)
                else:
                    await request.abort("blockedbyclient")
            else:
                await request.fulfill(status=200, content_type="text/html", body=html)

        loopback = DirectAudioLoopback(
            allowed_hosts=("staging.example.invalid",), maximum_replays=1)
        with patch.object(BrowserController, "_route", route):
            browser = BrowserController(
                ("https://staging.example.invalid",),
                init_scripts=(loopback.init_script(),),
            )
            try:
                await browser.open("https://staging.example.invalid")
                await browser._page.wait_for_function(
                    "__qaDirectAudioLoopback.status === 'prepared'", timeout=3000)
                result = await browser._page.evaluate("""async () => {
                  const media = await navigator.mediaDevices.getUserMedia({audio: true});
                  document.querySelector('main').textContent = 'Speak Now';
                  const context = new AudioContext();
                  const analyser = context.createAnalyser();
                  analyser.fftSize = 2048;
                  context.createMediaStreamSource(media).connect(analyser);
                  const samples = new Float32Array(analyser.fftSize);
                  let peak = 0;
                  const deadline = performance.now() + 250;
                  while (performance.now() < deadline) {
                    await new Promise(resolve => setTimeout(resolve, 5));
                    analyser.getFloatTimeDomainData(samples);
                    for (const value of samples) peak = Math.max(peak, Math.abs(value));
                  }
                  return {peak, attempts: __qaDirectAudioLoopback.prepareAttempts,
                    xhrFallbackCount: __qaDirectAudioLoopback.xhrFallbackCount,
                    failures: __qaDirectAudioLoopback.failures,
                    replayCount: __qaDirectAudioLoopback.replayCount,
                    activeSource: __qaDirectAudioLoopback.active.source,
                    startDelay: __qaDirectAudioLoopback.replayStartedAt -
                                __qaDirectAudioLoopback.recordingSeenAt};
                }""")
                self.assertEqual(transports, ["fetch", "xhr"])
                self.assertEqual(result["attempts"], 1)
                self.assertEqual(result["xhrFallbackCount"], 1)
                self.assertEqual(result["failures"], [])
                self.assertEqual(result["replayCount"], 1)
                self.assertEqual(result["activeSource"],
                                 "https://staging.example.invalid/SpeechAssessmentBank/q/fallback.wav")
                self.assertGreater(result["peak"], 0.05)
                self.assertLess(result["startDelay"], 100)
            finally:
                await browser.close()

    async def test_dynamic_audio_src_is_observed_before_recording(self):
        tone = _tone_wav()
        html = """<!doctype html><main>Listen Carefully</main>
        <audio id='prompt' preload='none'></audio>
        <script>setTimeout(() => document.getElementById('prompt').src =
          '/SpeechAssessmentBank/q/dynamic.wav?signed=not-persisted', 20)</script>"""

        async def route(controller, request):
            if "/dynamic.wav?" in request.request.url:
                await request.fulfill(status=200, content_type="audio/wav", body=tone)
            else:
                await request.fulfill(status=200, content_type="text/html", body=html)

        loopback = DirectAudioLoopback(
            allowed_hosts=("staging.example.invalid",), maximum_replays=1)
        with patch.object(BrowserController, "_route", route):
            browser = BrowserController(
                ("https://staging.example.invalid",),
                init_scripts=(loopback.init_script(),),
            )
            try:
                await browser.open("https://staging.example.invalid")
                await browser._page.wait_for_function(
                    "__qaDirectAudioLoopback.status === 'prepared'", timeout=3000)
                result = await browser._page.evaluate("""() => ({
                  attempts: __qaDirectAudioLoopback.prepareAttempts,
                  source: __qaDirectAudioLoopback.active.source,
                  replayCount: __qaDirectAudioLoopback.replayCount
                })""")
                self.assertEqual(result, {
                    "attempts": 1,
                    "source": "https://staging.example.invalid/SpeechAssessmentBank/q/dynamic.wav",
                    "replayCount": 0,
                })
            finally:
                await browser.close()

    def test_rejects_broad_or_invalid_configuration(self):
        with self.assertRaises(ValueError):
            DirectAudioLoopback(allowed_hosts=("https://example.test",))
        with self.assertRaises(ValueError):
            DirectAudioLoopback(maximum_replays=0)
        with self.assertRaises(ValueError):
            DirectAudioLoopback(maximum_prepare_attempts=0)
        with self.assertRaises(ValueError):
            DirectAudioLoopback(retry_base_ms=0)
        with self.assertRaises(ValueError):
            DirectAudioLoopback(failure_history_limit=0)
        with self.assertRaises(ValueError):
            DirectAudioLoopback(capture_bridge_url="http://localhost:8771",
                                capture_token="x" * 24,
                                source_profile="profile", source_test="TP-016")
        with self.assertRaises(ValueError):
            DirectAudioLoopback(capture_bridge_url="http://127.0.0.1:8771")
