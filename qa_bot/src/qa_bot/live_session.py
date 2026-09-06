"""Single-owner, observable browser session for one explicitly selected QA link.

The named profile must already be authorized for testing. This launcher reads
only its invitation, and operates and submits answers on one owned assessment
page. It never changes JobFinder assessment markers. URLs are never logged.
"""
from __future__ import annotations
import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import shutil
import time
from dataclasses import replace
from urllib.parse import urlsplit
import wave

from playwright.async_api import async_playwright
from qa_bot.audio.shared_microphone import SharedMicrophoneBridge
from qa_bot.audio.dynamic_read_aloud import DynamicReadAloudBridge
from qa_bot.audio.recording_observer import recording_observer_script
from qa_bot.audio.played_prompt_loopback import PlayedPromptLoopback
from qa_bot.audio.browser_decode import decode_to_wav
from qa_bot.adapters.stt.natively_local import NativelyLocalSTT
from qa_bot.adapters.llm.codex_cli import CodexCLIClient
from qa_bot.domain.question import QuestionSpec,OptionSpec,AssetRef,ResponseContract,ResponseKind
from qa_bot.knowledge.bank import QuestionBank,fingerprint
from qa_bot.solvers.engine import AnswerEngine
from qa_bot.solvers.spoken import SpokenTextSolver
from qa_bot.audio.replay_bank import SpeechReplayBank
from qa_bot.adapters.tts.macos_local import MacOSLocalTTS
from qa_bot.audio.topic_bridge import topic_bridge_script

ORIGIN = 'https://amcatglobal.aspiringminds.com'


def bundle(token, profile, test):
    return '\n'.join((
        SharedMicrophoneBridge(ORIGIN,idle_floor=.001).init_script(),
        recording_observer_script(ORIGIN, token),
        topic_bridge_script(ORIGIN),
        PlayedPromptLoopback(ORIGIN,token,profile,test).init_script(),
        DynamicReadAloudBridge('http://127.0.0.1:18769', token, ORIGIN, '/',
            profile, test, auto_detect=True, question_counter_selector='button.currentQue',
            suspension_selector='[id^="ngdialog"]',section_heading='Section A: Read and Speak').init_script(),
    ))


STATE_SCRIPT = '''() => {
 const r=globalThis.__qaDynamicReadAloud,b=globalThis.__qaMicrophoneBus,
       l=globalThis.__qaDirectAudioLoopback,t=globalThis.__qaTopicSpeech;
 return {text:document.body.innerText.slice(0,4000),
   number:document.querySelector('button.currentQue')?.textContent.trim(),
   read:r&&{status:r.status,prepared:r.prepareCount,replays:r.replayCount,
     retries:r.retryCount,key:r.currentKey,siteId:r.currentSiteId,failures:r.failures},
   repeat:l&&{status:l.status,replays:l.replayCount,siteId:l.currentSiteId,failures:l.failures},
   microphone:b&&{state:b.context.state,requests:b.streamsRequested,peak:b.peak,
     signal:b.nonzeroSamples,consumers:b.consumers?.map(n=>n.context.state)},
   topic:t&&{status:t.status,siteId:t.siteId,replays:t.replays,failures:t.failures},
   heard:l?.heardSources||[],visibility:document.visibilityState};
}'''


def safe_text(value):
    return re.sub(r'https?://[^\s"<>]+', '[URL]', str(value))


class Session:
    def __init__(self, page, output):
        self.page, self.output = page, output
        self.tasks = set()
        self.auto_speech = False
        self.ready_since = {}
        self.submitted = set()
        self.signal_baseline = {}
        self.auto_choices = False
        self.transcriptions = {}
        self.choice_tasks = set()
        self.stt_lock = asyncio.Semaphore(1)
        self.topic_tasks=set()
        self.auto_navigation=False
        self.navigation_at=0
        self.output.mkdir(parents=True, exist_ok=True)

    async def click(self, role, name):
        # Native accessibility also covers the platform's closed shadow buttons.
        tree = await self.cdp.send('Accessibility.getFullAXTree')
        nodes = [n for n in tree['nodes'] if not n.get('ignored')
                 and n.get('role', {}).get('value') == role
                 and n.get('name', {}).get('value', '').strip().casefold() == name.casefold()
                 and not any(p['name'] == 'disabled' and p['value'].get('value')
                             for p in n.get('properties', []))]
        if len(nodes) != 1:
            raise ValueError('one accessible enabled control required')
        node = nodes[0]['backendDOMNodeId']
        await self.cdp.send('DOM.scrollIntoViewIfNeeded', {'backendNodeId':node})
        box = await self.cdp.send('DOM.getBoxModel', {'backendNodeId':node})
        quad = box['model']['content']
        x, y = sum(quad[::2])/4, sum(quad[1::2])/4
        for kind in ('mousePressed','mouseReleased'):
            await self.cdp.send('Input.dispatchMouseEvent',
                {'type':kind,'x':x,'y':y,'button':'left','clickCount':1})

    async def controls(self, role=None):
        tree=await self.cdp.send('Accessibility.getFullAXTree')
        return [n for n in tree['nodes'] if not n.get('ignored') and
                n.get('role',{}).get('value') in ((role,) if role else ('button','radio','checkbox'))]

    async def transcribe(self, item):
        digest=item['sha256'];target=(self.prompt_dir/item['file']).resolve()
        if not target.is_relative_to(self.prompt_dir.resolve()):raise ValueError('prompt path rejected')
        encoded=target.read_bytes()
        if hashlib.sha256(encoded).hexdigest()!=digest:raise ValueError('prompt integrity failure')
        wav=self.output/(digest+'.wav')
        if not wav.exists():wav.write_bytes(await decode_to_wav(self.page,encoded))
        text_path=self.output/(digest+'.txt')
        if text_path.exists():text=text_path.read_text()
        else:
            async with self.stt_lock:
                text=await NativelyLocalSTT(self.project_root).transcribe_wav(wav,timeout=90)
            text_path.write_text(text)
        self.log('transcripts.jsonl',{'time':time.time(),**item,'transcript':text})
        print(json.dumps({'transcript_ready':item['id'],'words':len(text.split())}),flush=True)
        return text

    @staticmethod
    def stable_text(text):
        return re.sub(r'Remaining Time\s*:\s*\n\d{1,2}:\d{2}','',text)

    @staticmethod
    def topic_prompt(state):
        text=state['text']
        if 'Section D: Free Speech' not in text:return None
        matches=re.findall(r'Your topic is:[ \t]*([^\n]+)',text)
        if len(matches)!=1 or not 10<=len(matches[0])<=1500:return None
        return 'Your topic is: '+' '.join(matches[0].split())

    async def prepare_topic(self,state,prompt):
        number=state['number']
        try:
            with SpeechReplayBank(self.prompt_dir.parent/'speech.sqlite3',self.prompt_dir.parent/'answers') as bank:
                replay=bank.replay(prompt,source_profile=self.profile_id,source_test=self.test_id,source_question='topic-'+number)
                if replay is None:
                    isolated=self.output/'isolated';isolated.mkdir(exist_ok=True)
                    client=CodexCLIClient(Path(shutil.which('codex')),self.project_root/'configs/answer_schema.json',isolated)
                    solver=SpokenTextSolver(client,min_words=75)
                    answer=await solver('Prepare a natural English spoken response of 80 to 95 words for this authorized test topic. Use complete sentences, a clear example and a short conclusion. Output only the spoken words in the text field. Topic: '+prompt,timeout=20)
                    if len(answer.split())>105:raise ValueError('topic answer too long')
                    replay=await bank.get_or_create(prompt,answer,speech=MacOSLocalTTS(),voice='Samantha',model='macos-say',
                        source_profile=self.profile_id,source_test=self.test_id,source_question='topic-'+number,timeout=10)
                fresh=await self.page.evaluate(STATE_SCRIPT)
                if fresh.get('number')!=number or prompt not in ' '.join(fresh['text'].split()):raise ValueError('topic changed during preparation')
                result=await self.page.evaluate('x=>__qaTopicSpeech.arm(x)',{'number':number,'prompt':prompt,
                    'wav':base64.b64encode(replay.wav_path.read_bytes()).decode(),'sha256':replay.wav_sha256})
                self.log('topics.jsonl',{'number':number,'prompt':prompt,'answer':replay.answer_text,
                    'source':replay.source,'sha256':replay.audio_sha256,**result})
                print(json.dumps({'topic_prepared':number,**result}),flush=True)
        except Exception as error:
            self.log('topic-errors.jsonl',{'number':number,'error':safe_text(error)[:300]})
            print(json.dumps({'topic_blocked':number,'reason':safe_text(error)[:300]}),flush=True)

    async def solve_choice(self, state, sources):
        number=state['number']
        try:
            # A conversation is played as ordered alternating speaker files.
            passage=sources[-1]
            conversation=passage['path'].split('/stimulus/')[1].split('/')[0]
            sources=[s for s in sources if '/stimulus/'+conversation+'/' in s['path']]
            text='\n'.join(await asyncio.gather(*(self.transcriptions[s['sha256']] for s in sources)))
            nodes=await self.controls('radio')
            labels=[n.get('name',{}).get('value','').strip() for n in nodes]
            if len(labels)<2 or len(set(labels))!=len(labels):raise ValueError('ambiguous choices')
            current=await self.page.evaluate(STATE_SCRIPT)
            if current.get('number')!=number:raise ValueError('question changed during transcription')
            question_text=self.stable_text(current['text'])
            q=QuestionSpec('listening-'+number,'authorized-single', 'Listening Comprehension',
                'SVAR-LISTENING-MCQ',question_text,ResponseContract(ResponseKind.SINGLE_CHOICE,1,1),'pending',
                options=tuple(OptionSpec('option-'+str(i),i,label) for i,label in enumerate(labels,1)),
                context=(text,),assets=tuple(AssetRef('turn-'+str(i),'audio/mpeg',s['file'],s['sha256']) for i,s in enumerate(sources)),
                provenance=tuple('stt:'+s['sha256'] for s in sources),completeness=True,extraction_confidence=1)
            q=replace(q,content_hash=fingerprint(q)[0])
            executable=shutil.which('codex')
            if not executable:raise ValueError('Codex CLI unavailable')
            isolated=self.output/'isolated';isolated.mkdir(exist_ok=True)
            client=CodexCLIClient(Path(executable),self.project_root/'configs/answer_schema.json',isolated)
            with QuestionBank(self.output/'listening.sqlite3') as bank:
                answer=await AnswerEngine(bank,client).propose(q,authorized_qa=True,timeout=40)
            if not answer.proposal:raise ValueError(answer.reason)
            label=next(o.label for o in q.options if o.id==answer.proposal.selections[0].option_id)
            fresh=await self.page.evaluate(STATE_SCRIPT)
            fresh_labels=[n.get('name',{}).get('value','').strip() for n in await self.controls('radio')]
            if fresh.get('number')!=number or self.stable_text(fresh['text'])!=question_text or fresh_labels!=labels:
                raise ValueError('question changed during solving')
            await self.click('radio',label)
            selected=[n for n in await self.controls('radio') if n.get('name',{}).get('value','').strip()==label]
            if len(selected)!=1 or not any(p['name']=='checked' and p['value'].get('value') in (True,'true') for p in selected[0].get('properties',[])):
                raise ValueError('choice was not selected')
            submitted=False
            for button in ('SUBMIT ANSWER','NEXT'):
                try:await self.click('button',button);submitted=True;break
                except ValueError:pass
            if not submitted:raise ValueError('answer submit unavailable')
            self.log('choices.jsonl',{'time':time.time(),'number':number,'question':question_text,
                'transcript_sha256':[s['sha256'] for s in sources],'selected':label,'confidence':answer.proposal.confidence,
                'source':answer.source,'correctness_verified':False})
            print(json.dumps({'choice_submitted':number,'selected':label}),flush=True)
        except Exception as error:
            self.log('choice-errors.jsonl',{'number':number,'error':safe_text(error)[:500]})
            print(json.dumps({'choice_blocked':number,'reason':safe_text(error)[:300]}),flush=True)

    def log(self, filename, value):
        with (self.output / filename).open('a') as f:
            f.write(json.dumps(value, ensure_ascii=False) + '\n')

    def response(self, response):
        task = asyncio.create_task(self.record_response(response))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def record_response(self, response):
        req = response.request
        url = urlsplit(req.url)
        if req.method=='GET' and url.hostname=='qbdata-amcat.s3.amazonaws.com':
            self.log('audio-network.jsonl', {'time':time.time(),'path':url.path,'status':response.status})
        if (req.method=='GET' and response.status==200 and
                url.hostname=='qbdata-amcat.s3.amazonaws.com' and
                url.path.startswith('/SpeechAssessmentBank/')):
            try:
                data=await response.body()
                if 0<len(data)<=20_000_000:
                    await self.page.evaluate('(x)=>globalThis.__qaDirectAudioLoopback?.registerSource(x.bytes,x.url)',
                        {'bytes':base64.b64encode(data).decode(),'url':f'https://{url.hostname}{url.path}'})
            except Exception as error:self.log('observer-errors.jsonl',{'type':type(error).__name__,'phase':'source_registration'})
        if req.method not in ('POST', 'PUT') or url.hostname in ('127.0.0.1', 'localhost'):
            return
        try:
            body = req.post_data_buffer or b''
            report = dict(time=time.time(), host=url.hostname, path=url.path,
                          status=response.status, method=req.method, bytes=len(body))
            i = body.find(b'RIFF')
            if i >= 0 and body[i+8:i+12] == b'WAVE':
                size = struct.unpack_from('<I', body, i+4)[0] + 8
                audio = body[i:i+size]
                if len(audio) == size:
                    with wave.open(io.BytesIO(audio)) as wav:
                        duration = wav.getnframes() / wav.getframerate()
                        width = wav.getsampwidth()
                        pcm = wav.readframes(wav.getnframes())
                    sha = hashlib.sha256(audio).hexdigest()
                    name = 'upload-' + sha[:24] + '.wav'
                    (self.output / name).write_bytes(audio)
                    report.update(audio=name, sha256=sha, duration=duration)
                    if width == 2:
                        samples = struct.unpack('<' + 'h' * (len(pcm)//2), pcm)
                        report['peak'] = max(map(abs, samples), default=0) / 32768
                        report['rms'] = (sum(v*v for v in samples)/max(1,len(samples)))**.5/32768
            self.log('uploads.jsonl', report)
            if 'audio' in report:
                print(json.dumps({'upload':report}), flush=True)
        except Exception as error:
            self.log('observer-errors.jsonl', {'type':type(error).__name__})

    async def watch(self):
        previous = None
        while True:
            try:
                state = await self.page.evaluate(STATE_SCRIPT)
                if self.auto_navigation and time.monotonic()-self.navigation_at>.5:
                    self.navigation_at=time.monotonic()
                    if 'To resume your assessment' in state['text']:
                        try:await self.click('button','CONTINUE')
                        except ValueError:pass
                    else:
                        checkboxes=await self.controls('checkbox')
                        label='I confirm I have read and understood this Notice.'
                        if any(n.get('name',{}).get('value','').strip()==label for n in checkboxes):
                            await self.command({'action':'notice'})
                        elif 'Section ' in state['text'] and 'Listen Carefully' in state['text'] and 'NEXT' in state['text']:
                            try:await self.click('button','NEXT')
                            except ValueError:pass
                prompt=self.topic_prompt(state)
                if self.auto_speech and prompt and state.get('number') not in self.topic_tasks:
                    self.topic_tasks.add(state['number'])
                    task=asyncio.create_task(self.prepare_topic(state,prompt))
                    self.tasks.add(task);task.add_done_callback(self.tasks.discard)
                passages=[x for x in state.get('heard',[]) if x.get('section','').startswith('Section C:') and '/stimulus/' in x.get('path','')]
                for item in passages:
                    if item['sha256'] not in self.transcriptions:
                        task=asyncio.create_task(self.transcribe(item));self.transcriptions[item['sha256']]=task
                        self.tasks.add(task);task.add_done_callback(self.tasks.discard)
                if (self.auto_choices and passages and 'Section C:' in state['text']
                        and 'Warning!' not in state['text']
                        and state.get('number') not in self.choice_tasks):
                    radios=await self.controls('radio')
                    if len(radios)>=2:
                        self.choice_tasks.add(state['number'])
                        task=asyncio.create_task(self.solve_choice(state,passages))
                        self.tasks.add(task);task.add_done_callback(self.tasks.discard)
                number=state.get('number')
                microphone=state.get('microphone') or {}
                if number and number not in self.signal_baseline:
                    self.signal_baseline[number]=microphone.get('signal',0)
                read=state.get('read') or {};repeat=state.get('repeat') or {}
                topic=state.get('topic') or {}
                completed=(read.get('status')=='played' and read.get('siteId')=='navigation:'+str(number)) or (
                    repeat.get('status')=='ready' and repeat.get('siteId')=='navigation:'+str(number)) or (
                    topic.get('status')=='played' and topic.get('siteId')=='navigation:'+str(number))
                if (self.auto_speech and number and completed and number not in self.submitted
                        and 'Warning!' not in state['text'] and 'Speak Now' in state['text']
                        and microphone.get('signal',0)>self.signal_baseline.get(number,0)+2):
                    self.ready_since.setdefault(number,time.monotonic())
                    if time.monotonic()-self.ready_since[number]>.35:
                        try:
                            await self.click('button','SUBMIT ANSWER')
                            self.submitted.add(number)
                            self.log('actions.jsonl',{'time':time.time(),'action':'submit_speech','number':number})
                        except ValueError:pass  # The platform's minimum recording time still applies.
                key = json.dumps({k:v for k,v in state.items() if k != 'microphone'})
                if key != previous:
                    previous = key
                    self.log('states.jsonl', {'time':time.time(), **state})
                    if getattr(self,'last_printed_state',None)!=(state.get('number'),json.dumps([state.get('read'),state.get('repeat'),state.get('topic')])):
                        self.last_printed_state=(state.get('number'),json.dumps([state.get('read'),state.get('repeat'),state.get('topic')]))
                        print(json.dumps({'state':{k:state.get(k) for k in ('number','read','repeat','topic','microphone')}}), flush=True)
            except Exception:
                pass  # A document replacement has no reliable snapshot.
            await asyncio.sleep(.1)

    async def command(self, command):
        action = command['action']
        if action == 'state':
            return await self.page.evaluate(STATE_SCRIPT)
        if action == 'audio_evidence':
            return await self.page.evaluate('({recorders:globalThis.__qaRecorderEvidence,sources:globalThis.__qaDirectAudioLoopback?.observedSources})')
        if action == 'verify_recording':
            name=command['name']
            if not re.fullmatch(r'recorder-\d+\.(m4a|webm)',name):raise ValueError('recording name rejected')
            source=self.output.parent/name
            wav=self.output/(source.stem+'.wav')
            wav.write_bytes(await decode_to_wav(self.page,source.read_bytes()))
            transcript=await NativelyLocalSTT(self.project_root).transcribe_wav(wav,timeout=90)
            report={'recording':name,'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'transcript':transcript}
            self.log('recording-verification.jsonl',report)
            return report
        if action == 'browser_diagnostics':
            return await self.page.evaluate('''() => ({now:performance.now(),
                scripts:[...document.scripts].map(s=>s.src).filter(Boolean).map(x=>new URL(x).pathname),
                buttons:[...document.querySelectorAll('button')].filter(b=>b.textContent.trim()==='NEXT').map(b=>({text:b.textContent,disabled:b.disabled,html:b.outerHTML}))})''')
        if action == 'auto_speech':
            self.auto_speech = True
            return {'automatic_speech_submission':True}
        if action == 'auto_choices':
            self.auto_choices=True
            return {'automatic_listening_choices':True}
        if action == 'controls':
            return {'controls':[{'role':n['role']['value'],'name':n.get('name',{}).get('value',''), 'properties':n.get('properties',[])} for n in await self.controls()]}
        if action == 'click':
            await self.click('button', command['name'])
            return {'clicked':command['name']}
        if action == 'notice':
            label = 'I confirm I have read and understood this Notice.'
            await self.click('checkbox', label)
            await self.click('button', 'Continue')
            return {'notice_acknowledged':True}
        if action == 'retry_read':
            text = await self.page.locator('body').inner_text()
            if 'We are unable to hear you.' not in text:
                raise ValueError('expected audio retry dialog required')
            if not await self.page.evaluate('globalThis.__qaDynamicReadAloud?.retryCurrent()'):
                raise ValueError('same-question cached retry unavailable')
            await self.click('button', 'TRY AGAIN')
            return {'retry_started':True}
        if action == 'screenshot':
            path = self.output / 'current.png'
            await self.page.screenshot(path=str(path))
            return {'screenshot':str(path.resolve())}
        if action == 'close':
            return {'close':True}
        raise ValueError('unknown command')


async def selected_url(args):
    if not args.source_browser:
        from qa_bot.jobfinder_source import selected_invitation
        return await asyncio.to_thread(selected_invitation,args.selected_profile,
            os.environ.get('JOBFINDER_LOGIN',''),os.environ.get('JOBFINDER_PASSWORD',''))
    async with async_playwright() as pw:
        source = await pw.chromium.connect_over_cdp(args.source_browser)
        pages = [p for c in source.contexts for p in c.pages
                 if p.url.startswith('https://jobs.systeam.kz/mail/candidates')]
        if len(pages) != 1:
            raise ValueError('one source JobFinder page required')
        page = pages[0]
        link = page.get_by_role('link', name='To start your test click here', exact=True)
        if await link.count() != 1:
            raise ValueError('one already selected invitation required')
        owner=await link.evaluate("a=>a.closest('.cg-card')?.querySelector('.cg-name')?.textContent.trim()")
        if owner != args.selected_profile:
            raise ValueError('invitation does not belong to the selected profile')
        url = await link.get_attribute('href')
        if not url or not url.startswith(ORIGIN + '/'):
            raise ValueError('unexpected assessment origin')
        return url


async def run(args):
    token = os.environ.get('QA_LOCAL_BRIDGE_TOKEN', '')
    if len(token) < 24:
        raise ValueError('QA_LOCAL_BRIDGE_TOKEN required')
    url=await selected_url(args)  # Disconnect the read-only source client first.
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel='chrome', headless=False,
            args=['--disable-backgrounding-occluded-windows','--disable-renderer-backgrounding'])
        try:
            context = await browser.new_context()
            await context.grant_permissions(['local-network-access'], origin=ORIGIN)
            await context.add_init_script(script=bundle(token,args.profile_id,args.test))
            page = await context.new_page()
            session = Session(page, args.output)
            session.cdp = await context.new_cdp_session(page)
            session.project_root=Path(__file__).resolve().parents[2]
            session.prompt_dir=args.prompt_dir
            session.profile_id=args.profile_id;session.test_id=args.test
            if args.auto:
                session.auto_speech=session.auto_choices=session.auto_navigation=True
            page.on('response', session.response)
            page.on('pageerror', lambda error: session.log('page-errors.jsonl', {'time':time.time(),'error':safe_text(error)[:1000],'stack':safe_text(getattr(error,'stack',''))[:2000]}))
            # Exactly one owner handles native beforeunload dialogs.
            page.on('dialog', lambda d: asyncio.create_task(d.accept() if d.type == 'beforeunload' else d.dismiss()))
            await page.goto(url)
            watcher = asyncio.create_task(session.watch())
            print(json.dumps({'ready':True,'selected_profile':args.selected_profile}), flush=True)
            try:
                while True:
                    line = await asyncio.to_thread(input)
                    try:
                        result = await session.command(json.loads(line))
                        print(json.dumps({'result':result}, ensure_ascii=False), flush=True)
                        if result.get('close'):break
                    except Exception as error:
                        print(json.dumps({'error':safe_text(error)[:500]}), flush=True)
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                if session.tasks:await asyncio.gather(*session.tasks,return_exceptions=True)
        finally:
            await browser.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-browser')
    parser.add_argument('--selected-profile',required=True)
    parser.add_argument('--profile-id',required=True)
    parser.add_argument('--test',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--prompt-dir',type=Path,required=True)
    parser.add_argument('--auto',action='store_true',help='run the explicitly authorized assessment flow')
    args=parser.parse_args()
    try:asyncio.run(run(args))
    except (KeyboardInterrupt,EOFError):pass


if __name__ == '__main__':main()
