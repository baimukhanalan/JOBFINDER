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
    from qa_bot.runtime_ports import loopback_port,loopback_url
    return '\n'.join((
        SharedMicrophoneBridge(ORIGIN,idle_floor=.001).init_script(),
        recording_observer_script(ORIGIN, token,port=loopback_port('QA_RECORDING_CAPTURE_PORT',18772)),
        topic_bridge_script(ORIGIN),
        PlayedPromptLoopback(ORIGIN,token,profile,test,capture_url=loopback_url('QA_PROMPT_CAPTURE_PORT',18771)).init_script(),
        DynamicReadAloudBridge(loopback_url('QA_SPEECH_PORT',18769), token, ORIGIN, '/',
            profile, test, auto_detect=True, question_counter_selector='button.currentQue',
            suspension_selector='[id^="ngdialog"]',section_heading='Section A: Read and Speak').init_script(),
    ))


STATE_SCRIPT = r'''() => {
 const r=globalThis.__qaDynamicReadAloud,b=globalThis.__qaMicrophoneBus,
       l=globalThis.__qaDirectAudioLoopback,t=globalThis.__qaTopicSpeech;
 return {text:document.body.innerText.slice(0,4000),
   number:(()=>{const nodes=[...document.querySelectorAll('.currentQue')].filter(n=>n.getClientRects().length);return nodes.length===1?(nodes[0].innerText.trim().match(/^\d+/)||[])[0]||null:null})(),
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


def control_name(role,name):
    if role=='button':name=re.sub(r'[\ue000-\uf8ff]','',name)
    return ' '.join(name.split()).casefold()


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
        self.typing_tasks=set()
        self.personality_tasks=set()
        self.module_tasks=set()
        self.timeout_seen=False
        self.preflight=False
        self.output.mkdir(parents=True, exist_ok=True)

    async def click(self, role, name):
        # Native accessibility also covers the platform's closed shadow buttons.
        tree = await self.cdp.send('Accessibility.getFullAXTree')
        nodes = [n for n in tree['nodes'] if not n.get('ignored')
                 and n.get('role', {}).get('value') == role
                 and control_name(role,n.get('name', {}).get('value', '')) == control_name(role,name)
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
        from qa_bot.audio.transcript_cache import TranscriptCache, natively_model_identity
        digest=item['sha256'];target=(self.prompt_dir/item['file']).resolve()
        if not target.is_relative_to(self.prompt_dir.resolve()):raise ValueError('prompt path rejected')
        encoded=target.read_bytes()
        if hashlib.sha256(encoded).hexdigest()!=digest:raise ValueError('prompt integrity failure')
        bank_dir=getattr(self,'speech_bank_dir',None) or self.prompt_dir.parent
        cache=TranscriptCache(bank_dir/'transcripts')
        stt=NativelyLocalSTT(self.project_root)
        identity=await asyncio.to_thread(natively_model_identity,stt)
        async def transcribe_new():
            wav=self.output/(digest+'.wav')
            # Decode from verified original bytes; never trust an old per-run WAV.
            wav.write_bytes(await decode_to_wav(self.page,encoded))
            async with self.stt_lock:
                return await stt.transcribe_wav(wav,timeout=90)
        result=await cache.get_or_transcribe(encoded,model_identity=identity,transcribe=transcribe_new,
            source={'profile':self.profile_id,'test':self.test_id,'question':item.get('id','')})
        text=result.text
        (self.output/(digest+'.txt')).write_text(text)
        self.log('transcripts.jsonl',{'time':time.time(),**item,'transcript':text,
            'cache_source':result.source,'model_sha256':result.model_sha256,'text_sha256':result.text_sha256})
        print(json.dumps({'transcript_ready':item['id'],'words':len(text.split()),'source':result.source}),flush=True)
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
            bank_dir=getattr(self,'speech_bank_dir',None) or self.prompt_dir.parent
            with SpeechReplayBank(bank_dir/'speech.sqlite3',bank_dir/'answers') as bank:
                async def create_answer():
                    isolated=self.output/'isolated';isolated.mkdir(exist_ok=True)
                    client=CodexCLIClient(Path(shutil.which('codex')),self.project_root/'configs/answer_schema.json',isolated)
                    solver=SpokenTextSolver(client,min_words=75)
                    answer=await solver('Prepare a natural English spoken response of 80 to 95 words for this authorized test topic. Use complete sentences, a clear example and a short conclusion. Output only the spoken words in the text field. Topic: '+prompt,timeout=20)
                    if len(answer.split())>105:raise ValueError('topic answer too long')
                    return answer
                replay=await bank.get_or_create_spoken(prompt,answer_factory=create_answer,
                    speech=MacOSLocalTTS(),voice='Samantha',model='macos-say',
                    source_profile=self.profile_id,source_test=self.test_id,source_question='topic-'+number,
                    timeout=10,replay_only=getattr(self,'replay_only',False))
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

    @staticmethod
    def typing_passage(state):
        match=re.search(r'Type the given (?:sentence|text|paragraph) EXACTLY[^\n]*\n(.*?)\n\d{1,2}\s*:\s*\d{2}\s*Time Left',state['text'],re.S)
        return match.group(1) if match and 5<=len(match.group(1))<=10000 else None

    async def type_passage(self,state,passage):
        number=state['number']
        try:
            field=self.page.locator('textarea.typingTextArea')
            if await field.count()!=1:raise ValueError('one typing input required')
            existing=await field.input_value()
            if not passage.startswith(existing):raise ValueError('typing input conflicts with source')
            await field.press_sequentially(passage[len(existing):],delay=30,timeout=60000)
            fresh=await self.page.evaluate(STATE_SCRIPT)
            actual=await field.input_value()
            if fresh.get('number')!=number or self.typing_passage(fresh)!=passage or actual!=passage:
                raise ValueError('typing result or question changed')
            for role in ('button','link'):
                try:await self.click(role,'SUBMIT ANSWER');break
                except ValueError:continue
            else:raise ValueError('typing submit unavailable')
            self.log('typing.jsonl',{'number':number,'characters':len(passage),'exact_match':True,
                'sha256':hashlib.sha256(passage.encode()).hexdigest(),'practice':'only for practice' in state['text']})
            print(json.dumps({'typing_submitted':number,'characters':len(passage),'exact_match':True}),flush=True)
        except Exception as error:
            self.log('typing-errors.jsonl',{'number':number,'error':safe_text(error)[:300]})
            print(json.dumps({'typing_blocked':number,'reason':safe_text(error)[:300]}),flush=True)

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
            from qa_bot.knowledge.live_archive import session_archive
            with QuestionBank(self.output/'listening.sqlite3') as bank, session_archive(self) as archive:
                answer=await AnswerEngine(bank,client,archive=archive).propose(q,authorized_qa=True,allow_model=not getattr(self,'replay_only',False),timeout=40)
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
                if 'Your test is now complete. Thank you!' in state['text'] and not getattr(self,'final_captured',False):
                    self.final_captured=True
                    try:
                        await self.page.screenshot(path=str(self.output/'final.png'))
                        self.log('final-page.jsonl',{'time':time.time(),'text':state['text'],'screenshot':'final.png','completeness_audited':False})
                    except Exception as error:
                        self.log('observer-errors.jsonl',{'type':'final_screenshot','error':safe_text(error)[:200]})
                if 'Assessment Time out' in state['text'] and not self.timeout_seen:
                    self.timeout_seen=True
                    self.auto_navigation=self.auto_choices=self.auto_speech=False
                    for task in tuple(self.tasks):task.cancel()
                    self.log('run-failures.jsonl',{'time':time.time(),'reason':'assessment_time_out','number':state.get('number')})
                    print(json.dumps({'run_failed':'assessment_time_out'}),flush=True)
                if self.auto_choices and state.get('number'):
                    module=None
                    if "System Diagnostic Tool." in state["text"]:module="diagnostic_start"
                    elif re.search(r"QUESTION\n\d+ out of 16\n",state["text"]) and await self.page.locator("#frame").count()==1:module="computer"
                    elif "Choose the 'best' and the 'worst' action for the given situation." in state['text']:module='sales'
                    elif 'Compose an email response for the topic provided.' in state['text']:module='writex'
                    elif 'Section ' not in state['text'] and any(x in state['text'] for x in ('Choose the correct option.','Refer to the data presented and answer the question.')):module='analytical'
                    if module and module not in self.module_tasks:
                        self.module_tasks.add(module)
                        from qa_bot.solvers.analytical import run_module
                        task=asyncio.create_task(run_module(self,module));self.tasks.add(task);task.add_done_callback(self.tasks.discard)
                if self.auto_choices and 'Select an option with which you agree the most.' in state['text'] and state.get('number') not in self.personality_tasks:
                    self.personality_tasks.add(state.get('number'))
                    from qa_bot.solvers.personality import run_personality
                    task=asyncio.create_task(run_personality(self,state));self.tasks.add(task);task.add_done_callback(self.tasks.discard)
                passage=self.typing_passage(state)
                typing_key=(state.get('number'),passage)
                if self.auto_speech and passage and typing_key not in self.typing_tasks:
                    self.typing_tasks.add(typing_key)
                    task=asyncio.create_task(self.type_passage(state,passage));self.tasks.add(task);task.add_done_callback(self.tasks.discard)
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
                        elif not self.preflight and (('Device Testing' in state['text'] and 'This is a sample question to check your device compatibility.' in state['text']) or ('Device testing successful' in state['text'] and 'Your device is compatible.' in state['text'])):
                            try:await self.click('button','OK')
                            except ValueError:pass
                        elif not self.preflight and 'Device Testing' in state['text'] and 'Were you able to hear the question audio clearly?' in state['text'] and any(x.get('duration',0)>0 for x in state.get('heard',[])):
                            try:await self.click('button','YES')
                            except ValueError:pass
                        elif not self.preflight and state.get('number')=='1' and 'Click NEXT if you can hear your voice clearly' in state['text'] and (state.get('read') or {}).get('siteId')=='navigation:1' and (state.get('read') or {}).get('replays',0)>0 and (state.get('microphone') or {}).get('signal',0)>2:
                            try:await self.click('button','NEXT')
                            except ValueError:
                                if not getattr(self,'device_play_started',False):
                                    try:
                                        await self.click('button','Play')
                                        self.device_play_started=True
                                    except ValueError:pass
                        elif not self.preflight and (('Assessments\n' in state['text'] and 'Upcoming' in state['text']) or ('ASSESSMENT DESCRIPTION' in state['text'] and any(x in state['text'] for x in ('Typing','Basic Analytical Ability','SVAR - Spoken English','Basic Computer Literacy Simulation (Windows 10)')))):
                            try:await self.click('button','NEXT')
                            except ValueError:pass
                        elif not self.preflight and 'Section ' in state['text'] and 'Listen Carefully' in state['text'] and 'NEXT' in state['text']:
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
                    observed=await self.page.evaluate('globalThis.__qaDirectAudioLoopback.observedSources.filter(s=>s.player==="html" && s.path.includes("/stimulus/"))')
                    captured_paths={s['path'] for s in passages}
                    conversation_complete=bool(observed) and all(s['path'] in captured_paths and s.get('endedAt') is not None for s in observed)
                    radios=await self.controls('radio')
                    if len(radios)>=2 and conversation_complete:
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
        if self.preflight and action in ('auto_speech','auto_choices','auto_modules','retry_read'):
            raise ValueError('preflight cannot enable answer automation')
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
        if action == 'ax':
            tree=await self.cdp.send('Accessibility.getFullAXTree')
            return {'nodes':[{'role':n.get('role',{}).get('value'),'name':n.get('name',{}).get('value'),'id':n.get('backendDOMNodeId'),'properties':n.get('properties',[])} for n in tree['nodes'] if not n.get('ignored') and n.get('role',{}).get('value') not in ('button','RootWebArea','generic')]}
        if action == 'node_html':
            result=await self.cdp.send('DOM.getOuterHTML',{'backendNodeId':command['id']})
            return {'html':safe_text(result['outerHTML'])[:20000]}
        if action == 'auto_modules':
            import importlib
            from qa_bot.solvers import analytical
            importlib.reload(analytical)
            task=asyncio.create_task(analytical.run_module(self,command['module']))
            self.tasks.add(task);task.add_done_callback(self.tasks.discard)
            return {'started':command['module']}
        if action == 'question_dom':
            return await self.page.evaluate('''() => [...document.querySelectorAll('.question,.radio-outer,input,textarea,[role=slider]')].filter(n=>n.getClientRects().length).map(n=>n.outerHTML).join('\\n').slice(0,20000)''')
        if action == 'form_state':
            return await self.page.evaluate('''() => ({
                fields:[...document.querySelectorAll('textarea,input,[contenteditable=true]')].filter(n=>n.getClientRects().length).map(n=>({tag:n.tagName,id:n.id,type:n.type,name:n.name,value:n.value,html:n.outerHTML})),
                paragraphs:[...document.querySelectorAll('p,[id]')].filter(n=>n.getClientRects().length&&n.innerText?.trim()&&n.innerText.length<2500).map(n=>({tag:n.tagName,id:n.id,class:n.className,text:n.innerText})).slice(-80)})''')
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
            nodes=[n for n in await self.controls('checkbox') if n.get('name',{}).get('value','').strip()==label]
            if len(nodes)!=1:raise ValueError('one exact notice checkbox required')
            checked=any(p['name']=='checked' and p['value'].get('value') in (True,'true') for p in nodes[0].get('properties',[]))
            if not checked:await self.click('checkbox', label)
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
            if not args.preflight:
                await context.add_init_script(script=bundle(token,args.profile_id,args.test))
            page = await context.new_page()
            session = Session(page, args.output)
            session.cdp = await context.new_cdp_session(page)
            session.project_root=Path(__file__).resolve().parents[2]
            session.answer_archive_path=session.project_root/'runs/answer-archive.sqlite3'
            session.prompt_dir=args.prompt_dir
            session.speech_bank_dir=getattr(args,'speech_bank_dir',None)
            session.replay_only=getattr(args,'replay_only',False)
            session.profile_id=args.profile_id;session.test_id=args.test
            session.preflight=args.preflight
            if not args.preflight:
                async def capture_played(item):
                    from qa_bot.audio import played_capture
                    import importlib
                    importlib.reload(played_capture)
                    return await played_capture.capture_html(session,item)
                await page.expose_function('__qaCapturePlayedAudio',capture_played)
            if args.preflight:session.auto_navigation=True
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
                        if isinstance(result,dict) and result.get('close'):break
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
    parser.add_argument('--speech-bank-dir',type=Path,help='shared speech.sqlite3 and answers directory')
    parser.add_argument('--replay-only',action='store_true',help='use exact prior answers; never call a reasoning model')
    parser.add_argument('--preflight',action='store_true',help='observe initial state without speech bridges or answer automation')
    parser.add_argument('--auto',action='store_true',help='run the explicitly authorized assessment flow')
    args=parser.parse_args()
    if args.preflight and args.auto:parser.error("preflight cannot send answers")
    try:asyncio.run(run(args))
    except (KeyboardInterrupt,EOFError):pass


if __name__ == '__main__':main()
