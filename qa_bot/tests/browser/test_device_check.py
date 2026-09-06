import tempfile
from pathlib import Path
import unittest
from playwright.async_api import async_playwright
from qa_bot.live_session import Session, STATE_SCRIPT
from qa_bot.solvers.device_check import advance_device_check


class DeviceCheckTests(unittest.IsolatedAsyncioTestCase):
    async def setup_session(self, page, directory, extra=''):
        await page.set_content('''<style>button{padding:12px}.dialog{position:fixed;right:10px;top:10px;background:white;padding:20px}</style>
          <button class="currentQue">1</button><p>Click NEXT if you can hear your voice clearly</p>
          <button onclick="window.backgroundClicked=true">NEXT</button>'''+extra)
        await page.evaluate('''()=>{
          globalThis.__qaDynamicReadAloud={status:'idle',currentSiteId:'navigation:1',replayCount:1};
          globalThis.__qaMicrophoneBus={context:{state:'running'},nonzeroSamples:114};
        }''')
        session=Session(page,Path(directory));session.cdp=await page.context.new_cdp_session(page)
        return session

    async def test_duplicate_next_chooses_only_foreground_diagnostics(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                with tempfile.TemporaryDirectory() as directory:
                    page=await browser.new_page()
                    session=await self.setup_session(page,directory,'''<div class="dialog" id="ngdialog3">
                      <p>Connection</p><p>Audio</p><p>Question Audio</p>
                      <button onclick="window.diagnosticClicked=true">NEXT</button><button>TRY AGAIN</button></div>''')
                    handled=await advance_device_check(session,await page.evaluate(STATE_SCRIPT))
                    self.assertTrue(handled)
                    self.assertTrue(await page.evaluate('window.diagnosticClicked===true'))
                    self.assertFalse(await page.evaluate('window.backgroundClicked===true'))
            finally:await browser.close()

    async def test_disabled_diagnostics_next_does_not_fall_through_to_background(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                with tempfile.TemporaryDirectory() as directory:
                    page=await browser.new_page()
                    session=await self.setup_session(page,directory,'''<div class="dialog" role="dialog">
                      <p>Connection</p><p>Audio</p><p>Question Audio</p>
                      <button disabled onclick="window.diagnosticClicked=true">NEXT</button><button>TRY AGAIN</button></div>''')
                    await advance_device_check(session,await page.evaluate(STATE_SCRIPT))
                    self.assertFalse(await page.evaluate('window.backgroundClicked===true || window.diagnosticClicked===true'))
                    await page.locator('[role=dialog] button').first.evaluate('n=>n.disabled=false')
                    await advance_device_check(session,await page.evaluate(STATE_SCRIPT))
                    self.assertTrue(await page.evaluate('window.diagnosticClicked===true'))
            finally:await browser.close()

    async def test_closed_shadow_dialog_next_uses_native_input(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                with tempfile.TemporaryDirectory() as directory:
                    page=await browser.new_page()
                    session=await self.setup_session(page,directory,'''<div class="dialog" role="dialog">
                      <p>Connection</p><p>Audio</p><p>Question Audio</p><div id="host"></div><button>TRY AGAIN</button></div>''')
                    await page.evaluate("()=>{const shadow=document.querySelector('#host').attachShadow({mode:'closed'});shadow.innerHTML='<button>NEXT</button>';shadow.querySelector('button').onclick=()=>window.diagnosticClicked=true}")
                    await advance_device_check(session,await page.evaluate(STATE_SCRIPT))
                    self.assertTrue(await page.evaluate('window.diagnosticClicked===true'))
                    self.assertFalse(await page.evaluate('window.backgroundClicked===true'))
            finally:await browser.close()

    async def test_no_speech_evidence_or_preflight_cannot_advance(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                with tempfile.TemporaryDirectory() as directory:
                    page=await browser.new_page();session=await self.setup_session(page,directory)
                    state=await page.evaluate(STATE_SCRIPT);state['microphone']['signal']=0
                    await advance_device_check(session,state)
                    self.assertFalse(await page.evaluate('window.backgroundClicked===true'))
                    session.preflight=True
                    self.assertFalse(await advance_device_check(session,await page.evaluate(STATE_SCRIPT)))
                    self.assertFalse(await page.evaluate('window.backgroundClicked===true'))
            finally:await browser.close()
