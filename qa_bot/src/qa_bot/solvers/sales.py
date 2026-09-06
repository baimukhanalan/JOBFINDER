"""Bind best/worst answers to the current situation's labeled radio groups."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shutil
from qa_bot.domain.question import QuestionSpec,OptionSpec,ResponseContract,ResponseKind
from qa_bot.knowledge.bank import QuestionBank,fingerprint
from qa_bot.knowledge.live_archive import session_archive
from qa_bot.knowledge.historical import HistoricalAnswerResolver
from qa_bot.adapters.llm.codex_cli import CodexCLIClient
from qa_bot.solvers.engine import AnswerEngine

INSTRUCTION="Choose the 'best' and the 'worst' action for the given situation."
SALES_SCRIPT='''() => ({questions:[...document.querySelectorAll('.sjtNormal .questionContent')].filter(n=>n.getClientRects().length).map(n=>n.innerText.trim()),
 rows:[...document.querySelectorAll('table.renderSjt tr')].filter(n=>n.getClientRects().length).map(n=>({text:n.querySelector('.optionLabel')?.innerText.trim(),roles:[...n.querySelectorAll('label[aria-label]')].map(l=>({role:l.getAttribute('aria-label').toLowerCase(),input:l.htmlFor}))}))})'''

async def run_sales(session):
    from qa_bot.live_session import STATE_SCRIPT,safe_text
    seen=set()
    try:
        root=session.project_root.parent
        historical=HistoricalAnswerResolver.from_files(root/'data/questions.jsonl',root/'SHL_answers_all.csv')
        for _ in range(20):
            state=await session.page.evaluate(STATE_SCRIPT);number=state.get('number')
            if INSTRUCTION not in state['text']:break
            if not number or number in seen:raise ValueError('new sales question required')
            dom=await session.page.evaluate(SALES_SCRIPT)
            if len(dom['questions'])!=1 or not 3<=len(dom['rows'])<=6:raise ValueError('ambiguous sales situation')
            if any({x['role'] for x in row['roles']}!={'best','worst'} or len(row['roles'])!=2 for row in dom['rows']):raise ValueError('best/worst controls required')
            q=QuestionSpec('sales-'+number,'authorized-single','Sales Competency Test','SALES-BEST-WORST',dom['questions'][0],ResponseContract(ResponseKind.MULTI_CHOICE,2,2,('best','worst')),'pending',instruction=INSTRUCTION,
                options=tuple(OptionSpec('option-'+str(i),i,row['text']) for i,row in enumerate(dom['rows'],1)),completeness=True,extraction_confidence=1)
            q=replace(q,content_hash=fingerprint(q)[0])
            isolated=session.output/'isolated';isolated.mkdir(exist_ok=True)
            client=CodexCLIClient(Path(shutil.which('codex')),session.project_root/'configs/answer_schema.json',isolated,reasoning_effort='low')
            with QuestionBank(session.output/'sales.sqlite3') as bank, session_archive(session) as archive:
                result=await AnswerEngine(bank,client,historical=historical,archive=archive).propose(q,authorized_qa=True,allow_model=not getattr(session,'replay_only',False),timeout=45)
            if not result.proposal:raise ValueError(result.reason)
            if len({s.option_id for s in result.proposal.selections})!=2:raise ValueError('best and worst must differ')
            fresh=await session.page.evaluate(STATE_SCRIPT)
            if fresh['number']!=number or await session.page.evaluate(SALES_SCRIPT)!=dom:raise ValueError('sales question changed')
            inputs=[]
            for selection in result.proposal.selections:
                row=dom['rows'][next(i for i,o in enumerate(q.options) if o.id==selection.option_id)]
                identity=next(x['input'] for x in row['roles'] if x['role']==selection.role)
                field=session.page.locator('[id='+json.dumps(identity)+']')
                await field.check()
                if not await field.is_checked():raise ValueError('sales role not selected')
                inputs.append(identity)
            if len(await session.page.locator('table.renderSjt input:checked').all())!=2:raise ValueError('two selected controls required')
            if not all(await asyncio.gather(*(session.page.locator('[id='+json.dumps(i)+']').is_checked() for i in inputs))):raise ValueError('role selection changed')
            fresh=await session.page.evaluate(STATE_SCRIPT)
            if fresh['number']!=number or await session.page.evaluate(SALES_SCRIPT)!=dom:raise ValueError('sales question changed before submit')
            await session.click('button','SUBMIT ANSWER')
            session.log('sales.jsonl',{'number':number,'question_hash':q.content_hash,'source':result.source,'confidence':result.proposal.confidence,'roles':[{'role':s.role,'label':next(o.label for o in q.options if o.id==s.option_id)} for s in result.proposal.selections],'correctness_verified':False})
            print(json.dumps({'sales_submitted':number,'source':result.source}),flush=True)
            seen.add(number)
            for retry in range(150):
                await asyncio.sleep(.1)
                fresh=await session.page.evaluate(STATE_SCRIPT)
                if fresh.get('number')!=number:break
            else:raise ValueError('sales submission did not advance')
            await asyncio.sleep(.3)
        print(json.dumps({'sales_stopped':len(seen)}),flush=True)
    except Exception as error:
        session.log('sales-errors.jsonl',{'error':safe_text(error)[:300]})
        print(json.dumps({'sales_blocked':safe_text(error)[:300]}),flush=True)
