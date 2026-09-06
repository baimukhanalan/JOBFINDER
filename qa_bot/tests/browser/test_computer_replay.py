import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.async_api import async_playwright
from qa_bot.live_session import Session
from qa_bot.solvers.computer import automate

BODY='''<div>QUESTION<br>1 out of 16<br>Perform the synthetic action.<br>SKIP</div><a class="currentQue">1</a><iframe id="frame" width="400" height="300" src="/assets/msOfficeSimulation/run.html"></iframe><script>window.advance=()=>{document.body.innerText='Assessments\\nComplete'};</script>'''
FRAME='<button id="go" style="position:absolute;left:20px;top:20px" onclick="parent.advance()">Next scene</button>'

class ComputerReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_advancement_is_not_replayed_as_correct(self):
        calls=[]
        class Client:
            def __init__(self,*args,**kwargs):pass
            async def complete(self,q,**kwargs):
                calls.append(q['question_id'])
                return {'question_id':q['question_id'],'content_hash':q['content_hash'],'status':'answer','confidence':1,
                    'action':'click','id':'go','value':None,'target_id':None}
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.route('https://fixture.example/assets/msOfficeSimulation/**',lambda r:r.fulfill(content_type='text/html',body=FRAME))
                await page.route('https://fixture.example/',lambda r:r.fulfill(content_type='text/html',body=BODY))
                with tempfile.TemporaryDirectory() as directory:
                    for number in (1,2):
                        await page.goto('https://fixture.example/');await page.frame_locator('#frame').locator('#go').wait_for()
                        output=Path(directory)/str(number);output.mkdir()
                        session=Session(page,output);session.project_root=Path(directory);session.test_id=str(number);session.replay_only=number==2
                        with patch('qa_bot.adapters.llm.codex_cli.CodexCLIClient',Client),patch('shutil.which',return_value='/bin/true' if number==1 else None):
                            if number==1:await automate(session,page.frames[1])
                            else:
                                with self.assertRaisesRegex(ValueError,'unverified_scene_in_replay_mode'):
                                    await automate(session,page.frames[1])
                        if number==1:self.assertIn('Assessments',await page.inner_text('body'))
                    self.assertEqual(calls,['1'])
                    self.assertFalse((Path(directory)/'2/computer-actions.jsonl').exists())
            finally:await browser.close()

    async def test_unknown_replay_only_scene_never_constructs_model(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.route('https://fixture.example/assets/msOfficeSimulation/**',lambda r:r.fulfill(content_type='text/html',body=FRAME))
                await page.route('https://fixture.example/',lambda r:r.fulfill(content_type='text/html',body=BODY))
                await page.goto('https://fixture.example/');await page.frame_locator('#frame').locator('#go').wait_for()
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory));session.project_root=Path(directory);session.replay_only=True
                    with patch('qa_bot.adapters.llm.codex_cli.CodexCLIClient',side_effect=AssertionError('must not construct model')):
                        with self.assertRaisesRegex(ValueError,'unknown_scene_in_replay_mode'):
                            await automate(session,page.frames[1])
                    self.assertFalse((Path(directory)/'computer-actions.jsonl').exists())
            finally:await browser.close()

    async def test_scene_change_during_model_is_reobserved_before_action(self):
        calls=[]
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.route('https://fixture.example/assets/msOfficeSimulation/**',lambda r:r.fulfill(content_type='text/html',body=FRAME))
                await page.route('https://fixture.example/',lambda r:r.fulfill(content_type='text/html',body=BODY))
                await page.goto('https://fixture.example/');await page.frame_locator('#frame').locator('#go').wait_for()
                class Client:
                    def __init__(self,*args,**kwargs):pass
                    async def complete(self,q,**kwargs):
                        calls.append(q['content_hash'])
                        if len(calls)==1:
                            await page.frames[1].evaluate("document.querySelector('#go').innerText='Changed scene'")
                        return {'question_id':q['question_id'],'content_hash':q['content_hash'],'status':'answer','confidence':1,
                            'action':'click','id':'go','value':None,'target_id':None}
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory));session.project_root=Path(directory)
                    with patch('qa_bot.adapters.llm.codex_cli.CodexCLIClient',Client),patch('shutil.which',return_value='/bin/true'):
                        await automate(session,page.frames[1])
                    self.assertEqual(len(calls),2)
                    actions=(Path(directory)/'computer-actions.jsonl').read_text().splitlines()
                    self.assertEqual(len(actions),1)
                    retries=json.loads((Path(directory)/'computer-retries.jsonl').read_text().splitlines()[0])
                    self.assertEqual(retries['reason'],'scene_changed_before_action')
            finally:await browser.close()

    async def test_message_evidence_keeps_safe_values_and_ignores_other_frames(self):
        from qa_bot.solvers.computer import install_message_evidence,capture_message_evidence
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('<a class="currentQue">16</a><iframe id="frame"></iframe><iframe id="other"></iframe>')
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory))
                    await install_message_evidence(session)
                    await page.frames[1].evaluate("parent.postMessage({message:'Success',ready:true,score:1,url:'https://secret.example/?token=abc',nested:{success:true}},'*')")
                    await page.frames[2].evaluate("parent.postMessage({message:'Forged',success:true},'*')")
                    await page.wait_for_timeout(50)
                    events=await capture_message_evidence(session)
                    self.assertEqual(len(events),1);self.assertEqual(events[0]['question'],'16')
                    self.assertEqual(events[0]['fields'],{'message':'Success','ready':True,'score':1,'url':'[redacted]','nested':{'success':True}})
                    await capture_message_evidence(session)
                    self.assertEqual(len((Path(directory)/'computer-events.jsonl').read_text().splitlines()),1)
            finally:await browser.close()

    async def test_final_result_screen_submits_closed_shadow_control_once(self):
        class Client:
            def __init__(self,*args,**kwargs):pass
            async def complete(self,q,**kwargs):
                return {'question_id':q['question_id'],'content_hash':q['content_hash'],'status':'answer','confidence':.9,
                    'action':'click','id':'go','value':None,'target_id':None}
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                body=BODY.replace('1 out of 16','16 out of 16').replace('class="currentQue">1','class="currentQue">16')
                body=body.replace("window.advance=()=>{document.body.innerText='Assessments\\nComplete'}", "window.advance=()=>{const host=document.createElement('div');document.body.append(host);const root=host.attachShadow({mode:'closed'});const submit=document.createElement('button');submit.textContent='SUBMIT';submit.onclick=()=>{window.submits=(window.submits||0)+1;document.body.innerText='Your test is now complete. Thank you!'};root.append(submit)}")
                frame=FRAME.replace('parent.advance()',"document.body.innerText='result_message';parent.advance()")
                await page.route('https://fixture.example/assets/msOfficeSimulation/**',lambda r:r.fulfill(content_type='text/html',body=frame))
                await page.route('https://fixture.example/',lambda r:r.fulfill(content_type='text/html',body=body))
                await page.goto('https://fixture.example/');await page.frame_locator('#frame').locator('#go').wait_for()
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory));session.project_root=Path(directory)
                    session.cdp=await page.context.new_cdp_session(page)
                    with patch('qa_bot.adapters.llm.codex_cli.CodexCLIClient',Client),patch('shutil.which',return_value='/bin/true'):
                        await automate(session,page.frames[1])
                    self.assertEqual(await page.evaluate('window.submits'),1)
                    result=json.loads((Path(directory)/'computer.jsonl').read_text().splitlines()[-1])
                    self.assertEqual(result['number'],'16');self.assertFalse(result['correctness_verified'])
            finally:await browser.close()

    async def test_result_message_without_observed_task_actions_never_submits(self):
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                body=BODY.replace('1 out of 16','16 out of 16').replace('class="currentQue">1','class="currentQue">16')+'<button onclick="window.submitted=true">SUBMIT</button>'
                await page.route('https://fixture.example/assets/msOfficeSimulation/**',lambda r:r.fulfill(content_type='text/html',body='result_message'))
                await page.route('https://fixture.example/',lambda r:r.fulfill(content_type='text/html',body=body))
                await page.goto('https://fixture.example/')
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory));session.project_root=Path(directory);session.replay_only=True
                    with patch('qa_bot.adapters.llm.codex_cli.CodexCLIClient',side_effect=AssertionError('model forbidden')):
                        with self.assertRaisesRegex(ValueError,'unknown_scene_in_replay_mode'):
                            await automate(session,page.frames[1])
                    self.assertIsNone(await page.evaluate('window.submitted'))
                    self.assertFalse((Path(directory)/'computer-final-submit.jsonl').exists())
            finally:await browser.close()

    async def test_nested_message_evidence_is_bounded_and_redacted(self):
        from qa_bot.solvers.computer import install_message_evidence,capture_message_evidence
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('<a class="currentQue">3</a><iframe id="frame"></iframe>')
                with tempfile.TemporaryDirectory() as directory:
                    session=Session(page,Path(directory))
                    await install_message_evidence(session)
                    await page.frames[1].evaluate("""parent.postMessage({message:{success:true,attempts:1,url:'https://secret.example/?token=abc',long:'x'.repeat(101),results:[true,1,'correct'],deep:{next:{secret:'not retained'}},many:Array.from({length:30},(_,i)=>i),'unsafe/key':'not retained'}},'*')""")
                    await page.wait_for_timeout(50)
                    events=await capture_message_evidence(session)
                    message=events[0]['fields']['message']
                    self.assertTrue(message['success']);self.assertEqual(message['attempts'],1)
                    self.assertEqual(message['url'],'[redacted]');self.assertEqual(message['long'],'[redacted]')
                    self.assertEqual(message['results'],[True,1,'correct'])
                    self.assertEqual(message['deep']['next']['secret'],'[redacted]')
                    self.assertEqual(message['many'],list(range(20)))
                    self.assertNotIn('unsafe/key',message)
                    serialized=(Path(directory)/'computer-events.jsonl').read_text()
                    self.assertNotIn('token=abc',serialized);self.assertNotIn('not retained',serialized)
            finally:await browser.close()
