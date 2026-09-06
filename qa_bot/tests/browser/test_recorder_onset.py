import hashlib,io,math,tempfile,threading,unittest,wave
from pathlib import Path
from playwright.async_api import async_playwright
from qa_bot.audio.shared_microphone import SharedMicrophoneBridge
from qa_bot.audio.recording_observer import recording_observer_script
from qa_bot.audio.dynamic_read_aloud import DynamicReadAloudBridge
from qa_bot.audio.live_speech_bridge import make_server
from tests.browser.test_dynamic_read_aloud import _Controller


def marked_wav():
    data=bytearray()
    for i in range(9600):
        frequency=500 if i<1600 else (1500 if i>=8000 else 1000)
        data.extend(int(18000*math.sin(2*math.pi*frequency*i/16000)).to_bytes(2,'little',signed=True))
    out=io.BytesIO()
    with wave.open(out,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(data)
    return out.getvalue()


class RecorderOnsetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.origin='https://assessment.example';self.controller=_Controller();self.controller.wav=marked_wav()
        self.server=make_server(self.controller,token='s'*32,allowed_origin=self.origin)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.pw=await async_playwright().start();self.browser=await self.pw.chromium.launch(headless=True)
        self.context=await self.browser.new_context()
        await self.context.grant_permissions(['local-network-access'],origin=self.origin)
        scripts=SharedMicrophoneBridge(self.origin).init_script()+'\n'+recording_observer_script(self.origin,'s'*32)+'\n'+DynamicReadAloudBridge(f'http://127.0.0.1:{self.server.server_port}','s'*32,self.origin,'/',
            'fixture','fixture',auto_detect=True,question_counter_selector='.currentQue',section_heading='Section A: Read and Speak').init_script()
        await self.context.add_init_script(script=scripts)
        self.page=await self.context.new_page()
        await self.page.route(self.origin+'/',lambda r:r.fulfill(content_type='text/html',body='''<main><h1>Section A: Read and Speak</h1><button id="begin">Begin</button><button class="currentQue">5</button><p>Read the given sentence out loud.</p><p class="sentence">What lovely ambiance.</p><p class="phase">Get Ready</p><p class="timer">Remaining Time : 00:02</p></main>'''))
        await self.page.route('http://127.0.0.1:18772/**',lambda r:r.fulfill(status=201,headers={'Access-Control-Allow-Origin':self.origin,'Access-Control-Allow-Headers':'*'},body='{}'))
        await self.page.goto(self.origin+'/');await self.page.click('#begin')
        await self.page.wait_for_function("__qaDynamicReadAloud.status==='armed'",timeout=3000)
        await self.page.evaluate('''async()=>{
          const stream=await navigator.mediaDevices.getUserMedia({audio:true});
          globalThis.chunks=[];globalThis.recorder=new MediaRecorder(stream,{mimeType:'audio/mp4;codecs=opus'});
          recorder.addEventListener('dataavailable',event=>chunks.push(event.data));
          globalThis.stopped=new Promise(resolve=>recorder.addEventListener('stop',resolve,{once:true}));
        }''')

    async def asyncTearDown(self):
        await self.browser.close();await self.pw.stop();self.server.shutdown();self.server.server_close();self.thread.join()

    async def recording_metrics(self):
        return await self.page.evaluate('''async()=>{
          recorder.stop();await stopped;
          const decoded=await __qaMicrophoneBus.context.decodeAudioData(await new Blob(chunks).arrayBuffer());
          const samples=decoded.getChannelData(0),rate=decoded.sampleRate,window=Math.round(rate*.020),bands={500:0,1500:0};
          let peak=0;for(const v of samples)peak=Math.max(peak,Math.abs(v));
          for(let offset=0;offset+window<=samples.length;offset+=window){
            for(const frequency of [500,1500]){
              let sine=0,cosine=0;
              for(let i=0;i<window;i++){const angle=2*Math.PI*frequency*i/rate;sine+=samples[offset+i]*Math.sin(angle);cosine+=samples[offset+i]*Math.cos(angle)}
              if(2*Math.hypot(sine,cosine)/window>.12)bands[frequency]+=.020;
            }
          }
          return {peak,bands,duration:decoded.duration};
        }''')

    async def test_actual_delayed_recorder_preserves_first_and_last_signal(self):
        await self.page.locator('.phase').evaluate("n=>n.textContent='Speak Now'")
        await self.page.wait_for_timeout(80)
        self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.replayCount'),0)
        await self.page.evaluate('recorder.start()')
        await self.page.wait_for_function("__qaDynamicReadAloud.status==='played'")
        await self.page.wait_for_timeout(150)
        metrics=await self.recording_metrics()
        self.assertGreaterEqual(metrics['bands']['500'],.079)
        self.assertGreaterEqual(metrics['bands']['1500'],.079)
        self.assertGreater(metrics['duration'],.6)
        self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.failures'),[])

    async def test_question_change_before_scheduled_source_start_cancels_audio(self):
        await self.page.locator('.phase').evaluate("n=>n.textContent='Speak Now'")
        await self.page.evaluate('recorder.start()')
        await self.page.wait_for_function('__qaDynamicReadAloud.replayCount===1')
        await self.page.evaluate("()=>{document.querySelector('.currentQue').textContent='6';document.querySelector('.sentence').textContent='A different question starts here.'}")
        await self.page.wait_for_timeout(800)
        metrics=await self.recording_metrics()
        self.assertLess(metrics['peak'],.01)
        self.assertEqual(metrics['bands']['500'],0)
        self.assertEqual(metrics['bands']['1500'],0)

    async def test_insufficient_remaining_window_never_truncates_answer(self):
        await self.page.locator('.timer').evaluate("n=>n.textContent='Remaining Time : 00:00'")
        await self.page.locator('.phase').evaluate("n=>n.textContent='Speak Now'")
        await self.page.evaluate('recorder.start()')
        await self.page.wait_for_function("__qaDynamicReadAloud.failures.includes('recording_window_too_short')")
        self.assertEqual(await self.page.evaluate('__qaDynamicReadAloud.replayCount'),0)
        await self.page.wait_for_timeout(250)
        self.assertLess((await self.recording_metrics())['peak'],.01)
