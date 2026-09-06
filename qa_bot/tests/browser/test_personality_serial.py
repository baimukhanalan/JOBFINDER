import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from playwright.async_api import async_playwright
from qa_bot.live_session import Session, STATE_SCRIPT
from qa_bot.solvers.personality import INSTRUCTION, run_personality


class PersonalitySerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_watch_keeps_one_task_through_native_next_and_transition(self):
        class SlowReturnSession(Session):
            active=0
            peak=0
            async def click(self,role,name):
                if name!='NEXT':return await super().click(role,name)
                self.active+=1;self.peak=max(self.peak,self.active)
                try:
                    await super().click(role,name)
                    # DOM advances while the previous native dispatch is in flight.
                    await asyncio.sleep(.35)
                finally:self.active-=1
        with tempfile.TemporaryDirectory() as directory:
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(headless=True)
                try:
                    page=await browser.new_page()
                    await page.set_content(f'<div>{INSTRUCTION}</div><div class="question">Synthetic 1</div><a class="currentQue">1</a><div id="options"></div><button>NEXT</button>')
                    await page.evaluate('''() => {
                        window.sent=[];
                        const labels=['Strongly Disagree','Disagree','Neither agree nor disagree','Agree','Strongly Agree'];
                        options.innerHTML=labels.map((x,i)=>`<label class="radio-outer" id="label${i}" title="${x}"><input id="input${i}" type="radio" name="answer">${i}</label>`).join('');
                        document.querySelector('button').onclick=()=>{
                            const n=Number(document.querySelector('.currentQue').innerText);
                            sent.push({number:n,selected:[...document.querySelectorAll('input')].findIndex(x=>x.checked)});
                            if(n===6){document.body.innerHTML='Fixture complete';return;}
                            document.querySelector('.currentQue').innerText=String(n+1);
                            document.querySelector('.question').innerText='Synthetic '+(n+1);
                            document.querySelectorAll('input').forEach(x=>x.checked=false);
                        };
                    }''')
                    session=SlowReturnSession(page,Path(directory));session.cdp=await page.context.new_cdp_session(page);session.auto_choices=True
                    proposal=SimpleNamespace(selections=[SimpleNamespace(option_id='option-3')])
                    session.historical=SimpleNamespace(lookup=lambda q:SimpleNamespace(proposal=proposal,source_ids=('fixture',)))
                    watch=asyncio.create_task(session.watch())
                    try:
                        await page.wait_for_function("document.body.innerText==='Fixture complete'")
                        await asyncio.sleep(.5)
                    finally:
                        watch.cancel();await asyncio.gather(watch,return_exceptions=True)
                        await asyncio.gather(*session.tasks,return_exceptions=True)
                    self.assertEqual(session.peak,1)
                    self.assertEqual(await page.evaluate('sent'),[dict(number=n,selected=2) for n in range(1,7)])
                    logs=[json.loads(l) for l in (Path(directory)/'personality.jsonl').read_text().splitlines()]
                    self.assertEqual([x['number'] for x in logs],[str(n) for n in range(1,7)])
                    self.assertTrue(all(x['transition_observed'] for x in logs))
                    self.assertFalse((Path(directory)/'personality-errors.jsonl').exists())
                finally:await browser.close()

    async def test_stale_snapshot_does_not_consume_new_question(self):
        with tempfile.TemporaryDirectory() as directory:
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(headless=True)
                try:
                    page=await browser.new_page();await page.set_content('<a class="currentQue">1</a>')
                    session=Session(page,Path(directory));state=await page.evaluate(STATE_SCRIPT)
                    await page.locator('.currentQue').evaluate("n=>n.innerText='2'")
                    self.assertFalse(await run_personality(session,state))
                    self.assertEqual(session.personality_tasks,set())
                finally:await browser.close()
