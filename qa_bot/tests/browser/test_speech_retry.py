import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from playwright.async_api import async_playwright
from qa_bot.audio.dynamic_read_aloud import DynamicReadAloudBridge
from qa_bot.audio.live_speech_bridge import make_server
from qa_bot.live_session import Session, STATE_SCRIPT
from qa_bot.solvers.speech_retry import recover_read_warning
from tests.browser.test_dynamic_read_aloud import _Controller


class SpeechRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.origin='https://assessment.example'; self.controller=_Controller()
        self.server=make_server(self.controller,token='r'*32,allowed_origin=self.origin)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.temp=tempfile.TemporaryDirectory()
        self.pw=await async_playwright().start(); self.browser=await self.pw.chromium.launch(headless=True)
        self.context=await self.browser.new_context()
        await self.context.grant_permissions(['local-network-access'],origin=self.origin)
        bridge=DynamicReadAloudBridge(f'http://127.0.0.1:{self.server.server_port}','r'*32,self.origin,'/',
            'fixture','fixture',auto_detect=True,question_counter_selector='button.currentQue',
            suspension_selector='[role=dialog]',section_heading='Section A: Read and Speak')
        await self.context.add_init_script(script=bridge.init_script())
        self.page=await self.context.new_page()
        await self.page.route(self.origin+'/',lambda r:r.fulfill(content_type='text/html',body='''
          <main><h1>Section A: Read and Speak</h1><button class="currentQue">5</button>
          <p>Read the given sentence out loud.</p><p class="sentence">What lovely ambiance.</p><p class="phase">Speak Now</p></main>
          <div role="dialog"><h2>Warning</h2><p>We are unable to hear you.</p><button id="retry">TRY AGAIN</button><button>TRY LATER</button></div>
          <script>retry.onclick=()=>{window.clicked=(window.clicked||0)+1;document.querySelector('[role=dialog]').remove();document.querySelector('.phase').textContent='Get Ready';setTimeout(()=>document.querySelector('.phase').textContent='Speak Now',100)};</script>'''))
        await self.page.goto(self.origin+'/')
        self.session=Session(self.page,Path(self.temp.name));self.session.cdp=await self.context.new_cdp_session(self.page)

    async def asyncTearDown(self):
        await self.browser.close();await self.pw.stop();self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    async def test_missing_short_answer_is_prepared_behind_dialog_then_retried_once(self):
        self.assertEqual(self.controller.prepared,[])
        self.assertTrue(await recover_read_warning(self.session,await self.page.evaluate(STATE_SCRIPT)))
        await self.page.wait_for_function("__qaDynamicReadAloud.status==='played'")
        self.assertEqual(await self.page.evaluate('window.clicked'),1)
        self.assertEqual(self.controller.prepared[0]['question'],'What lovely ambiance.')
        self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.replayCount'),1)
        self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.retryCount'),1)
        await self.page.evaluate("()=>{const d=document.createElement('div');d.role='dialog';d.innerHTML='<p>We are unable to hear you.</p><button>TRY AGAIN</button>';document.body.append(d)}")
        self.assertFalse(await recover_read_warning(self.session,await self.page.evaluate(STATE_SCRIPT)))
        self.assertFalse(await self.page.evaluate('__qaDynamicReadAloud.prepareRetryCurrent()'))
        self.assertEqual(len(self.controller.prepared),1)

    async def test_session_watch_recovers_missing_audio_once_without_navigation(self):
        self.session.auto_speech=True
        self.session.auto_navigation=False
        task=asyncio.create_task(self.session.watch())
        try:
            await self.page.wait_for_function("__qaDynamicReadAloud.status==='played' && window.clicked===1",timeout=5000)
            self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.replayCount'),1)
            self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.retryCount'),1)
            self.assertEqual([x['question'] for x in self.controller.prepared],['What lovely ambiance.'])
            await self.page.evaluate("()=>{const d=document.createElement('div');d.role='dialog';d.innerHTML='<p>We are unable to hear you.</p><button>TRY AGAIN</button>';document.body.append(d)}")
            await self.page.wait_for_timeout(300)
            self.assertEqual(await self.page.evaluate('window.clicked'),1)
            self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.retryCount'),1)
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_changed_question_during_prepare_cannot_retry_stale_audio(self):
        self.controller.delay_seconds=.2
        task=asyncio.create_task(recover_read_warning(self.session,await self.page.evaluate(STATE_SCRIPT)))
        await self.page.wait_for_function("__qaDynamicReadAloud.status==='preparing'")
        await self.page.evaluate("()=>{document.querySelector('.currentQue').textContent='6';document.querySelector('.sentence').textContent='Good morning.'}")
        self.assertFalse(await task)
        self.assertFalse(await self.page.evaluate('window.clicked===1'))
        self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.replayCount'),0)
