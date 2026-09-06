"""Visible controls in the observed Windows simulation frame."""
import json
import asyncio
from urllib.parse import urlsplit

MESSAGE_SCRIPT=r"""()=>{
 if(window.__qaSimulationEvidenceInstalled)return;
 window.__qaSimulationEvidenceInstalled=true;window.__qaSimulationMessages=[];
 addEventListener('message',e=>{
  if(e.source!==document.querySelector('#frame')?.contentWindow)return;
  let d=e.data;if(typeof d==='string'){try{d=JSON.parse(d)}catch{return}}
  if(!d||typeof d!=='object')return;
  const safe=/^[A-Za-z0-9 _.,:-]{1,100}$/;
  let budget=200;
  const sanitize=(value,depth)=>{
   if(depth>3||--budget<0)return '[redacted]';
   if(value===null||typeof value==='boolean')return value;
   if(typeof value==='number')return Number.isFinite(value)?value:'[redacted]';
   if(typeof value==='string')return safe.test(value)&&!/^(?:https?|ftp|file|data|javascript):/i.test(value)?value:'[redacted]';
   if(Array.isArray(value))return value.slice(0,20).map(item=>sanitize(item,depth+1));
   if(typeof value==='object'){
    const clean=Object.create(null);
    for(const [key,item] of Object.entries(value).slice(0,20)){
     if(safe.test(key))clean[key]=sanitize(item,depth+1);
    }
    return clean;
   }
   return '[redacted]';
  };
  const fields=sanitize(d,0);
  const match=document.querySelector('.currentQue')?.innerText?.match(/^\s*(\d+)/);
  window.__qaSimulationMessages.push({sequence:window.__qaSimulationMessages.length+1,
   question:match?match[1]:null,time:Date.now(),fields});
 });
}"""

async def install_message_evidence(session):
    await session.page.evaluate(MESSAGE_SCRIPT)


async def capture_message_evidence(session):
    await install_message_evidence(session)
    events=await session.page.evaluate('window.__qaSimulationMessages||[]')
    cursor=getattr(session,'computer_message_cursor',0)
    for event in events[cursor:]:session.log('computer-events.jsonl',event)
    session.computer_message_cursor=len(events)
    return events


async def click_confirmation_submit(session,dialog):
    """Click only SUBMIT inside the exact dialog, including closed shadow UI."""
    submit=dialog.get_by_role('button',name='SUBMIT',exact=True)
    count=await submit.count()
    if count==1:
        if not await submit.is_enabled():raise ValueError('one enabled final simulation confirmation submit required')
        await submit.click();return
    if count>1:raise ValueError('one enabled final simulation confirmation submit required')
    from qa_bot.live_session import control_name
    dialog_id=await dialog.get_attribute('id')
    if not dialog_id or not dialog_id.startswith('ngdialog'):raise ValueError('exact confirmation dialog required')
    tree=await session.cdp.send('Accessibility.getFullAXTree')
    candidates=[]
    for node in tree['nodes']:
        if node.get('ignored') or node.get('role',{}).get('value')!='button':continue
        if control_name('button',node.get('name',{}).get('value',''))!='submit':continue
        if any(p['name']=='disabled' and p['value'].get('value') for p in node.get('properties',[])):continue
        backend=node.get('backendDOMNodeId')
        if not backend:continue
        resolved=await session.cdp.send('DOM.resolveNode',{'backendNodeId':backend})
        object_id=resolved['object']['objectId']
        try:
            scoped=await session.cdp.send('Runtime.callFunctionOn',{'objectId':object_id,
                'functionDeclaration':"function(id){if(!this.isConnected)return false;for(let n=this;n;n=n.parentNode||(n.getRootNode&&n.getRootNode().host)||null){if(n.id===id)return !!n.getClientRects().length&&n.innerText.includes('Are you ready to submit this assessment?')}return false}",
                'arguments':[{'value':dialog_id}],'returnByValue':True})
            if scoped.get('result',{}).get('value') is True:candidates.append(backend)
        finally:await session.cdp.send('Runtime.releaseObject',{'objectId':object_id})
    if len(candidates)!=1:raise ValueError('one enabled final simulation confirmation submit required')
    await session.cdp.send('DOM.scrollIntoViewIfNeeded',{'backendNodeId':candidates[0]})
    box=await session.cdp.send('DOM.getBoxModel',{'backendNodeId':candidates[0]})
    quad=box['model']['content'];x=sum(quad[::2])/4;y=sum(quad[1::2])/4
    for kind in ('mousePressed','mouseReleased'):
        await session.cdp.send('Input.dispatchMouseEvent',{'type':kind,'x':x,'y':y,'button':'left','clickCount':1})


async def run(session,module):
    frames=[f for f in session.page.frames if urlsplit(f.url).path=='/assets/msOfficeSimulation/run.html']
    if len(frames)!=1:raise ValueError('one simulation frame required')
    frame=frames[0]
    await install_message_evidence(session)
    if module=='computer':
        try:await automate(session,frame)
        except Exception as error:
            from qa_bot.live_session import safe_text
            session.log('computer-errors.jsonl',{'error':safe_text(error)[:300]})
            print(json.dumps({'computer_blocked':safe_text(error)[:300]}),flush=True)
        return
    if module.startswith('computer_step:'):
        from qa_bot.live_session import STATE_SCRIPT
        action=json.loads(module.split(':',1)[1]);state=await session.page.evaluate(STATE_SCRIPT)
        if state.get('number')!=str(action['question']):raise ValueError('simulation question changed')
        target=frame.locator('[id='+json.dumps(action['id'])+']')
        if await target.count()!=1 or not await target.is_visible():raise ValueError('one visible simulation control required')
        if 'text' in action:
            if action.get('replace'):await target.fill('')
            await target.press_sequentially(action['text'],delay=60)
            if action.get('key'):await target.press(action['key'])
        elif action.get('double'):await target.dblclick()
        else:await target.click()
        session.log('computer-actions.jsonl',action)
        print(json.dumps({'computer_action':action}),flush=True)
        await asyncio.sleep(1.5)
        await run(session,'computer_inspect')
        return
    if module=='computer_inspect':
        state=await frame.evaluate("""()=>({text:document.body.innerText.slice(0,6000),nodes:[...document.querySelectorAll('input,button,a,[role=button],[role=textbox],textarea')].filter(n=>n.getClientRects().length).map(n=>({tag:n.tagName,id:n.id,role:n.getAttribute('role'),text:n.innerText?.slice(0,100),label:n.getAttribute('aria-label'),rect:(()=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})()})).slice(0,100)})""")
        from qa_bot.live_session import STATE_SCRIPT
        outer=await session.page.evaluate(STATE_SCRIPT)
        state['question']=outer['text'][:350]
        state['events']=await session.page.evaluate('window.__qaSimulationMessages||[]')
        await session.page.screenshot(path=str(session.output/'computer-current.png'))
        session.log('computer-states.jsonl',state)
        print(json.dumps({'computer_ui':state}),flush=True)
        return
    raise ValueError('unsupported simulation action')


NODES_SCRIPT="""()=>[...document.querySelectorAll('input,textarea,button,[role=button]')].filter(n=>{if(!n.id||!n.getClientRects().length||getComputedStyle(n).visibility==='hidden')return false;const r=n.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return hit===n||n.contains(hit)}).map(n=>{const r=n.getBoundingClientRect();return {id:n.id,tag:n.tagName,label:n.getAttribute('aria-label'),value:('value' in n)?n.value:null,rect:{x:r.x,y:r.y,width:r.width,height:r.height}}})"""


async def settle_after_action(session):
    """Wait for the resulting frame to settle, without a fixed 1.5 s delay."""
    import hashlib
    previous=None
    for _ in range(10):
        await asyncio.sleep(.15)
        if session.timeout_seen:return
        if await session.page.locator('#frame').count()!=1:return
        try:
            digest=hashlib.sha256(await session.page.locator('#frame').screenshot(timeout=1000)).hexdigest()
        except Exception as error:
            if any(x in str(error) for x in ('detached','Execution context was destroyed')):return
            raise
        if digest==previous:return
        previous=digest


async def automate(session,frame):
    from qa_bot.knowledge.computer_cache import ComputerActionCache
    await install_message_evidence(session)
    cache=ComputerActionCache(session.project_root/'runs/computer-actions.sqlite3')
    try:await _automate(session,frame,cache)
    finally:cache.close()


def completed_task_result(question,task,pending,events,now_ms):
    """Recognize a causally bound provider result without promoting a cache entry."""
    from qa_bot.knowledge.computer_protocol import outcome
    if not pending or any(json.loads(canonical).get('task')!=task for _,canonical,_ in pending):
        return False
    return outcome(question,pending,events,now_ms)['verification']=='verified_success'


async def _automate(session,frame,cache):
    import hashlib
    import re
    import shutil
    import time
    from pathlib import Path
    from qa_bot.live_session import STATE_SCRIPT,safe_text
    from qa_bot.adapters.llm.codex_cli import CodexCLIClient
    isolated=session.output/'isolated';isolated.mkdir(exist_ok=True)
    from qa_bot.knowledge.computer_cache import identity
    from qa_bot.run_audit import final_text_status
    client=None
    source_test=getattr(session,'test_id',None) or session.output.parent.name
    seen=set();previous=None;pending=[];active_frames=set();result_wait_started={}
    actions_applied=0;started_at=time.monotonic()
    terminal_since=None;confirmation_sent_at=None
    for step in range(2000):
        try:
            if time.monotonic()-started_at>20*60:raise ValueError('simulation runtime limit reached')
            events=await capture_message_evidence(session)
            state=await session.page.evaluate(STATE_SCRIPT)
            if session.timeout_seen or 'Assessment Time out' in state['text']:return
            if (state.get('number')=='16' or previous=='16') and 'Are you ready to submit this assessment?' in state['text']:
                terminal_since=None
                dialogs=session.page.locator('[id^="ngdialog"]').filter(has_text='Are you ready to submit this assessment?')
                visible=[dialogs.nth(i) for i in range(await dialogs.count()) if await dialogs.nth(i).is_visible()]
                if len(visible)!=1:raise ValueError('one visible final simulation confirmation required')
                if confirmation_sent_at is None:
                    fresh=await session.page.evaluate(STATE_SCRIPT)
                    if session.timeout_seen or 'Assessment Time out' in fresh['text']:return
                    if 'Are you ready to submit this assessment?' not in fresh['text']:continue
                    await click_confirmation_submit(session,visible[0])
                    confirmation_sent_at=time.monotonic()
                    session.log('computer-final-submit.jsonl',{'number':'16','confirmed':True,
                        'evidence':'exact_visible_confirmation_dialog','time':time.time()})
                elif time.monotonic()-confirmation_sent_at>10:
                    raise ValueError('final simulation confirmation did not close')
                await asyncio.sleep(.2)
                continue
            if 'out of 16' not in state['text']:
                terminal=final_text_status(state['text'])
                if terminal=='blocked' or (terminal!='clean' and 'Your test is now complete. Thank you!' in state['text']):
                    terminal_since=None
                    raise ValueError('final simulation screen has an unresolved overlay')
                if terminal=='clean':
                    if terminal_since is None:terminal_since=time.monotonic()
                    if time.monotonic()-terminal_since<1:
                        await asyncio.sleep(.2);continue
                else:
                    terminal_since=None
                    if not any(marker in state['text'] for marker in ('Assessments\n','ASSESSMENT DESCRIPTION')):
                        await asyncio.sleep(.2);continue
                if previous and previous not in seen:
                    outcome=cache.promote(pending,source_test,question=previous,events=events,advanced_at_ms=time.time()*1000);pending=[]
                    session.log('computer-outcomes.jsonl',{'question':previous,**outcome})
                    session.log('computer.jsonl',{'number':previous,'advanced':True,'correctness_verified':outcome['verification']=='verified_success','time':time.time()})
                    seen.add(previous)
                print(json.dumps({'computer_stopped':len(seen)}),flush=True);return
            terminal_since=None
            frames=[f for f in session.page.frames if urlsplit(f.url).path=='/assets/msOfficeSimulation/run.html']
            if len(frames)!=1:await asyncio.sleep(.3);continue
            frame=frames[0]
            number=state['number']
            task=re.search(r'QUESTION\n\d+ out of 16\n(.*?)\n(?:SKIP|SUBMIT)(?:\n|$)',state['text'],re.S)
            if not task:raise ValueError('simulation task unavailable')
            result_screen=(await frame.locator('body').inner_text()).strip()=='result_message'
            provider_success=(previous==number and completed_task_result(number,task[1],pending,events,time.time()*1000))
            if (result_screen or provider_success) and number in active_frames and pending:
                # Every provider result advances through a delayed 2 s callback.
                # The final footer SUBMIT bypasses saving the response, so all
                # questions wait for the provider's next screen or confirmation.
                if number not in result_wait_started:
                    result_wait_started[number]=time.monotonic()
                    session.log('computer-result-wait.jsonl',{'number':number,
                        'reason':'awaiting_provider_response_callback','time':time.time(),
                        'provider_task_success_observed':bool(provider_success)})
                if time.monotonic()-result_wait_started[number]>15:
                    raise ValueError('provider response transition did not arrive')
                await asyncio.sleep(.2)
                continue
            if not result_screen:
                active_frames.add(number);result_wait_started.pop(number,None)
            if previous and number!=previous:
                outcome=cache.promote(pending,source_test,question=previous,events=events,advanced_at_ms=time.time()*1000);pending=[]
                session.log('computer-outcomes.jsonl',{'question':previous,**outcome})
                session.log('computer.jsonl',{'number':previous,'advanced':True,'correctness_verified':outcome['verification']=='verified_success','time':time.time()})
                seen.add(previous)
            previous=number
            await asyncio.sleep(.25)
            nodes=await frame.evaluate(NODES_SCRIPT)
            shot=session.output/f'computer-{number}-step-{step}.png'
            data=await session.page.locator('#frame').screenshot(path=str(shot))
            content_hash=hashlib.sha256((task[1]+json.dumps(nodes,sort_keys=True)).encode()).hexdigest()
            history=[]
            log=session.output/'computer-actions.jsonl'
            if log.exists():history=[x for x in (json.loads(l) for l in log.read_text().splitlines()) if str(x.get('question'))==number][-8:]
            stable=await session.page.evaluate(STATE_SCRIPT)
            if stable.get('number')!=number or task[1] not in stable['text']:continue
            key,canonical=identity(task[1],nodes,hashlib.sha256(data).hexdigest(),frame.url)
            cache.observe(key,canonical,source_test,number)
            session.log('computer-observations.jsonl',{'question':number,'key':key,'state':json.loads(canonical),'screenshot':shot.name})
            cached=cache.lookup(key,canonical)
            source='previous_exact' if cached else 'model'
            original_test=cached[1] if cached else source_test
            if cached:
                result={'question_id':number,'content_hash':content_hash,'status':'answer','confidence':cached[2],**cached[0]}
            else:
                if getattr(session,'replay_only',False):
                    reason='unverified_scene_in_replay_mode' if cache.lookup(key,canonical,require_verified=False) else 'unknown_scene_in_replay_mode'
                    raise ValueError(reason)
                if client is None:
                    executable=shutil.which('codex')
                    if not executable:raise ValueError('Codex unavailable for unknown simulation scene')
                    client=CodexCLIClient(Path(executable),session.project_root/'configs/ui_action_schema.json',isolated,reasoning_effort='low');client.ui_actions=True
                result=await client.complete({'question_id':number,'content_hash':content_hash,'task':task[1],'nodes':nodes,'recent_actions':history,
                    '_image_attachments':[{'location':str(shot.resolve()),'sha256':hashlib.sha256(data).hexdigest()}]},timeout=35)
            fresh=await session.page.evaluate(STATE_SCRIPT)
            if session.timeout_seen or 'Assessment Time out' in fresh['text']:return
            if fresh.get('number')!=number or task[1] not in fresh['text']:continue
            if result['question_id']!=number or result['content_hash']!=content_hash or result['status']!='answer' or (not cached and result['confidence']<.85):
                retries=getattr(session,'computer_retries',{})
                if retries.get(number,0)>=1:raise ValueError('uncertain simulation action after fresh snapshot')
                retries[number]=1;session.computer_retries=retries
                session.log('computer-retries.jsonl',{'question':number,'reason':'uncertain_action_fresh_snapshot'})
                continue
            # The model can take seconds. Revalidate the entire visual scene,
            # including values and hit-test visibility, immediately before use.
            current_nodes=await frame.evaluate(NODES_SCRIPT)
            current_data=await session.page.locator('#frame').screenshot()
            fresh_key,_=identity(task[1],current_nodes,hashlib.sha256(current_data).hexdigest(),frame.url)
            if fresh_key!=key:
                session.log('computer-retries.jsonl',{'question':number,'reason':'scene_changed_before_action','source':source})
                continue
            action_events=await capture_message_evidence(session)
            current_state=await session.page.evaluate(STATE_SCRIPT)
            if session.timeout_seen or 'Assessment Time out' in current_state['text']:return
            if current_state.get('number')!=number or task[1] not in current_state['text']:continue
            if cached and cache.lookup(key,canonical)!=cached:continue
            if actions_applied>=100:raise ValueError('simulation action limit reached')
            metadata={'action_started_ms':time.time()*1000,'event_sequence_before_action':max((e.get('sequence',0) for e in action_events),default=0),'key':key,'correctness_verified':bool(cached),'source':source,'original_test':original_test,'screenshot':shot.name}
            known={n['id'] for n in nodes}
            if result['action'].endswith('_point'):
                box=await session.page.locator('#frame').bounding_box()
                coordinates=[(result['x'],result['y'])]
                if result['action']=='drag_point':coordinates.append((result['target_x'],result['target_y']))
                if any(type(x) not in (int,float) or type(y) not in (int,float) or not 0<x<box['width'] or not 0<y<box['height'] for x,y in coordinates):raise ValueError('simulation coordinates outside frame')
                x,y=coordinates[0];x+=box['x'];y+=box['y']
                if result['action']=='drag_point':
                    tx,ty=coordinates[1]
                    await session.page.mouse.move(x,y);await session.page.mouse.down();await session.page.mouse.move(box['x']+tx,box['y']+ty,steps=20);await session.page.mouse.up()
                elif result['action']=='double_click_point':await session.page.mouse.dblclick(x,y)
                else:await session.page.mouse.click(x,y,button='right' if result['action']=='right_click_point' else 'left')
                actions_applied+=1
                metadata['action_finished_ms']=time.time()*1000
                pending.append((key,canonical,{**result,**metadata}))
                if cached:cache.used(key,source_test,number,original_test)
                session.log('computer-actions.jsonl',{'question':number,**result,**metadata})
                print(json.dumps({'computer_step':number,'action':result['action'],'confidence':result['confidence']}),flush=True)
                await settle_after_action(session);continue
            if result['id'] not in known:raise ValueError('unknown simulation control')
            target=frame.locator('[id='+json.dumps(result['id'])+']')
            if await target.count()!=1 or not await target.is_visible():continue
            action=result['action'];value=result['value']
            if action=='type':
                if not isinstance(value,str) or len(value)>1000:raise ValueError('invalid simulation text')
                if await target.get_attribute('type') in ('hidden','password'):raise ValueError('unsupported simulation field')
                await target.fill('')
                await target.press_sequentially(value,delay=20,timeout=5000)
            elif action=='click':await target.click(timeout=5000)
            elif action=='double_click':await target.dblclick(timeout=5000)
            elif action=='right_click':await target.click(button='right',timeout=5000)
            elif action=='key':
                if value not in ('Enter','Tab','Delete','Backspace','ArrowDown','ArrowUp','Escape','Control+f','Control+x','Control+v','Control+a'):raise ValueError('unsupported simulation key')
                await target.press(value,timeout=5000)
            elif action=='drag':
                if result['target_id'] not in known:raise ValueError('unknown drag destination')
                await target.drag_to(frame.locator('[id='+json.dumps(result['target_id'])+']'),timeout=5000)
            else:raise ValueError('unsupported simulation action')
            actions_applied+=1
            metadata['action_finished_ms']=time.time()*1000
            pending.append((key,canonical,{**result,**metadata}))
            if cached:cache.used(key,source_test,number,original_test)
            session.log('computer-actions.jsonl',{'question':number,**result,**metadata})
            print(json.dumps({'computer_step':number,'action':action,'confidence':result['confidence']}),flush=True)
            await settle_after_action(session)
        except Exception as error:
            if any(x in str(error) for x in ('Frame was detached','Execution context was destroyed','Cannot find context')):
                await asyncio.sleep(.3);continue
            raise
    raise ValueError('simulation observation limit reached')
