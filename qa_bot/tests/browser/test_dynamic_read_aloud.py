import hashlib
import io
import math
import threading
import time
import unittest
import wave

from playwright.async_api import async_playwright

from qa_bot.audio.dynamic_read_aloud import DynamicReadAloudBridge
from qa_bot.audio.live_speech_bridge import make_server


def _tone_wav() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        frames = bytearray()
        for index in range(4000):
            sample = int(11000 * math.sin(2 * math.pi * 440 * index / 16000))
            frames.extend(sample.to_bytes(2, "little", signed=True))
        audio.writeframes(frames)
    return stream.getvalue()


class _Controller:
    def __init__(self):
        self.wav = _tone_wav()
        self.prepared = []
        self.audio_requests = []
        self.delay_seconds = 0

    def prepare(self, request):
        self.prepared.append(request)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return {"status": "ready"}

    def audio(self, request):
        self.audio_requests.append(request)
        return self.wav, hashlib.sha256(self.wav).hexdigest()


def _question(site_id: str, sentence: str, *, phase: str = "Prepare") -> str:
    return f"""<section data-qa-read-aloud-question data-question-id='{site_id}'>
      <p data-qa-read-aloud-instruction>Read the given sentence out loud.</p>
      <p data-qa-read-aloud-text>{sentence}</p>
      <p data-qa-read-aloud-phase>{phase}</p>
    </section>"""


def _screenshot_question(number: int, sentence: str, *, phase: str = "Get Ready") -> str:
    return f"""<main class='assessment-shell'>
      <header><h1>Read and Speak</h1><span>Section A</span></header>
      <div class='progress'>Question {number} of 5</div>
      <section class='prompt-card'>
        <p class='instruction'>Read the given sentence out loud.</p>
        <div class='sentence'>{sentence}</div>
        <div class='phase'>{phase}</div>
      </section>
      <button>Submit Answer</button>
    </main>"""


class DynamicReadAloudTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.origin = "https://assessment.example"
        self.token = "d" * 32
        self.controller = _Controller()
        self.server = make_server(
            self.controller, token=self.token, allowed_origin=self.origin)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.engine = await async_playwright().start()
        self.browser = await self.engine.chromium.launch(
            headless=True, args=["--autoplay-policy=user-gesture-required"])
        self.context = await self.browser.new_context()
        await self.context.grant_permissions(["local-network-access"], origin=self.origin)
        bridge = DynamicReadAloudBridge(
            f"http://127.0.0.1:{self.server.server_port}", self.token,
            self.origin, "/assessment", "profile-15", "TP-015",
            auto_detect=self._testMethodName.startswith("test_auto_detect"),
        )
        await self.context.add_init_script(script=bridge.init_script())
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.engine.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    async def _open(self, body):
        await self.page.route(self.origin + "/assessment", lambda route: route.fulfill(
            status=200, content_type="text/html", body=body))
        await self.page.goto(self.origin + "/assessment")

    async def test_two_unknown_sentences_reuse_one_microphone_stream(self):
        first = "A completely new first sentence appears here."
        second = "The second unseen sentence is prepared in real time."
        await self._open(_question("q-101", first))
        await self.page.wait_for_function(
            "globalThis.__qaDynamicReadAloud?.status === 'armed'", timeout=3000)
        await self.page.click("[data-qa-read-aloud-question]")
        await self.page.evaluate("""async () => {
          const media = await navigator.mediaDevices.getUserMedia({audio: true});
          const context = new AudioContext();
          const analyser = context.createAnalyser();
          analyser.fftSize = 2048;
          context.createMediaStreamSource(media).connect(analyser);
          globalThis.__qaTestMedia = media;
          globalThis.__qaTestAnalyser = analyser;
        }""")

        async def record_peak():
            return await self.page.evaluate("""async () => {
              const samples = new Float32Array(__qaTestAnalyser.fftSize);
              let peak = 0;
              const deadline = performance.now() + 180;
              while (performance.now() < deadline) {
                await new Promise(resolve => setTimeout(resolve, 5));
                __qaTestAnalyser.getFloatTimeDomainData(samples);
                for (const value of samples) peak = Math.max(peak, Math.abs(value));
              }
              return peak;
            }""")

        await self.page.locator("[data-qa-read-aloud-phase]").evaluate(
            "node => node.textContent = 'Speak Now'")
        self.assertGreater(await record_peak(), 0.05)
        await self.page.wait_for_timeout(300)

        await self.page.locator("[data-qa-read-aloud-question]").evaluate(
            """(root, data) => {
              root.setAttribute('data-question-id', data.id);
              root.querySelector('[data-qa-read-aloud-text]').textContent = data.sentence;
              root.querySelector('[data-qa-read-aloud-phase]').textContent = 'Prepare';
            }""", {"id": "q-102", "sentence": second})
        await self.page.wait_for_function(
            "globalThis.__qaDynamicReadAloud?.prepareCount === 2 && "
            "globalThis.__qaDynamicReadAloud?.status === 'armed'", timeout=3000)
        await self.page.locator("[data-qa-read-aloud-phase]").evaluate(
            "node => node.textContent = 'Speak Now'")
        self.assertGreater(await record_peak(), 0.05)

        result = await self.page.evaluate("""() => ({
          status: __qaDynamicReadAloud.status,
          failures: __qaDynamicReadAloud.failures,
          prepareCount: __qaDynamicReadAloud.prepareCount,
          audioCount: __qaDynamicReadAloud.audioCount,
          replayCount: __qaDynamicReadAloud.replayCount,
          graphCreations: __qaDynamicReadAloud.graphCreations,
          epoch: __qaDynamicReadAloud.epoch,
          trackState: __qaTestMedia.getAudioTracks()[0].readyState,
          startDelay: __qaDynamicReadAloud.replayStartedAt -
                      __qaDynamicReadAloud.recordingSeenAt
        })""")
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["prepareCount"], 2)
        self.assertEqual(result["audioCount"], 2)
        self.assertEqual(result["replayCount"], 2)
        self.assertEqual(result["graphCreations"], 1)
        self.assertEqual(result["epoch"], 2)
        self.assertEqual(result["trackState"], "live")
        self.assertLess(result["startDelay"], 75)
        self.assertEqual([item["question"] for item in self.controller.prepared],
                         [first, second])
        self.assertTrue(all(item["answer"] == item["question"]
                            for item in self.controller.prepared))
        self.assertEqual(self.controller.audio_requests, self.controller.prepared)

    async def test_two_visible_question_roots_fail_closed(self):
        await self._open(_question("q-1", "First visible sentence.") +
                         _question("q-2", "Second visible sentence."))
        await self.page.wait_for_function(
            "globalThis.__qaDynamicReadAloud?.failures.includes('ambiguous_question_root')")
        result = await self.page.evaluate("""() => ({
          status: __qaDynamicReadAloud.status,
          prepareCount: __qaDynamicReadAloud.prepareCount,
          replayCount: __qaDynamicReadAloud.replayCount
        })""")
        self.assertEqual(result, {"status": "blocked", "prepareCount": 0,
                                  "replayCount": 0})
        error = await self.page.evaluate("""async () => {
          try { await navigator.mediaDevices.getUserMedia({audio: true}); return null; }
          catch (error) { return {name: error.name, message: error.message}; }
        }""")
        self.assertEqual(error["name"], "NotReadableError")
        self.assertIn("ambiguous", error["message"])
        self.assertEqual(self.controller.prepared, [])

    async def test_recording_window_blocks_instead_of_starting_late(self):
        self.controller.delay_seconds = 0.25
        await self._open(_question(
            "q-slow", "This sentence deliberately takes too long to prepare."))
        await self.page.wait_for_function(
            "globalThis.__qaDynamicReadAloud?.status === 'preparing'", timeout=3000)
        await self.page.locator("[data-qa-read-aloud-phase]").evaluate(
            "node => node.textContent = 'Speak Now'")
        await self.page.wait_for_function(
            "__qaDynamicReadAloud.failures.includes('recording_unprepared')",
            timeout=3000)
        await self.page.wait_for_timeout(350)
        result = await self.page.evaluate("""() => ({
          status: __qaDynamicReadAloud.status,
          unprepared: __qaDynamicReadAloud.recordingUnpreparedCount,
          replayCount: __qaDynamicReadAloud.replayCount,
          audioCount: __qaDynamicReadAloud.audioCount,
          failures: __qaDynamicReadAloud.failures,
        })""")
        self.assertEqual(result, {
            "status": "blocked",
            "unprepared": 1,
            "replayCount": 0,
            "audioCount": 0,
            "failures": ["recording_unprepared"],
        })

    async def test_auto_detect_two_screenshot_shaped_unknown_sentences(self):
        first = "Every customer deserves a clear and thoughtful response."
        second = "Please review the account details before confirming payment."
        await self._open(_screenshot_question(1, first))
        await self.page.wait_for_function(
            "globalThis.__qaDynamicReadAloud?.status === 'armed'", timeout=3000)
        await self.page.click("main")
        await self.page.evaluate(
            "globalThis.__autoMedia = navigator.mediaDevices.getUserMedia({audio:true})")
        await self.page.locator(".phase").evaluate("node => node.textContent = 'Speak Now'")
        await self.page.wait_for_function("__qaDynamicReadAloud.replayCount === 1")
        await self.page.wait_for_timeout(300)
        await self.page.locator("main").evaluate("""(root, data) => {
          root.querySelector('.progress').textContent = 'Question 2 of 5';
          root.querySelector('.sentence').textContent = data;
          root.querySelector('.phase').textContent = 'Get Ready';
        }""", second)
        await self.page.wait_for_function(
            "__qaDynamicReadAloud.prepareCount === 2 && __qaDynamicReadAloud.status === 'armed'",
            timeout=3000)
        await self.page.locator(".phase").evaluate("node => node.textContent = 'Recording'")
        await self.page.wait_for_function("__qaDynamicReadAloud.replayCount === 2")
        result = await self.page.evaluate("""async () => {
          const media = await __autoMedia;
          return {failures: __qaDynamicReadAloud.failures,
                  prepareCount: __qaDynamicReadAloud.prepareCount,
                  audioCount: __qaDynamicReadAloud.audioCount,
                  replayCount: __qaDynamicReadAloud.replayCount,
                  graphCreations: __qaDynamicReadAloud.graphCreations,
                  epoch: __qaDynamicReadAloud.epoch,
                  trackState: media.getAudioTracks()[0].readyState};
        }""")
        self.assertEqual(result, {
            "failures": [], "prepareCount": 2, "audioCount": 2,
            "replayCount": 2, "graphCreations": 1, "epoch": 2,
            "trackState": "live",
        })
        self.assertEqual([item["question"] for item in self.controller.prepared],
                         [first, second])
        self.assertTrue(all(item["answer"] == item["question"]
                            for item in self.controller.prepared))
        self.assertNotEqual(self.controller.prepared[0]["source_question"],
                            self.controller.prepared[1]["source_question"])

    async def test_auto_detect_ignores_non_question_page_then_arms(self):
        sentence = "Please review the account details before confirming payment."
        await self._open(
            "<main><h1>Candidate Online Assessment Data Protection Notice</h1>"
            "<p>This notice contains several paragraphs of unrelated explanatory text.</p>"
            "<p>Nothing on this page is a spoken assessment question.</p></main>")
        await self.page.wait_for_timeout(120)
        idle = await self.page.evaluate("""() => ({
          status: __qaDynamicReadAloud.status,
          failures: __qaDynamicReadAloud.failures,
          prepareCount: __qaDynamicReadAloud.prepareCount,
        })""")
        self.assertEqual(idle, {"status": "idle", "failures": [], "prepareCount": 0})

        await self.page.locator("body").evaluate(
            "(body, html) => body.innerHTML = html", _screenshot_question(1, sentence))
        await self.page.wait_for_function(
            "__qaDynamicReadAloud.status === 'armed'", timeout=3000)
        result = await self.page.evaluate("""() => ({
          status: __qaDynamicReadAloud.status,
          failures: __qaDynamicReadAloud.failures,
          prepareCount: __qaDynamicReadAloud.prepareCount,
        })""")
        self.assertEqual(result, {"status": "armed", "failures": [], "prepareCount": 1})

    async def test_auto_detect_two_equal_candidates_block(self):
        body = _screenshot_question(1, "The first plausible sentence has enough words.")
        body = body.replace(
            "<div class='sentence'>The first plausible sentence has enough words.</div>",
            "<div class='sentence'>The first plausible sentence has enough words.</div>"
            "<p class='duplicate'>Another plausible sentence also has enough words.</p>")
        await self._open(body)
        await self.page.wait_for_function(
            "__qaDynamicReadAloud.failures.includes('ambiguous_sentence')")
        result = await self.page.evaluate("""() => ({
          prepareCount: __qaDynamicReadAloud.prepareCount,
          replayCount: __qaDynamicReadAloud.replayCount,
          status: __qaDynamicReadAloud.status
        })""")
        self.assertEqual(result, {"prepareCount": 0, "replayCount": 0,
                                  "status": "blocked"})
        self.assertEqual(self.controller.prepared, [])

    def test_configuration_is_restricted(self):
        values = dict(
            bridge_url="http://127.0.0.1:8769", token="x" * 24,
            allowed_origin="https://assessment.example", allowed_path_prefix="/assessment",
            source_profile="profile", source_test="TP-015",
        )
        with self.assertRaises(ValueError):
            DynamicReadAloudBridge(**{**values, "allowed_origin": "http://assessment.example"})
        with self.assertRaises(ValueError):
            DynamicReadAloudBridge(**{**values, "stable_observations": 1})
