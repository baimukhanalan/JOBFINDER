import unittest
import io
import math
import tempfile
import wave
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from qa_bot.adapters.browser.controller import BrowserController
from qa_bot.adapters.browser.staging import StagingBrowser
from qa_bot.execution.executor import ActionExecutor
from qa_bot.domain.answer import AnswerProposal, Selection
from qa_bot.knowledge.bank import QuestionBank
from qa_bot.solvers.engine import AnswerEngine
from qa_bot.orchestration.answer_pipeline import AnswerPipeline
from qa_bot.audio.fake_microphone import ChromiumFakeMicrophone
from unittest.mock import AsyncMock


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_sandbox_consent_toggle_enables_continue_and_navigates_once(self):
        fixture_root = Path(__file__).resolve().parents[1] / "fixtures/screens"
        consent = (fixture_root / "consent.html").read_text(encoding="utf-8")
        complete = (fixture_root / "consent-complete.html").read_text(encoding="utf-8")

        async def route(controller, request):
            body = complete if request.request.url.endswith("consent-complete.html") else consent
            await request.fulfill(status=200, content_type="text/html", body=body)

        with patch.object(BrowserController, "_route", route):
            controller = BrowserController(("https://staging.example.invalid",))
            try:
                await controller.open("https://staging.example.invalid/consent.html")
                port = StagingBrowser(controller, ("https://staging.example.invalid",))
                await port.accept_sandbox_consent()
                self.assertTrue(controller._page.url.endswith("/consent-complete.html"))
                counts = await controller._page.evaluate("""() => ({
                    toggle: sessionStorage.getItem('qaConsentToggleClicks'),
                    proceed: sessionStorage.getItem('qaConsentContinueClicks')
                })""")
                self.assertEqual(counts, {"toggle": "1", "proceed": "1"})
            finally:
                await controller.close()

    async def test_chromium_receives_staged_audio_as_microphone(self):
        stream = io.BytesIO()
        with wave.open(stream, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            frames = bytearray()
            for index in range(32000):
                sample = int(12000 * math.sin(2 * math.pi * 440 * index / 16000))
                frames.extend(sample.to_bytes(2, "little", signed=True))
            audio.writeframes(frames)
        with tempfile.TemporaryDirectory() as directory:
            microphone = ChromiumFakeMicrophone(Path(directory) / "answer.wav")
            microphone.stage(stream.getvalue())
            html = "<!doctype html><title>synthetic microphone</title><p>ready</p>"

            async def route(controller, request):
                await request.fulfill(status=200, content_type="text/html", body=html)

            with patch.object(BrowserController, "_route", route):
                controller = BrowserController(
                    ("https://staging.example.invalid",),
                    launch_args=microphone.launch_args(),
                )
                try:
                    await controller.open("https://staging.example.invalid")
                    peak = await controller._page.evaluate("""async () => {
                        const media = await navigator.mediaDevices.getUserMedia({audio: true});
                        const context = new AudioContext();
                        const analyser = context.createAnalyser();
                        analyser.fftSize = 2048;
                        context.createMediaStreamSource(media).connect(analyser);
                        const samples = new Float32Array(analyser.fftSize);
                        let peak = 0;
                        for (let pass = 0; pass < 20; pass++) {
                            await new Promise(resolve => setTimeout(resolve, 50));
                            analyser.getFloatTimeDomainData(samples);
                            for (const value of samples) peak = Math.max(peak, Math.abs(value));
                        }
                        media.getTracks().forEach(track => track.stop());
                        await context.close();
                        return peak;
                    }""")
                    self.assertGreater(peak, 0.05)
                finally:
                    await controller.close()

    async def test_real_chromium_select_confirm_and_submit_block(self):
        path = Path(__file__).resolve().parents[1] / "fixtures/questions/ANA-01.html"
        html = path.read_text(encoding="utf-8").replace("<main ", '<main data-qa-environment="staging" ')
        async def route(controller, request):
            await request.fulfill(status=200, content_type="text/html", body=html)
        with patch.object(BrowserController, "_route", route):
            controller = BrowserController(("https://staging.example.invalid",))
            try:
                await controller.open("https://staging.example.invalid")
                port = StagingBrowser(controller, ("https://staging.example.invalid",))
                q = await port.current()
                a = AnswerProposal(q.question_id, q.content_hash, q.response_contract.kind,
                                   0.99, selections=(Selection("o2"),))
                executor = ActionExecutor(port)
                self.assertEqual((await executor.apply(q, a)).status, "applied")
                self.assertEqual(await port.selected(), ["o2"])
                self.assertEqual((await executor.navigate(q, "submit")).status, "blocked")
                self.assertEqual((await executor.apply(q, replace(a, question_id="old"))).status, "blocked")
                with QuestionBank(":memory:") as bank:
                    bank.save_candidate(q, a)
                    bank.approve(q, reviewer="test-human", reason="synthetic expected selection")
                    model = AsyncMock()
                    pipeline = AnswerPipeline(AnswerEngine(bank, model), executor=executor)
                    result = await pipeline.process(q, synthetic=True, apply=True)
                    self.assertEqual(result.status, "applied")
                    model.complete.assert_not_called()
            finally:
                await controller.close()

    async def test_production_origin_never_staging(self):
        with self.assertRaises(ValueError):
            StagingBrowser(None, ("https://amcatglobal.aspiringminds.com",))

    async def test_fill_text_confirmed(self):
        path = Path(__file__).resolve().parents[1] / "fixtures/questions/WRITEX-01.html"
        html = path.read_text(encoding="utf-8").replace("<main ", '<main data-qa-environment="staging" ')
        async def route(controller, request):
            await request.fulfill(status=200, content_type="text/html", body=html)
        with patch.object(BrowserController, "_route", route):
            controller = BrowserController(("https://staging.example.invalid",))
            try:
                await controller.open("https://staging.example.invalid")
                port = StagingBrowser(controller, ("https://staging.example.invalid",))
                q = await port.current()
                text = "synthetic " * 35
                a = AnswerProposal(q.question_id, q.content_hash, q.response_contract.kind, 0.99, text=text)
                self.assertEqual((await ActionExecutor(port).apply(q, a)).status, "applied")
                self.assertEqual(await port.value(), text)
            finally:
                await controller.close()
