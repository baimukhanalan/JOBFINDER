import tempfile
import unittest
from pathlib import Path
from playwright.async_api import async_playwright
from qa_bot.live_session import Session


class NativeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_shadow_controls_and_ambiguous_or_disabled_rejection(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('''<div id="host"></div><script>
                    const root=document.querySelector('#host').attachShadow({mode:'closed'});
                    root.innerHTML='<label><input type="radio" name="answer">Correct choice</label><button aria-disabled="true">Blocked</button><button>Duplicate</button><button>Duplicate</button><button id="submit">SUBMIT ANSWER</button>';
                    root.querySelector('#submit').onclick=()=>globalThis.submitted=true;
                </script>''')
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory))
                    session.cdp=await page.context.new_cdp_session(page)
                    await session.click('radio','Correct choice')
                    nodes=await session.controls('radio')
                    self.assertTrue(any(p['name']=='checked' and p['value']['value']=='true' for p in nodes[0]['properties']))
                    for label in ('Blocked','Duplicate','Missing'):
                        with self.assertRaises(ValueError):await session.click('button',label)
                    self.assertFalse(await page.evaluate('globalThis.submitted===true'))
                    await session.click('button','SUBMIT ANSWER')
                    self.assertTrue(await page.evaluate('globalThis.submitted===true'))
            finally:await browser.close()

    def test_timer_removal_preserves_option_numbers(self):
        before='Remaining Time :\n00:20\nHow many?\n20\n30'
        after='Remaining Time :\n00:19\nHow many?\n20\n30'
        self.assertEqual(Session.stable_text(before),Session.stable_text(after))
        self.assertIn('20\n30',Session.stable_text(before))

    def test_topic_is_ready_during_prompt_audio_without_timer_in_cache_key(self):
        base='Section D: Free Speech\nSpeak on the topic provided to you.\nYour topic is: Describe a playground.\n'
        for phase in ('Listen Carefully','Get Ready\n30','Speak Now\n45'):
            self.assertEqual(Session.topic_prompt({'text':base+phase}),'Your topic is: Describe a playground.')
        self.assertIsNone(Session.topic_prompt({'text':'Section D: Free Speech\nIn this section you will receive a topic.'}))
