import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.async_api import async_playwright
from qa_bot.live_session import Session
from qa_bot.solvers.computer import automate


class Client:
    def __init__(self,*args,**kwargs):pass
    async def complete(self,q,**kwargs):
        return {'question_id':q['question_id'],'content_hash':q['content_hash'],'status':'answer','confidence':.9,
            'action':'click','id':'go','value':None,'target_id':None}


class FinalConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self,*,recognized=True,duplicate=False,closed_shadow=False):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                prompt='Are you ready to submit this assessment?' if recognized else 'A different warning requires attention.'
                body='''<div>QUESTION<br>16 out of 16<br>Perform the final synthetic action.<br>SUBMIT</div><a class="currentQue">16</a><iframe id="frame" src="/assets/msOfficeSimulation/run.html"></iframe><script>
                window.advance=()=>{
                  document.body.innerText='Your test is now complete. Thank you!';
                  setTimeout(()=>{
                    const outside=document.createElement('button');outside.textContent='SUBMIT';outside.onclick=()=>window.wrong=(window.wrong||0)+1;document.body.append(outside);
                    const dialog=document.createElement('div');dialog.id='ngdialog-final';dialog.textContent=PROMPT;document.body.append(dialog);
                    const button=document.createElement('button');button.textContent='SUBMIT';button.onclick=()=>{window.confirmed=(window.confirmed||0)+1;window.confirmedAt=Date.now();document.body.innerText='Your test is now complete. Thank you!'};dialog.append(button);
                    if(DUPLICATE)dialog.append(button.cloneNode(true));
                  },150);
                };</script>'''.replace('PROMPT',json.dumps(prompt)).replace('DUPLICATE','true' if duplicate else 'false')
                if closed_shadow:body=body.replace('dialog.append(button);',"const host=document.createElement('span');dialog.append(host);host.attachShadow({mode:'closed'}).append(button);")
                await page.route('https://fixture.example/assets/msOfficeSimulation/**',lambda r:r.fulfill(content_type='text/html',body='<button id="go" onclick="parent.advance()">Finish synthetic task</button>'))
                await page.route('https://fixture.example/',lambda r:r.fulfill(content_type='text/html',body=body))
                await page.goto('https://fixture.example/');await page.frame_locator('#frame').locator('#go').wait_for()
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory));session.project_root=Path(directory)
                    session.cdp=await page.context.new_cdp_session(page)
                    with patch('qa_bot.adapters.llm.codex_cli.CodexCLIClient',Client),patch('shutil.which',return_value='/bin/true'):
                        if not recognized or duplicate:
                            with self.assertRaisesRegex(ValueError,'unresolved overlay|one enabled final simulation confirmation submit'):
                                await automate(session,page.frames[1])
                            self.assertIsNone(await page.evaluate('window.confirmed'))
                            self.assertFalse((Path(directory)/'computer.jsonl').exists())
                        else:
                            await automate(session,page.frames[1])
                            self.assertEqual(await page.evaluate('window.confirmed'),1)
                            self.assertGreaterEqual(await page.evaluate('Date.now()-window.confirmedAt'),1000)
                            completed=json.loads((Path(directory)/'computer.jsonl').read_text().splitlines()[-1])
                            self.assertEqual(completed['number'],'16')
                            submit=json.loads((Path(directory)/'computer-final-submit.jsonl').read_text().splitlines()[-1])
                            self.assertEqual(submit['evidence'],'exact_visible_confirmation_dialog')
                        self.assertIsNone(await page.evaluate('window.wrong'))
            finally:await browser.close()

    async def test_late_modal_with_null_number_uses_only_scoped_submit(self):await self.execute()
    async def test_unknown_late_overlay_stops_without_clicking(self):await self.execute(recognized=False)
    async def test_ambiguous_confirmation_buttons_stop(self):await self.execute(duplicate=True)

    async def test_closed_shadow_confirmation_keeps_native_click_inside_dialog(self):await self.execute(closed_shadow=True)
