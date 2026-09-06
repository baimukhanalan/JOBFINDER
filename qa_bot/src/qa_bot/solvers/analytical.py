"""Observed question UI, image-backed reasoning and guarded native submission."""
import asyncio
from dataclasses import replace
import hashlib
import importlib
import json
from pathlib import Path
import re
import shutil
import time
from urllib.parse import urlsplit
from qa_bot.domain.question import QuestionSpec,OptionSpec,AssetRef,ResponseContract,ResponseKind
from qa_bot.knowledge.bank import QuestionBank,fingerprint
from qa_bot.knowledge.live_archive import session_archive

async def fetch_question_image(session, url, number):
    """Retry a transient read failure only while the same question remains."""
    from playwright.async_api import Error as BrowserError
    from qa_bot.live_session import STATE_SCRIPT
    for attempt in range(3):
        fresh=await session.page.evaluate(STATE_SCRIPT)
        if fresh.get('number')!=number:raise ValueError('question changed during image fetch')
        try:
            response=await session.page.request.get(url,timeout=10000)
            if response.status!=200:
                if response.status in (500,502,503,504) and attempt<2:
                    await asyncio.sleep(.3*(attempt+1));continue
                raise ValueError('question image unavailable')
            return await response.body(), response.headers.get('content-type','').split(';')[0]
        except BrowserError:
            if attempt==2:raise
            session.log('image-fetch-retries.jsonl',{'time':time.time(),'number':number,'attempt':attempt+1,'reason':'transient_browser_transport'})
            await asyncio.sleep(.3*(attempt+1))

async def label_info(session,node):
    resolved=await session.cdp.send('DOM.resolveNode',{'backendNodeId':node})
    try:
        result=await session.cdp.send('Runtime.callFunctionOn',{'objectId':resolved['object']['objectId'],'functionDeclaration':'''function(){const p=this.parentElement,inputs=p.querySelectorAll('input'),input=inputs.length===1?inputs[0]:null;return {text:this.innerText.trim(),checked:p.querySelector('input')?.checked,selected:p.getAttribute('aria-selected'),image:!!this.querySelector('img'),input_count:inputs.length,input_value:input?input.value:null,input_value_attribute:input?input.getAttribute('value'):null}}''','returnByValue':True})
        return result['result']['value']
    finally:await session.cdp.send('Runtime.releaseObject',{'objectId':resolved['object']['objectId']})

def selected_input_evidence(info,*,question,option,index):
    """Bind the clicked control to the extracted question without leaking values."""
    result={'question_id':question.question_id,'question_content_hash':question.content_hash,
        'selected_option_id':option.id,'selected_option_position':index+1,
        'selected_input_count':info.get('input_count'),'selection_observed_at':time.time()}
    for key in ('input_value','input_value_attribute'):
        value=info.get(key)
        result['selected_'+key+'_kind']='null' if value is None else type(value).__name__
        encoded=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
        result['selected_'+key+'_json_sha256']=hashlib.sha256(encoded).hexdigest()
        if isinstance(value,str):
            result['selected_'+key+'_sha256']=hashlib.sha256(value.encode()).hexdigest()
            if re.fullmatch(r'(?:[0-9]{1,6}|[A-Ha-h])',value):result['selected_'+key]=value
            if re.fullmatch(r'[0-9]{1,6}',value) and int(value)<=100000:
                result['selected_'+key+'_numeric_json_sha256']=hashlib.sha256(str(int(value)).encode()).hexdigest()
    return result


async def options(session):
    nodes=await session.controls('LabelText')
    result=[]
    for node in nodes:
        info=await label_info(session,node['backendDOMNodeId'])
        if info['text'] or info['image']:result.append({'node':node['backendDOMNodeId'],**info})
    return result

async def click_node(session,node):
    await session.cdp.send('DOM.scrollIntoViewIfNeeded',{'backendNodeId':node})
    quad=(await session.cdp.send('DOM.getBoxModel',{'backendNodeId':node}))['model']['content']
    x,y=sum(quad[::2])/4,sum(quad[1::2])/4
    for kind in ('mousePressed','mouseReleased'):
        await session.cdp.send('Input.dispatchMouseEvent',{'type':kind,'x':x,'y':y,'button':'left','clickCount':1})

def question_text(text):
    text=re.sub(r'^Skip to main content\s*\n\d{1,2}\s*:\s*\d{2}\s*\nHelp\s*\nExit\s*\n','',text)
    return re.sub(r'\n1\n2\n3\n[\s\S]*','',text).strip()

async def run_module(session,module):
    from qa_bot.live_session import STATE_SCRIPT,safe_text
    from qa_bot.adapters.llm import codex_cli
    from qa_bot.solvers import engine,calculator
    importlib.reload(codex_cli);importlib.reload(calculator);importlib.reload(engine)
    if module in ('computer_inspect','computer') or module.startswith('computer_step:'):
        from qa_bot.solvers import computer
        importlib.reload(computer)
        await computer.run(session,module)
        return
    if module in ('accept_terms','diagnostic_start'):
        from qa_bot.solvers import onboarding
        importlib.reload(onboarding)
        await (onboarding.accept_terms(session) if module=='accept_terms' else onboarding.run_diagnostic(session))
        return
    if module=='sales_inspect':
        print(json.dumps({'sales_dom':await session.page.locator('#main-q-parent').inner_html()}),flush=True)
        return
    if module=='sales':
        from qa_bot.solvers import sales
        importlib.reload(sales)
        await sales.run_sales(session)
        return
    if module=='writex':
        from qa_bot.solvers import writex
        importlib.reload(writex)
        await writex.run_writex(session)
        return
    if module=='stop':
        targets=[t for t in session.tasks if t is not asyncio.current_task() and getattr(t.get_coro(),'__name__','')=='run_module']
        for t in targets:t.cancel()
        await asyncio.gather(*targets,return_exceptions=True)
        print(json.dumps({'module_tasks_stopped':len(targets)}),flush=True)
        return
    if module=='inspect':
        print(json.dumps({'images':await session.page.locator('#main-q-parent img').evaluate_all('(nodes)=>nodes.filter(n=>n.getClientRects().length).map(n=>({src:n.currentSrc,html:n.outerHTML}))')}),flush=True)
        return
    if module!='analytical':raise ValueError('unsupported observed module')
    seen=set()
    try:
        for _ in range(19):
            state=await session.page.evaluate(STATE_SCRIPT)
            if 'Assessments\n' in state['text']:break
            number=state.get('number');text=question_text(state['text'])
            if not number or number in seen or not any(x in text for x in ('Choose the correct option.','Refer to the data presented and answer the question.')):raise ValueError('expected new analytical question')
            opts=await options(session)
            if not 2<=len(opts)<=8:raise ValueError('ambiguous analytical choices')
            labels=[o['text'] or 'Image option '+str(i) for i,o in enumerate(opts,1)]
            await session.page.wait_for_function("[...document.querySelectorAll('#main-q-parent img')].filter(n=>n.getClientRects().length).every(n=>n.complete&&n.naturalWidth>0&&n.currentSrc)",timeout=10000)
            snapshot=session.output/f'analytical-{number}.png'
            data=await session.page.screenshot(path=str(snapshot),full_page=True)
            assets=[AssetRef('question-image','image/png',str(snapshot.resolve()),hashlib.sha256(data).hexdigest())]
            image_urls=await session.page.locator('#main-q-parent img').evaluate_all('(nodes)=>nodes.filter(n=>n.getClientRects().length).map(n=>n.currentSrc)')
            for i,url in enumerate(dict.fromkeys(image_urls),1):
                parsed=urlsplit(url)
                if parsed.scheme!='https' or parsed.hostname not in ('s3.amazonaws.com','qbdata-amcat.s3.amazonaws.com'):
                    raise ValueError('unrecognized question image origin')
                body,media=await fetch_question_image(session,url,number)
                if media not in ('image/png','image/jpeg') or not 0<len(body)<=20_000_000:raise ValueError('question image format rejected')
                target=session.output/f'analytical-{number}-figure-{i}{".png" if media=="image/png" else ".jpg"}'
                target.write_bytes(body)
                assets.append(AssetRef('figure-'+str(i),media,str(target.resolve()),hashlib.sha256(body).hexdigest()))
            q=QuestionSpec('analytical-'+number,'authorized-single','Basic Analytical Ability','ANALYTICAL-MCQ',text,ResponseContract(ResponseKind.SINGLE_CHOICE,1,1),'pending',
                instruction='Choose the correct option. Read the attached full question image, including all figures, list numbering and option images.',
                options=tuple(OptionSpec('option-'+str(i),i,label) for i,label in enumerate(labels,1)),
                assets=tuple(assets),completeness=True,extraction_confidence=1)
            q=replace(q,content_hash=fingerprint(q)[0])
            isolated=session.output/'isolated';isolated.mkdir(exist_ok=True)
            client=codex_cli.CodexCLIClient(Path(shutil.which('codex')),session.project_root/'configs/answer_schema.json',isolated,reasoning_effort='low')
            historical=None
            if not image_urls and 'PASSAGE' not in state['text']:
                from qa_bot.knowledge.historical import HistoricalAnswerResolver
                pure=session.page.locator('#simpleMcqQuesContainer')
                if await pure.count()==1:
                    plain=await pure.inner_text()
                    heading=await session.page.locator('h1.direction').all_inner_texts()
                    if len(heading)==1:
                        q=replace(q,question_text=plain,instruction=heading[0].strip())
                        q=replace(q,content_hash=fingerprint(q)[0])
                        root=session.project_root.parent
                        historical=HistoricalAnswerResolver.from_files(root/'data/questions.jsonl',root/'SHL_answers_all.csv')
            with QuestionBank(session.output/'analytical.sqlite3') as bank, session_archive(session,ignored_observation_assets=('question-image',)) as archive:
                answer=await engine.AnswerEngine(bank,client,historical=historical,archive=archive).propose(q,authorized_qa=True,allow_model=not getattr(session,'replay_only',False),timeout=45)
            retryable_uncertainty = (answer.reason in ('model_abstained', 'low_confidence')
                                    or 'numeric option absent or ambiguous' in answer.reason)
            if not answer.proposal and retryable_uncertainty and not getattr(session,'replay_only',False):
                review_state=await session.page.evaluate(STATE_SCRIPT)
                timer=re.search(r'^Skip to main content\s*\n(\d{1,2})\s*:\s*(\d{2})',review_state['text'])
                remaining=int(timer[1])*60+int(timer[2]) if timer else 0
                if review_state.get('number')==number and remaining>50:
                    initial_reason=answer.reason
                    reviewer=codex_cli.CodexCLIClient(Path(shutil.which('codex')),session.project_root/'configs/answer_schema.json',isolated,reasoning_effort='medium',best_effort=True)
                    with QuestionBank(session.output/'analytical.sqlite3') as bank:
                        answer=await engine.AnswerEngine(bank,reviewer).propose(q,authorized_qa=True,timeout=45,best_effort=True)
                    session.log('analytical-uncertainties.jsonl',{'time':time.time(),'number':number,
                        'question_id':q.question_id,'question_content_hash':q.content_hash,
                        'initial_reason':initial_reason,'review_source':answer.source,
                        'review_reason':answer.reason,'confidence':answer.proposal.confidence if answer.proposal else None,
                        'correctness_verified':False,'saved_to_shared_answers':False})
            if not answer.proposal:raise ValueError(answer.reason)
            selected=next(i for i,o in enumerate(q.options) if o.id==answer.proposal.selections[0].option_id)
            fresh=await session.page.evaluate(STATE_SCRIPT);fresh_opts=await options(session)
            if fresh.get('number')!=number or question_text(fresh['text'])!=text or [(o['text'],o['image']) for o in fresh_opts]!=[(o['text'],o['image']) for o in opts]:raise ValueError('question changed during solving')
            if await session.page.locator('#main-q-parent img').evaluate_all('(nodes)=>nodes.filter(n=>n.getClientRects().length).map(n=>n.currentSrc)')!=image_urls:raise ValueError('question image changed')
            await click_node(session,fresh_opts[selected]['node'])
            info=await label_info(session,fresh_opts[selected]['node'])
            if not info['checked'] and info['selected']!='true':raise ValueError('selection not checked')
            fresh=await session.page.evaluate(STATE_SCRIPT)
            if fresh.get('number')!=number:raise ValueError('question changed before submit')
            evidence=selected_input_evidence(info,question=q,option=q.options[selected],index=selected)
            await session.click('button','SUBMIT ANSWER')
            session.log('analytical.jsonl',{'number':number,'question':text,'selected':labels[selected],'confidence':answer.proposal.confidence,'source':answer.source,'image_sha256':q.assets[0].sha256,'correctness_verified':False,**evidence})
            print(json.dumps({'analytical_submitted':number,'confidence':answer.proposal.confidence}),flush=True)
            seen.add(number)
            for attempt in range(150):
                await asyncio.sleep(.1)
                fresh=await session.page.evaluate(STATE_SCRIPT)
                if fresh.get('number')!=number:break
            else:raise ValueError('submission did not advance')
            await asyncio.sleep(.3)
        print(json.dumps({'analytical_stopped':len(seen)}),flush=True)
    except Exception as error:
        session.log('analytical-errors.jsonl',{'error':safe_text(error)[:400]})
        print(json.dumps({'analytical_blocked':safe_text(error)[:400]}),flush=True)
        # One bounded recovery on the same unanswered question; never skip it.
        if str(error)=='question changed during solving' or str(error).startswith('model_or_validation_failed:'):
            fresh=await session.page.evaluate(STATE_SCRIPT)
            retries=getattr(session,'analytical_retries',{})
            timer=re.search(r'^Skip to main content\s*\n(\d{1,2})\s*:\s*(\d{2})',fresh['text'])
            remaining=int(timer[1])*60+int(timer[2]) if timer else 0
            if fresh.get('number')==number and retries.get(number,0)<1 and remaining>60:
                retries[number]=retries.get(number,0)+1;session.analytical_retries=retries
                session.log('analytical-retries.jsonl',{'number':number,'reason':safe_text(error)[:300]})
                await asyncio.sleep(.3)
                await run_module(session,'analytical')
