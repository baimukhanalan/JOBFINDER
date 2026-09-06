import tempfile
import unittest
from pathlib import Path
from playwright.async_api import async_playwright
from qa_bot.live_session import Session


class NativeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_disables_all_automatic_answers_and_navigation(self):
        import asyncio
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page();await page.set_content('Assessment Time out')
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory));session.cdp=await page.context.new_cdp_session(page)
                    session.auto_navigation=session.auto_choices=session.auto_speech=True
                    task=asyncio.create_task(session.watch())
                    await asyncio.sleep(.25);task.cancel();await asyncio.gather(task,return_exceptions=True)
                    self.assertTrue(session.timeout_seen)
                    self.assertFalse(session.auto_navigation or session.auto_choices or session.auto_speech)
                    self.assertIn('assessment_time_out',(Path(directory)/'run-failures.jsonl').read_text())
            finally:await browser.close()

    async def test_notice_does_not_uncheck_already_acknowledged_checkbox(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page();await page.set_content('<label><input checked type="checkbox">I confirm I have read and understood this Notice.</label><button>Continue</button>')
                await page.evaluate("document.querySelector('button').onclick=()=>window.accepted=document.querySelector('input').checked")
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory));session.cdp=await page.context.new_cdp_session(page)
                    await session.command({'action':'notice'})
                    self.assertTrue(await page.evaluate('accepted'))
            finally:await browser.close()

    async def test_preflight_cannot_enable_answer_automation(self):
        with tempfile.TemporaryDirectory() as directory:
            session=Session(None,Path(directory));session.preflight=True
            for action in ('auto_speech','auto_choices','auto_modules','retry_read'):
                with self.assertRaisesRegex(ValueError,'preflight'):
                    await session.command({'action':action})
            self.assertFalse(session.auto_speech)
            self.assertFalse(session.auto_choices)

    async def test_closed_shadow_hidden_radio_is_selected_by_native_label(self):
        from qa_bot.solvers.analytical import options,click_node,label_info
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('''<div id="host"></div><script>
                  const root=host.attachShadow({mode:'closed'});
                  root.innerHTML='<div aria-selected="false"><input style="display:none" type="radio" id="a"><label tabindex="0" for="a">A safe fixture</label></div>';
                  </script>''')
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory));session.cdp=await page.context.new_cdp_session(page)
                    opts=await options(session);self.assertEqual(len(opts),1)
                    self.assertEqual(opts[0]['text'],'A safe fixture')
                    self.assertFalse(opts[0]['checked'])
                    await click_node(session,opts[0]['node'])
                    self.assertTrue((await label_info(session,opts[0]['node']))['checked'])
            finally:await browser.close()

    async def test_scale_selects_exact_option_and_rejects_changed_question(self):
        from types import SimpleNamespace
        from qa_bot.solvers.personality import run_personality
        from qa_bot.live_session import STATE_SCRIPT
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('''<div class="question">A synthetic preference.</div>
                  <a class="currentQue">12<span style="display:none">Tooltip</span></a>
                  <div id="options"></div><button id="next">NEXT</button>
                  <script>
                  const labels=['Strongly Disagree','Disagree','Neither agree nor disagree','Agree','Strongly Agree'];
                  options.innerHTML=labels.map((x,i)=>`<label class="radio-outer" id="label${i}" title="${x}"><input style="display:none" id="input${i}" type="radio" name="answer">${i}</label>`).join('');
                  next.onclick=()=>window.submitted=true;
                  </script>''')
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory));session.cdp=await page.context.new_cdp_session(page)
                    proposal=SimpleNamespace(selections=[SimpleNamespace(option_id='option-3')])
                    session.historical=SimpleNamespace(lookup=lambda q:SimpleNamespace(proposal=proposal,source_ids=('fixture',)))
                    state=await page.evaluate(STATE_SCRIPT);self.assertEqual(state['number'],'12')
                    await run_personality(session,state)
                    self.assertTrue(await page.locator('#input2').is_checked())
                    self.assertTrue(await page.evaluate('submitted'))
                    await page.evaluate("window.submitted=false;document.querySelector('.currentQue').innerText='13'")
                    await run_personality(session,state)
                    self.assertFalse(await page.evaluate('submitted'))
            finally:await browser.close()

    async def test_typing_uses_keyboard_events_and_preserves_punctuation(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('''<div>Type the given sentence EXACTLY as shown in the space provided.</div>
                    <div>"Good morning," she said.</div><div>00 : 55 Time Left</div>
                    <textarea class="typingTextArea"></textarea><a href="#" id="submit">SUBMIT ANSWER</a>
                    <a class="currentQue">1</a><script>
                    window.keys=0;window.pastes=0;document.querySelector('textarea').onkeydown=()=>window.keys++;
                    document.querySelector('textarea').onpaste=e=>{window.pastes++;e.preventDefault()};
                    document.querySelector('#submit').onclick=e=>{e.preventDefault();window.submitted=true};</script>''')
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory));session.cdp=await page.context.new_cdp_session(page)
                    from qa_bot.live_session import STATE_SCRIPT
                    state=await page.evaluate(STATE_SCRIPT);passage=session.typing_passage(state)
                    self.assertEqual(passage,'"Good morning," she said.')
                    await session.type_passage(state,passage)
                    self.assertEqual(await page.locator('textarea').input_value(),passage)
                    self.assertTrue(await page.evaluate('submitted'))
                    self.assertGreater(await page.evaluate('keys'),0)
                    self.assertEqual(await page.evaluate('pastes'),0)
            finally:await browser.close()

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
