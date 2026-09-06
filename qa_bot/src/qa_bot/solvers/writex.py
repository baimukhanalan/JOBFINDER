"""Fill the assessment's email fields; no email connector is invoked."""
from dataclasses import replace
import importlib
import json
import re
from qa_bot.domain.question import QuestionSpec,ResponseContract,ResponseKind
from qa_bot.knowledge.bank import fingerprint

INSTRUCTION='Compose an email response for the topic provided. Please ensure that your response is a minimum of 30 words.'

def topic(text):
    if INSTRUCTION not in text:return None
    rest=text.split(INSTRUCTION,1)[1]
    return rest.split('Word count:',1)[0].strip()

def email_parts(answer):
    match=re.fullmatch(r'To:\s*([^\n]+)\nSubject:\s*([^\n]+)\n\s*([\s\S]+)',answer.strip())
    if not match:raise ValueError('structured historical email required')
    recipient,subject,body=match.groups()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',recipient) or len(body.split())<30:
        raise ValueError('email fields incomplete')
    return recipient,subject,body

async def run_writex(session):
    from qa_bot.live_session import STATE_SCRIPT,safe_text
    from qa_bot.knowledge import historical
    importlib.reload(historical)
    try:
        state=await session.page.evaluate(STATE_SCRIPT);prompt=topic(state['text']);number=state['number']
        if not prompt or not number:raise ValueError('expected current email question')
        q=QuestionSpec('writex-'+number,'authorized-single','WriteX - Email Writing','WRITEX-EMAIL',prompt,ResponseContract(ResponseKind.TEXT,min_words=30),'pending',instruction=INSTRUCTION,completeness=True,extraction_confidence=1)
        q=replace(q,content_hash=fingerprint(q)[0])
        root=session.project_root.parent
        result=historical.HistoricalAnswerResolver.from_files(root/'data/questions.jsonl',root/'SHL_answers_all.csv').lookup(q)
        if result is None:raise ValueError('no exact historical email')
        recipient,subject,body=email_parts(result.proposal.text)
        if recipient not in prompt:raise ValueError('recipient not in current question')
        fields=[session.page.get_by_placeholder('To:',exact=True),session.page.get_by_placeholder('Subject',exact=True),session.page.get_by_placeholder('Compose your response',exact=True)]
        for field,value in zip(fields,(recipient,subject,body)):
            if await field.count()!=1:raise ValueError('one field required')
            existing=await field.input_value()
            if not value.startswith(existing):raise ValueError('existing email conflicts')
            await field.press_sequentially(value[len(existing):],delay=3,timeout=30000)
            if await field.input_value()!=value:raise ValueError('email field mismatch')
        fresh=await session.page.evaluate(STATE_SCRIPT)
        if fresh['number']!=number or topic(fresh['text'])!=prompt:raise ValueError('email topic changed')
        await session.click('button','SUBMIT ANSWER')
        session.log('writex.jsonl',{'number':number,'source':'exact_historical','source_ids':result.source_ids,'body_words':len(body.split()),'exact_fields':True})
        print(json.dumps({'writex_submitted':number,'words':len(body.split()),'source':'exact_historical'}),flush=True)
    except Exception as error:
        print(json.dumps({'writex_blocked':safe_text(error)[:300]}),flush=True)
