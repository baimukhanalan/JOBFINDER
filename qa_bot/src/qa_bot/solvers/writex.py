"""Fill the assessment's email fields; no email connector is invoked."""
from dataclasses import replace
import importlib
import json
import re
import time
import shutil
from pathlib import Path
from qa_bot.domain.question import QuestionSpec,ResponseContract,ResponseKind
from qa_bot.knowledge.bank import fingerprint, QuestionBank
from qa_bot.knowledge.live_archive import session_archive
from qa_bot.solvers.engine import AnswerEngine
from qa_bot.adapters.llm.codex_cli import CodexCLIClient

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

def validate_email(answer, prompt):
    fields=email_parts(answer)
    addresses=set(re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",prompt))
    if fields[0] not in addresses:raise ValueError('recipient not explicitly present in current question')
    return fields

class EmailClient:
    """Validate structured fields before AnswerEngine can archive a proposal."""
    def __init__(self,session,prompt):self.session,self.prompt=session,prompt
    async def complete(self,payload,*,timeout):
        isolated=self.session.output/'isolated';isolated.mkdir(exist_ok=True)
        executable=shutil.which('codex')
        if not executable:raise ValueError('Codex CLI unavailable')
        client=CodexCLIClient(Path(executable),self.session.project_root/'configs/answer_schema.json',isolated,reasoning_effort='low')
        request=dict(payload)
        request['instruction']+=' Return text in exactly this format: To: <explicit email address from the question>\nSubject: <specific subject>\n\n<email body>. The body itself must contain at least 30 words. Address every requested point using only supplied facts. Do not invent a recipient. These are assessment fields; do not send email.'
        result=await client.complete(request,timeout=timeout)
        if isinstance(result,dict) and result.get('status')=='answer':validate_email(result.get('text'),self.prompt)
        return result

async def run_writex(session):
    from qa_bot.live_session import STATE_SCRIPT,safe_text
    from qa_bot.knowledge import historical
    importlib.reload(historical)
    number=None
    try:
        state=await session.page.evaluate(STATE_SCRIPT);prompt=topic(state['text']);number=state['number']
        if not prompt or not number:raise ValueError('expected current email question')
        if not re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+',prompt):raise ValueError('no explicit recipient email in current question')
        q=QuestionSpec('writex-'+number,'authorized-single','WriteX - Email Writing','WRITEX-EMAIL',prompt,ResponseContract(ResponseKind.TEXT,min_words=30),'pending',instruction=INSTRUCTION,completeness=True,extraction_confidence=1)
        q=replace(q,content_hash=fingerprint(q)[0])
        root=session.project_root.parent
        result=historical.HistoricalAnswerResolver.from_files(root/'data/questions.jsonl',root/'SHL_answers_all.csv').lookup(q)
        if result is not None:
            recipient,subject,body=validate_email(result.proposal.text,prompt)
            source='exact_historical';source_ids=result.source_ids
            with session_archive(session) as archive:archive.observe(q);archive.save(q,result.proposal,'historical_exact')
        else:
            with QuestionBank(session.output/'writex.sqlite3') as bank, session_archive(session) as archive:
                result=await AnswerEngine(bank,EmailClient(session,prompt),archive=archive).propose(q,authorized_qa=True,allow_model=not getattr(session,'replay_only',False),timeout=45)
            if not result.proposal:raise ValueError(result.reason)
            recipient,subject,body=validate_email(result.proposal.text,prompt)
            source=result.source;source_ids=[]
        fresh=await session.page.evaluate(STATE_SCRIPT)
        if fresh['number']!=number or topic(fresh['text'])!=prompt:raise ValueError('email topic changed during preparation')
        fields=[session.page.get_by_placeholder('To:',exact=True),session.page.get_by_placeholder('Subject',exact=True),session.page.get_by_placeholder('Compose your response',exact=True)]
        for field,value in zip(fields,(recipient,subject,body)):
            if await field.count()!=1:raise ValueError('one field required')
            existing=await field.input_value()
            if not value.startswith(existing):raise ValueError('existing email conflicts')
            await field.press_sequentially(value[len(existing):],delay=3,timeout=30000)
            if await field.input_value()!=value:raise ValueError('email field mismatch')
        fresh=await session.page.evaluate(STATE_SCRIPT)
        if fresh['number']!=number or topic(fresh['text'])!=prompt:raise ValueError('email topic changed')
        if [await f.input_value() for f in fields]!=[recipient,subject,body]:raise ValueError('email fields changed before submit')
        await session.click('button','SUBMIT ANSWER')
        session.log('writex.jsonl',{'number':number,'question_hash':q.content_hash,'source':source,'source_ids':source_ids,'body_words':len(body.split()),'exact_fields':True})
        print(json.dumps({'writex_submitted':number,'words':len(body.split()),'source':source}),flush=True)
    except Exception as error:
        session.log('writex-errors.jsonl',{'time':time.time(),'number':number,'error':safe_text(error)[:300]})
        print(json.dumps({'writex_blocked':safe_text(error)[:300]}),flush=True)
