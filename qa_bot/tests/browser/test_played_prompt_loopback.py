import unittest
import json
import hashlib
from playwright.async_api import async_playwright
from qa_bot.audio.shared_microphone import SharedMicrophoneBridge
from qa_bot.audio.played_prompt_loopback import PlayedPromptLoopback
from tests.browser.test_dynamic_read_aloud import _tone_wav


class PlayedPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_html_conversation_captures_only_played_turns(self):
        origin='https://assessment.example'
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                context=await browser.new_context()
                await context.grant_permissions(['local-network-access'],origin=origin)
                await context.add_init_script(script=SharedMicrophoneBridge(origin).init_script()+'\n'+
                    PlayedPromptLoopback(origin,'t'*32,'synthetic','test').init_script())
                page=await context.new_page()
                await page.route(origin+'/',lambda r:r.fulfill(content_type='text/html',body='''
                    <button id="begin">Begin</button><h1>Section C: Listening Comprehension</h1>
                    <button class="currentQue">23</button><h2>Listen Carefully</h2>'''))
                await page.route('https://qbdata-amcat.s3.amazonaws.com/**',lambda r:r.fulfill(
                    status=403 if r.request.url.endswith('/current.wav') else 200,
                    body=_tone_wav(),content_type='audio/wav',headers={'Access-Control-Allow-Origin':origin}))
                await page.route('http://127.0.0.1:18771/**',lambda r:r.fulfill(status=201,
                    headers={'Access-Control-Allow-Origin':origin,'Access-Control-Allow-Headers':'*'},
                    content_type='application/json',body=json.dumps({'file':'audio/turn.wav','sha256':hashlib.sha256(_tone_wav()).hexdigest()})))
                await page.goto(origin+'/');await page.click('#begin')
                await page.evaluate('''async () => {
                    const root='https://qbdata-amcat.s3.amazonaws.com/SpeechAssessmentBank/QuestionBank/stimulus/418/';
                    const preload=globalThis.preload=new Audio(root+'next.wav');preload.preload='auto';preload.load();
                    const player=globalThis.turn=new Audio(root+'current.wav?qa_signature=fixture');await player.play();
                }''')
                await page.wait_for_function('__qaDirectAudioLoopback.heardSources.length===1')
                state=await page.evaluate('({heard:__qaDirectAudioLoopback.heardSources,replays:__qaDirectAudioLoopback.replayCount,failures:__qaDirectAudioLoopback.failures})')
                self.assertEqual(state['failures'],[])
                self.assertEqual(state['replays'],0)
                self.assertTrue(state['heard'][0]['path'].endswith('/current.wav'))
                self.assertEqual(state['heard'][0]['id'],'23')
            finally:await browser.close()

    async def test_html_capture_binding_receives_signed_playback_without_browser_fetch(self):
        origin='https://assessment.example';calls=[]
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                context=await browser.new_context()
                await context.add_init_script(script=SharedMicrophoneBridge(origin).init_script()+'\n'+PlayedPromptLoopback(origin,'t'*32,'synthetic','test').init_script())
                page=await context.new_page()
                async def capture(item):
                    calls.append(item)
                    return {'file':'audio/fixture.mp3','sha256':hashlib.sha256(_tone_wav()).hexdigest()}
                await page.expose_function('__qaCapturePlayedAudio',capture)
                await page.route(origin+'/',lambda r:r.fulfill(content_type='text/html',body='<button id="begin">Begin</button><h1>Section C: Listening Comprehension</h1><button class="currentQue">23</button>'))
                await page.route('https://qbdata-amcat.s3.amazonaws.com/**',lambda r:r.fulfill(body=_tone_wav(),content_type='audio/wav'))
                await page.goto(origin+'/');await page.click('#begin')
                await page.evaluate("async()=>{const a=globalThis.a=new Audio('https://qbdata-amcat.s3.amazonaws.com/SpeechAssessmentBank/stimulus/fixture.mp3?qa_signature=fixture');await a.play()}")
                await page.wait_for_function('__qaDirectAudioLoopback.heardSources.length===1')
                self.assertEqual(len(calls),1);self.assertIn('?qa_signature=fixture',calls[0]['url'])
                state=await page.evaluate('__qaDirectAudioLoopback.heardSources[0]')
                self.assertNotIn('?',state['path']);self.assertEqual(state['id'],'23')
            finally:await browser.close()

    async def test_prefetched_next_prompt_cannot_replace_played_current_prompt(self):
        origin='https://assessment.example'
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                context=await browser.new_context()
                await context.grant_permissions(['local-network-access'],origin=origin)
                await context.add_init_script(script=SharedMicrophoneBridge(origin).init_script()+'\n'+
                    PlayedPromptLoopback(origin,'t'*32,'synthetic','test').init_script())
                page=await context.new_page()
                await page.route(origin+'/',lambda r:r.fulfill(content_type='text/html',body='''
                    <button id="begin">Begin</button><h1>Section B: Listen and Repeat</h1>
                    <button class="currentQue">15</button><h2>Listen Carefully</h2>'''))
                current_audio=_tone_wav()
                next_audio=bytearray(current_audio)
                next_audio[44:]=bytes(len(next_audio)-44)
                await page.route('https://qbdata-amcat.s3.amazonaws.com/**',lambda r:r.fulfill(
                    body=bytes(next_audio) if r.request.url.endswith('next.wav') else current_audio,content_type='audio/wav'))
                await page.route('http://127.0.0.1:18771/**',lambda r:r.fulfill(status=201,
                    headers={'Access-Control-Allow-Origin':origin,'Access-Control-Allow-Headers':'*'},
                    content_type='application/json',body=json.dumps({'file':'audio/current.wav','sha256':hashlib.sha256(current_audio).hexdigest()})))
                await page.goto(origin+'/');await page.click('#begin')
                await page.evaluate('''async () => {
                    const c=globalThis.promptContext=new AudioContext();await c.resume();
                    globalThis.Zone={current:{run(callback,receiver,args){globalThis.callbackZone=true;try{return callback.apply(receiver,args)}finally{globalThis.callbackZone=false}}}};
                    const root='https://qbdata-amcat.s3.amazonaws.com/SpeechAssessmentBank/RepeatSentences/';
                    const current=await c.decodeAudioData(await (await fetch(root+'current.wav')).arrayBuffer(),()=>{globalThis.callbackInZone=globalThis.callbackZone===true});
                    const player=c.createBufferSource();player.buffer=current;player.connect(c.destination);player.start();
                    await c.decodeAudioData(await (await fetch(root+'next.wav')).arrayBuffer());
                    document.querySelector('h2').textContent='Speak Now';
                }''')
                await page.wait_for_function('__qaDirectAudioLoopback.status === "ready"')
                state=await page.evaluate('({sources:__qaDirectAudioLoopback.playedSources,replays:__qaDirectAudioLoopback.replayCount,peak:__qaMicrophoneBus.peak})')
                self.assertEqual(state['replays'],1)
                self.assertEqual(len(state['sources']),1)
                self.assertTrue(state['sources'][0]['path'].endswith('/current.wav'))
                self.assertGreater(state['peak'],.01)
                self.assertTrue(await page.evaluate('callbackInZone'))
                await page.wait_for_function('__qaDirectAudioLoopback.heardSources.length===1')
                await page.evaluate('''() => {
                    document.querySelector('button.currentQue').textContent='16';
                    document.querySelector('h2').textContent='Speak Now';
                }''')
                await page.wait_for_timeout(100)
                self.assertEqual(await page.evaluate('__qaDirectAudioLoopback.replayCount'),1)
            finally:await browser.close()
