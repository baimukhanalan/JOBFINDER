import threading
import unittest

from playwright.async_api import async_playwright

from qa_bot.audio.shared_microphone import SharedMicrophoneBridge
from qa_bot.audio.dynamic_read_aloud import DynamicReadAloudBridge
from qa_bot.audio.direct_loopback import DirectAudioLoopback
from qa_bot.audio.live_speech_bridge import make_server
from tests.browser.test_dynamic_read_aloud import _Controller


class SharedMicrophoneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.origin = "https://assessment.example"
        self.controller = _Controller()
        self.server = make_server(self.controller, token="t" * 32, allowed_origin=self.origin)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.engine = await async_playwright().start()
        self.browser = await self.engine.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        await self.context.grant_permissions(["local-network-access"], origin=self.origin)
        read = DynamicReadAloudBridge(
            f"http://127.0.0.1:{self.server.server_port}", "t" * 32, self.origin,
            "/", "synthetic", "shared-stream", auto_detect=True,
            question_counter_selector="button.currentQue",
            suspension_selector='[role="dialog"]',
        )
        loop = DirectAudioLoopback(
            allowed_hosts=("audio.example",), path_markers=("/SpeechAssessmentBank/",),
            section_heading="Section B: Listen and Repeat",
        )
        script = (SharedMicrophoneBridge(self.origin).init_script() + "\n"
                  + loop.init_script() + "\n" + read.init_script())
        await self.context.add_init_script(script=script)
        self.page = await self.context.new_page()
        await self.page.route(self.origin + "/", lambda route: route.fulfill(
            body="<button id='start'>Start device test</button><main></main>",
            content_type="text/html"))
        await self.page.route("https://audio.example/**", lambda route: route.fulfill(
            body=self.controller.wav, content_type="audio/wav"))
        await self.page.goto(self.origin + "/")
        await self.page.click("#start")

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.engine.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    async def test_stream_opened_before_question_receives_read_and_repeat_audio(self):
        await self.page.evaluate("""async () => {
          globalThis.earlyStream = await navigator.mediaDevices.getUserMedia({audio:true});
          document.querySelector('main').innerHTML = `<h1>Section A: Read and Speak</h1>
            <button class="currentQue">1</button>
            <p>Read the given sentence out loud.</p>
            <p>Every customer deserves a clear and thoughtful response.</p>
            <p class="phase">Get Ready</p>
            <audio src="https://audio.example/SpeechAssessmentBank/repeat.wav"></audio>`;
        }""")
        await self.page.wait_for_function("__qaDynamicReadAloud.status === 'armed'")
        await self.page.wait_for_function("__qaDirectAudioLoopback.prepared.size === 1")
        await self.page.evaluate("""() => {
          document.querySelector('main').setAttribute('aria-hidden','true');
        }""")
        await self.page.wait_for_timeout(100)
        await self.page.evaluate("""() => {
          const dialog = document.createElement('div');
          dialog.setAttribute('role','dialog'); dialog.innerHTML='<p>Did you hear the audio?</p>';
          dialog.style.opacity = '0';
          document.body.append(dialog);
        }""")
        await self.page.wait_for_timeout(100)
        self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.prepareCount'), 1)
        await self.page.evaluate("""() => {
          document.querySelector('[role="dialog"]').remove();
          document.querySelector('main').removeAttribute('aria-hidden');
        }""")
        await self.page.locator(".phase").evaluate("el => el.textContent = 'Speak Now'")
        await self.page.wait_for_function("__qaMicrophoneBus.peak > 0.01")
        result = await self.page.evaluate("""() => ({
          same: earlyStream === __qaMicrophoneBus.destination.stream,
          read: __qaDynamicReadAloud.replayCount,
          repeat: __qaDirectAudioLoopback.replayCount,
          counter: __qaDynamicReadAloud.currentSiteId,
          failures: __qaDynamicReadAloud.failures
        })""")
        self.assertEqual(result, {"same": True, "read": 1, "repeat": 0,
                                  "counter": "navigation:1", "failures": []})
        await self.page.wait_for_function("__qaDynamicReadAloud.status === 'played'")
        await self.page.evaluate("""() => {
          document.querySelector('main').innerHTML = `<h1>Section B: Listen and Repeat</h1>
            <p class="phase">Get Ready</p>
            <audio src="https://audio.example/SpeechAssessmentBank/repeat.wav"></audio>`;
        }""")
        await self.page.wait_for_function("__qaDynamicReadAloud.status === 'idle'")
        await self.page.evaluate("__qaMicrophoneBus.peak = 0")
        await self.page.locator(".phase").evaluate("el => el.textContent = 'Speak Now'")
        await self.page.wait_for_function(
            "__qaDirectAudioLoopback.replayCount === 1 && __qaMicrophoneBus.peak > 0.01")
        self.assertTrue(await self.page.evaluate(
            "earlyStream === __qaDirectAudioLoopback.destination.stream"))

    async def test_ambiguous_active_navigation_does_not_prepare_or_play(self):
        await self.page.evaluate("""() => {
          document.querySelector('main').innerHTML = `<h1>Section A: Read and Speak</h1>
            <button class="currentQue">1</button><button class="currentQue">2</button>
            <p>Read the given sentence out loud.</p>
            <p>Every customer deserves a clear and thoughtful response.</p><p>Get Ready</p>`;
        }""")
        await self.page.wait_for_function("__qaDynamicReadAloud.status === 'blocked'")
        self.assertEqual(self.controller.prepared, [])
        self.assertEqual(await self.page.evaluate("__qaDynamicReadAloud.replayCount"), 0)

    async def test_legacy_recorder_and_transient_phase_do_not_cancel_preparation(self):
        self.controller.delay_seconds = 0.2
        await self.page.evaluate("""async () => {
          document.querySelector('main').innerHTML = `<h1>Section A: Read and Speak</h1>
            <button class="currentQue">2</button>
            <p>Read the given sentence out loud.</p>
            <p>The recorder must receive the same prepared answer.</p><p class="phase">Get Ready</p>`;
          globalThis.modernStream = await navigator.mediaDevices.getUserMedia({audio:true});
          globalThis.legacyStream = await new Promise((resolve, reject) =>
            navigator.webkitGetUserMedia({audio:true}, resolve, reject));
        }""")
        await self.page.wait_for_function("__qaDynamicReadAloud.status === 'preparing'")
        await self.page.locator('.phase').evaluate("el => el.textContent = 'Speak Now'")
        await self.page.wait_for_timeout(75)
        await self.page.locator('.phase').evaluate("el => el.textContent = 'Listen Carefully'")
        await self.page.wait_for_function("__qaDynamicReadAloud.status === 'armed'")
        self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.failures'), [])
        self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.replayCount'), 0)
        await self.page.locator('.phase').evaluate("el => el.textContent = 'Speak Now'")
        await self.page.wait_for_function('__qaMicrophoneBus.peak > 0.01')
        self.assertTrue(await self.page.evaluate(
            'modernStream === legacyStream && legacyStream === __qaMicrophoneBus.destination.stream'))
        self.assertEqual(await self.page.evaluate('__qaMicrophoneBus.streamsRequested'), 2)
        self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.replayCount'), 1)
        await self.page.wait_for_function("__qaDynamicReadAloud.status === 'played'")
        self.assertFalse(await self.page.evaluate('__qaDynamicReadAloud.retryCurrent()'))
        await self.page.evaluate("""() => {
          const dialog=document.createElement('div');dialog.setAttribute('role','dialog');
          document.body.append(dialog);
        }""")
        self.assertTrue(await self.page.evaluate('__qaDynamicReadAloud.retryCurrent()'))
        self.assertFalse(await self.page.evaluate('__qaDynamicReadAloud.retryCurrent()'))
        await self.page.evaluate("document.querySelector('[role=dialog]').remove()")
        await self.page.wait_for_function('__qaDynamicReadAloud.replayCount === 2')
        self.assertEqual(len(self.controller.prepared), 1)
        self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.retryCount'), 1)


class SharedMicrophoneConfigTests(unittest.TestCase):
    def test_requires_exact_origin(self):
        for origin in ("http://assessment.example", "https://assessment.example/path",
                       "https://user:password@assessment.example", "https://assessment.example?q=1"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                SharedMicrophoneBridge(origin)
