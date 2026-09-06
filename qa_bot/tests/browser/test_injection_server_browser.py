import threading
import unittest

from playwright.async_api import async_playwright

from qa_bot.audio.injection_server import make_server


class InjectionServerBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_https_origin_fetches_and_runs_prepared_script(self):
        origin = "https://assessment.example"
        token = "b" * 32
        script = "globalThis.__preparedInjection = {ready: true};"
        server = make_server(script, token=token, allowed_origin=origin)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        engine = await async_playwright().start()
        browser = await engine.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.grant_permissions(["local-network-access"], origin=origin)
        page = await context.new_page()
        try:
            await page.route(origin + "/", lambda route: route.fulfill(
                status=200, content_type="text/html", body="<main>fixture</main>"
            ))
            await page.goto(origin + "/")
            result = await page.evaluate(
                """async ({url, token}) => {
                  const response = await fetch(url, {
                    method: 'GET', cache: 'no-store', credentials: 'omit',
                    headers: {'X-Injection-Token': token}
                  });
                  const source = await response.text();
                  const node = document.createElement('script');
                  node.textContent = source;
                  document.documentElement.append(node);
                  node.remove();
                  return {status: response.status,
                          ready: globalThis.__preparedInjection?.ready === true,
                          digest: response.headers.get('X-Injection-SHA256')};
                }""",
                {"url": f"http://127.0.0.1:{server.server_port}/injection.js",
                 "token": token},
            )
            self.assertEqual(result["status"], 200)
            self.assertTrue(result["ready"])
            self.assertRegex(result["digest"], r"^[a-f0-9]{64}$")
        finally:
            await context.close()
            await browser.close()
            await engine.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    async def test_browser_cannot_fetch_from_wrong_origin_host_or_path(self):
        origin = "https://assessment.example"
        foreign_origin = "https://other.example"
        token = "c" * 32
        server = make_server(
            "globalThis.__mustNotRun = true;", token=token,
            allowed_origin=origin,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        engine = await async_playwright().start()
        browser = await engine.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.grant_permissions(["local-network-access"], origin=origin)
        await context.grant_permissions(["local-network-access"], origin=foreign_origin)
        page = await context.new_page()
        try:
            for page_origin in (origin, foreign_origin):
                await page.route(page_origin + "/", lambda route: route.fulfill(
                    status=200, content_type="text/html", body="<main>fixture</main>"
                ))

            async def rejected(page_origin, target):
                await page.goto(page_origin + "/")
                return await page.evaluate(
                    """async ({url, token}) => {
                      try {
                        const response = await fetch(url, {
                          method: 'GET', cache: 'no-store', credentials: 'omit',
                          headers: {'X-Injection-Token': token}
                        });
                        return {fetched: true, status: response.status};
                      } catch (error) {
                        return {fetched: false, name: error.name};
                      }
                    }""", {"url": target, "token": token})

            port = server.server_port
            cases = (
                (foreign_origin, f"http://127.0.0.1:{port}/injection.js"),
                (origin, f"http://localhost:{port}/injection.js"),
                (origin, f"http://127.0.0.1:{port}/other.js"),
            )
            for page_origin, target in cases:
                with self.subTest(page_origin=page_origin, target=target):
                    result = await rejected(page_origin, target)
                    self.assertFalse(result["fetched"])
                    self.assertEqual(result["name"], "TypeError")
            self.assertIsNone(await page.evaluate("globalThis.__mustNotRun"))
        finally:
            await context.close()
            await browser.close()
            await engine.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
