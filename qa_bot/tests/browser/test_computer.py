import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.async_api import async_playwright
from qa_bot.live_session import Session
from qa_bot.solvers.computer import automate

class ComputerTests(unittest.IsolatedAsyncioTestCase):
    async def test_reloads_simulation_frame_and_ignores_covered_controls(self):
        calls=[]
        class Client:
            def __init__(self,*args,**kwargs):pass
            async def complete(self,q,**kwargs):
                calls.append(q['question_id'])
                assert [n['id'] for n in q['nodes']]==['go']
                result={'question_id':q['question_id'],'content_hash':q['content_hash'],'status':'answer','confidence':1,'action':'click','id':'go','value':None,'target_id':None}
                if q['question_id']=='2':
                    rect=q['nodes'][0]['rect'];result.update(action='click_point',id='',x=rect['x']+rect['width']/2,y=rect['y']+rect['height']/2,target_x=None,target_y=None)
                return result
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.route('https://fixture.example/assets/msOfficeSimulation/**',lambda r:r.fulfill(content_type='text/html',body='<div id="covered" role="button" style="position:absolute;inset:0">Covered</div><button id="go" style="position:absolute;inset:0" onclick="parent.advance()">Next scene</button>'))
                await page.route('https://fixture.example/',lambda r:r.fulfill(content_type='text/html',body='''<div id="task">QUESTION<br>1 out of 16<br>Perform the synthetic action.<br>SKIP</div><a class="currentQue">1</a><iframe id="frame" src="/assets/msOfficeSimulation/run.html"></iframe><script>
                window.advance=()=>{const n=+document.querySelector('.currentQue').textContent;if(n===2){document.body.innerText='Assessments\\nComplete';return}document.querySelector('.currentQue').innerText='2';task.innerHTML='QUESTION<br>2 out of 16<br>Perform the synthetic action.<br>SKIP';frame.outerHTML='<iframe id="frame" src="/assets/msOfficeSimulation/run.html?scene=2"></iframe>'};</script>'''))
                await page.goto('https://fixture.example/');await page.frame_locator('#frame').locator('#go').wait_for()
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory));session.project_root=Path(directory)
                    with patch('qa_bot.adapters.llm.codex_cli.CodexCLIClient',Client),patch('shutil.which',return_value=sys.executable):
                        await automate(session,page.frames[1])
                    self.assertEqual(calls,['1','2'])
                    self.assertEqual(len((Path(directory)/'computer-actions.jsonl').read_text().splitlines()),2)
            finally:await browser.close()
