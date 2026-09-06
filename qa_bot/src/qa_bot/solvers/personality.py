"""Replay exact historical QA choices through the observed five-point scale."""
from dataclasses import replace
import asyncio
import json
from qa_bot.domain.question import QuestionSpec, OptionSpec, ResponseContract, ResponseKind
from qa_bot.knowledge.bank import fingerprint
from qa_bot.knowledge.historical import HistoricalAnswerResolver

SCALE_SCRIPT = '''() => ({
 text:[...document.querySelectorAll('.question')].filter(n=>n.getClientRects().length).map(n=>n.innerText.trim()),
 options:[...document.querySelectorAll('label.radio-outer')].filter(n=>n.getClientRects().length).map(n=>({id:n.id,label:(n.title||n.innerText).trim(),input:n.querySelector('input')?.id,checked:n.querySelector('input')?.checked}))})'''
INSTRUCTION='Select an option with which you agree the most.'

async def run_personality(session,state):
    from qa_bot.live_session import STATE_SCRIPT, safe_text
    number=state['number']
    try:
        current=await session.page.evaluate(STATE_SCRIPT)
        if not number or current['number']!=number:return False
        dom=await session.page.evaluate(SCALE_SCRIPT)
        if len(dom['text'])!=1 or len(dom['options'])!=5:raise ValueError('one visible five-point question required')
        fresh=await session.page.evaluate(STATE_SCRIPT)
        if fresh['number']!=number or await session.page.evaluate(SCALE_SCRIPT)!=dom:return False
        # Consume only a freshly verified question. A blocked exact lookup must
        # not be restarted on every watch tick; no answer is invented for it.
        session.personality_tasks.add(number)
        labels=[o['label'] for o in dom['options']]
        q=QuestionSpec('personality-'+number,'authorized-single','Personality','PERSONALITY-SCALE',dom['text'][0],ResponseContract(ResponseKind.SINGLE_CHOICE,1,1),'pending',instruction=INSTRUCTION,options=tuple(OptionSpec('option-'+str(i),i,label) for i,label in enumerate(labels,1)),completeness=True,extraction_confidence=1)
        q=replace(q,content_hash=fingerprint(q)[0])
        if not hasattr(session,'historical'):
            root=session.project_root.parent
            session.historical=HistoricalAnswerResolver.from_files(root/'data/questions.jsonl',root/'SHL_answers_all.csv')
        result=session.historical.lookup(q)
        if result is None:raise ValueError('no exact historical QA answer')
        selected=next(i for i,o in enumerate(q.options) if o.id==result.proposal.selections[0].option_id)
        fresh=await session.page.evaluate(STATE_SCRIPT)
        if fresh['number']!=number or await session.page.evaluate(SCALE_SCRIPT)!=dom:
            session.personality_tasks.discard(number)
            return False
        # Keep the key after dispatch failures: retrying NEXT could submit twice.
        target=dom['options'][selected]
        await session.page.locator('[id='+json.dumps(target['id'])+']').click()
        if not await session.page.locator('[id='+json.dumps(target['input'])+']').is_checked():raise ValueError('scale selection not checked')
        fresh=await session.page.evaluate(STATE_SCRIPT)
        if fresh['number']!=number:raise ValueError('question changed before submit')
        await session.click('button','NEXT')
        for _ in range(100):
            fresh=await session.page.evaluate(STATE_SCRIPT)
            if fresh.get('number')!=number or INSTRUCTION not in fresh['text']:break
            await asyncio.sleep(.05)
        else:raise ValueError('personality NEXT did not advance')
        session.log('personality.jsonl',{'number':number,'question':q.question_text,'answer':labels[selected],'source_ids':result.source_ids,'official_answer_key':False,'source':'exact_historical','transition_observed':True})
        print(json.dumps({'personality_submitted':number,'source':'exact_historical'}),flush=True)
        return True
    except Exception as error:
        session.log('personality-errors.jsonl',{'number':number,'error':safe_text(error)[:300]})
        print(json.dumps({'personality_blocked':number,'reason':safe_text(error)[:300]}),flush=True)
        return False
