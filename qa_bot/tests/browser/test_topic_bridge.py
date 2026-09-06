import base64
import hashlib
import io
import unittest
import wave
from playwright.async_api import async_playwright
from qa_bot.audio.shared_microphone import SharedMicrophoneBridge
from qa_bot.audio.topic_bridge import topic_bridge_script
from tests.browser.test_dynamic_read_aloud import _tone_wav


class TopicBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_floor_is_below_speech_threshold_and_off_during_recording(self):
        origin='https://assessment.example'
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                context=await browser.new_context()
                await context.add_init_script(script=SharedMicrophoneBridge(origin,idle_floor=.001).init_script())
                page=await context.new_page()
                await page.route(origin+'/',lambda r:r.fulfill(content_type='text/html',body='<button>Begin</button><h2>Thinking Time</h2>'))
                await page.goto(origin+'/');await page.click('button')
                await page.wait_for_function('__qaMicrophoneBus.peak>0')
                await page.wait_for_function('''() => {const a=__qaMicrophoneBus.analyser,b=new Uint8Array(a.frequencyBinCount);a.getByteFrequencyData(b);return b.some(x=>x>0)}''')
                result=await page.evaluate('''async()=>{
                  const a=__qaMicrophoneBus.analyser,values=[];
                  for(let i=0;i<12;i++){const b=new Uint8Array(a.frequencyBinCount);a.getByteFrequencyData(b);values.push(b.reduce((s,x)=>s+x,0));await new Promise(r=>setTimeout(r,20));}
                  return {values,peak:__qaMicrophoneBus.peak,signal:__qaMicrophoneBus.nonzeroSamples};
                }''')
                self.assertGreater(len(set(result['values'])),1,result)
                self.assertLess(result['peak'],.002)
                self.assertEqual(result['signal'],0)
                await page.evaluate("document.querySelector('h2').textContent='Speak Now'")
                await page.wait_for_function('__qaMicrophoneBus.idleInput.gain.gain.value===0')
            finally:await browser.close()

    async def test_exact_armed_topic_waits_for_recording_and_modal_clearance(self):
        origin='https://assessment.example'
        out=io.BytesIO()
        with wave.open(io.BytesIO(_tone_wav())) as reader:pcm=reader.readframes(reader.getnframes())
        with wave.open(out,'wb') as writer:
            writer.setnchannels(1);writer.setsampwidth(2);writer.setframerate(16000);writer.writeframes(pcm*64)
        audio=out.getvalue()
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                context=await browser.new_context()
                await context.add_init_script(script=SharedMicrophoneBridge(origin).init_script()+'\n'+topic_bridge_script(origin))
                page=await context.new_page()
                await page.route(origin+'/',lambda r:r.fulfill(content_type='text/html',body='''
                    <button id="begin">Begin</button><h1>Section D: Free Speech</h1>
                    <button class="currentQue">28</button><p>Describe your favorite place.</p><h2>Get Ready</h2>'''))
                await page.goto(origin+'/');await page.click('#begin')
                data={'number':'28','prompt':'Describe your favorite place.','wav':base64.b64encode(audio).decode(),'sha256':hashlib.sha256(audio).hexdigest()}
                await page.evaluate('x=>__qaTopicSpeech.arm(x)',data)
                self.assertEqual(await page.evaluate('__qaTopicSpeech.replays'),0)
                await page.evaluate('''() => {document.querySelector('h2').textContent='Speak Now';
                    const dialog=document.createElement('div');dialog.id='ngdialog1';document.body.append(dialog);}''')
                await page.wait_for_timeout(100)
                self.assertEqual(await page.evaluate('__qaTopicSpeech.replays'),0)
                await page.evaluate("document.querySelector('#ngdialog1').remove()")
                await page.wait_for_function('__qaMicrophoneBus.peak>.01')
                self.assertEqual(await page.evaluate('__qaTopicSpeech.replays'),1)
                await page.wait_for_timeout(100)
                self.assertEqual(await page.evaluate('__qaTopicSpeech.replays'),1)
            finally:await browser.close()
