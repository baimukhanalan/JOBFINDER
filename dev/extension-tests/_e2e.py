"""E2E: load the unpacked extension in real Chromium, fill a test form, assert results."""
import asyncio
import http.server
import os
import socket
import threading

from playwright.async_api import async_playwright

EXT = os.path.dirname(os.path.abspath(__file__))


def _serve(directory):
    free = socket.socket(); free.bind(("127.0.0.1", 0)); port = free.getsockname()[1]; free.close()
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=directory, **k)
    httpd = http.server.HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


async def main():
    httpd, port = _serve(EXT)
    url = f"http://127.0.0.1:{port}/_test_form.html"
    os.environ["DISPLAY"] = ":99"
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            "", headless=False,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}",
                  "--no-sandbox", "--no-first-run"],
            env={**os.environ, "DISPLAY": ":99"},
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until="load")
        await page.wait_for_timeout(1200)  # let the content script inject

        # --- 1) deterministic fill (trigger via DOM event → content script) ---
        await page.evaluate("window.dispatchEvent(new CustomEvent('__applyAssistFill'))")
        await page.wait_for_timeout(1500)

        vals = await page.evaluate("""() => ({
            fn: document.getElementById('fn').value,
            ln: document.getElementById('ln').value,
            em: document.getElementById('em').value,
            ph: document.getElementById('ph').value,
            co: document.getElementById('co').value,
            yrs: document.getElementById('yrs').value,
            auth_us: (document.querySelector('input[name=auth_us]:checked')||{}).value || '',
            auth_ca: (document.querySelector('input[name=auth_ca]:checked')||{}).value || '(none)',
            sponsor: (document.querySelector('input[name=sponsor]:checked')||{}).value || '',
            age18: (document.querySelector('input[name=age18]:checked')||{}).value || '',
            agree: document.querySelector('input[name=agree]').checked,
        })""")

        # --- 2) LLM/cache draft for open-ended ---
        await page.evaluate("window.dispatchEvent(new CustomEvent('__applyAssistDraft'))")
        await page.wait_for_timeout(35000)  # allow LLM round-trip (cache is instant)
        areas = await page.evaluate("""() => ({
            q1: document.getElementById('q1').value,
            q2: document.getElementById('q2').value,
            q3: document.getElementById('q3').value,
        })""")

        await ctx.close()
    httpd.shutdown()

    # --- assertions ---
    checks = [
        ("first name", vals["fn"] == "Michael"),
        ("last name", vals["ln"] == "Heck"),
        ("email", vals["em"] == "michaelheck@amaskills.com"),
        ("phone", "512" in vals["ph"]),
        ("country select = United States", vals["co"] == "United States"),
        ("years = 15", vals["yrs"] == "15"),
        ("US work-auth = yes", vals["auth_us"] == "yes"),
        ("CANADA work-auth LEFT BLANK (guard)", vals["auth_ca"] == "(none)"),
        ("sponsorship = no", vals["sponsor"] == "no"),
        ("18+ = yes", vals["age18"] == "yes"),
        ("agree checkbox checked", vals["agree"] is True),
        ("why-textarea drafted", len(areas["q1"]) > 30),
        ("difficult-customer drafted", len(areas["q2"]) > 30),
        ("anything-else drafted", len(areas["q3"]) > 10),
    ]
    print("\n=== E2E RESULTS ===")
    ok = 0
    for name, passed in checks:
        print(("  PASS " if passed else "  FAIL ") + name)
        ok += bool(passed)
    print(f"\n{ok}/{len(checks)} passed")
    print("\n--- filled values ---")
    for k, v in {**vals, **areas}.items():
        print(f"  {k}: {str(v)[:70]}")


asyncio.run(main())
