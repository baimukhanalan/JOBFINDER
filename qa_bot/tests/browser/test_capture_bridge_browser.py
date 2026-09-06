import json
import tempfile
import threading
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

from qa_bot.audio.capture_bridge import make_server


class CaptureBridgeBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_secure_assessment_page_can_store_audio_on_loopback(self):
        origin = "https://assessment.example"
        token = "b" * 32
        with tempfile.TemporaryDirectory() as directory:
            server = make_server(Path(directory), token=token, allowed_origin=origin)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            engine = await async_playwright().start()
            browser = await engine.chromium.launch(headless=True)
            context = await browser.new_context()
            await context.grant_permissions(["local-network-access"], origin=origin)
            page = await context.new_page()
            try:
                await page.route(origin + "/", lambda route: route.fulfill(
                    status=200, content_type="text/html", body="<h1>Authorized fixture</h1>"
                ))
                await page.goto(origin + "/")
                result = await page.evaluate(
                    """async ({url, token}) => {
                        const response = await fetch(url, {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/octet-stream',
                                'X-Capture-Token': token,
                                'X-Capture-Name': 'browser-audio.mp3'
                            },
                            body: new Uint8Array([73, 68, 51, 1, 2, 3])
                        });
                        return {status: response.status, body: await response.json()};
                    }""",
                    {"url": f"http://127.0.0.1:{server.server_port}/capture", "token": token},
                )
                self.assertEqual(result["status"], 201)
                self.assertEqual(result["body"]["bytes"], 6)
                self.assertEqual((Path(directory) / "browser-audio.mp3").read_bytes(),
                                 b"ID3\x01\x02\x03")
            finally:
                await context.close()
                await browser.close()
                await engine.stop()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
