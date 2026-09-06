"""Visible controls in the observed Windows simulation frame."""
import json
import asyncio
from urllib.parse import urlsplit

async def run(session,module):
    frames=[f for f in session.page.frames if urlsplit(f.url).path=='/assets/msOfficeSimulation/run.html']
    if len(frames)!=1:raise ValueError('one simulation frame required')
    frame=frames[0]
    if module=='computer_inspect':
        await session.page.evaluate("""()=>{if(window.__qaSimulationMessages)return;window.__qaSimulationMessages=[];addEventListener('message',e=>{if(e.source!==document.querySelector('#frame')?.contentWindow)return;let d=e.data;if(typeof d==='string'){try{d=JSON.parse(d)}catch{return}}if(!d||typeof d!=='object')return;const clean={keys:Object.keys(d)};for(const [k,v] of Object.entries(d)){if(typeof v==='number'||typeof v==='boolean')clean[k]=v;else if(/status|result|success|correct|score/i.test(k)&&typeof v==='string'&&/^[a-zA-Z0-9 _.,:-]{1,100}$/.test(v))clean[k]=v}window.__qaSimulationMessages.push(clean)})}""")
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


async def automate(session,frame):
    import hashlib
    import re
    import shutil
    import time
    from pathlib import Path
    from qa_bot.live_session import STATE_SCRIPT,safe_text
    from qa_bot.adapters.llm.codex_cli import CodexCLIClient
    isolated=session.output/'isolated';isolated.mkdir(exist_ok=True)
    client=CodexCLIClient(Path(shutil.which('codex')),session.project_root/'configs/ui_action_schema.json',isolated,reasoning_effort='low');client.ui_actions=True
    seen=set();previous=None
    for step in range(100):
        try:
            state=await session.page.evaluate(STATE_SCRIPT)
            if session.timeout_seen or 'Assessment Time out' in state['text']:return
            if 'out of 16' not in state['text']:
                if not any(marker in state['text'] for marker in ('Assessments\n','ASSESSMENT DESCRIPTION','Your test is now complete')):
                    await asyncio.sleep(.3);continue
                if previous and previous not in seen:
                    session.log('computer.jsonl',{'number':previous,'advanced':True,'time':time.time()})
                    seen.add(previous)
                print(json.dumps({'computer_stopped':len(seen)}),flush=True);return
            frames=[f for f in session.page.frames if urlsplit(f.url).path=='/assets/msOfficeSimulation/run.html']
            if len(frames)!=1:await asyncio.sleep(.3);continue
            frame=frames[0]
            number=state['number']
            if previous and number!=previous:
                session.log('computer.jsonl',{'number':previous,'advanced':True,'time':time.time()})
                seen.add(previous)
            previous=number
            task=re.search(r'QUESTION\n\d+ out of 16\n(.*?)\nSKIP',state['text'],re.S)
            if not task:raise ValueError('simulation task unavailable')
            await asyncio.sleep(.25)
            nodes=await frame.evaluate("""()=>[...document.querySelectorAll('input,textarea,button,[role=button]')].filter(n=>{if(!n.id||!n.getClientRects().length||getComputedStyle(n).visibility==='hidden')return false;const r=n.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return hit===n||n.contains(hit)}).map(n=>{const r=n.getBoundingClientRect();return {id:n.id,tag:n.tagName,label:n.getAttribute('aria-label'),value:('value' in n)?n.value:null,rect:{x:r.x,y:r.y,width:r.width,height:r.height}}})""")
            shot=session.output/f'computer-{number}-step-{step}.png'
            data=await session.page.locator('#frame').screenshot(path=str(shot))
            content_hash=hashlib.sha256((task[1]+json.dumps(nodes,sort_keys=True)).encode()).hexdigest()
            history=[]
            log=session.output/'computer-actions.jsonl'
            if log.exists():history=[x for x in (json.loads(l) for l in log.read_text().splitlines()) if str(x.get('question'))==number][-8:]
            stable=await session.page.evaluate(STATE_SCRIPT)
            if stable.get('number')!=number or task[1] not in stable['text']:continue
            result=await client.complete({'question_id':number,'content_hash':content_hash,'task':task[1],'nodes':nodes,'recent_actions':history,
                '_image_attachments':[{'location':str(shot.resolve()),'sha256':hashlib.sha256(data).hexdigest()}]},timeout=35)
            fresh=await session.page.evaluate(STATE_SCRIPT)
            if session.timeout_seen or 'Assessment Time out' in fresh['text']:return
            if fresh.get('number')!=number or task[1] not in fresh['text']:continue
            if result['question_id']!=number or result['content_hash']!=content_hash or result['status']!='answer' or result['confidence']<.85:
                retries=getattr(session,'computer_retries',{})
                if retries.get(number,0)>=1:raise ValueError('uncertain simulation action after fresh snapshot')
                retries[number]=1;session.computer_retries=retries
                session.log('computer-retries.jsonl',{'question':number,'reason':'uncertain_action_fresh_snapshot'})
                continue
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
                session.log('computer-actions.jsonl',{'question':number,**result})
                print(json.dumps({'computer_step':number,'action':result['action'],'confidence':result['confidence']}),flush=True)
                await asyncio.sleep(1.5);continue
            if result['id'] not in known:raise ValueError('unknown simulation control')
            target=frame.locator('[id='+json.dumps(result['id'])+']')
            if await target.count()!=1 or not await target.is_visible():continue
            action=result['action'];value=result['value']
            if action=='type':
                if not isinstance(value,str) or len(value)>1000:raise ValueError('invalid simulation text')
                if await target.get_attribute('type') in ('hidden','password'):raise ValueError('unsupported simulation field')
                await target.fill('')
                await target.press_sequentially(value,delay=40,timeout=5000)
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
            session.log('computer-actions.jsonl',{'question':number,'id':result['id'],'action':action,'value':value,'target':result['target_id'],'confidence':result['confidence']})
            print(json.dumps({'computer_step':number,'action':action,'confidence':result['confidence']}),flush=True)
            await asyncio.sleep(1.5)
        except Exception as error:
            if any(x in str(error) for x in ('Frame was detached','Execution context was destroyed','Cannot find context')):
                await asyncio.sleep(.3);continue
            raise
    raise ValueError('simulation step limit reached')
