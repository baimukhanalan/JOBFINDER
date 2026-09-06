import csv
import json
from pathlib import Path
import tempfile
import unittest
from playwright.async_api import async_playwright
from qa_bot.live_session import Session
from qa_bot.solvers.writex import run_writex,INSTRUCTION as EMAIL
from qa_bot.solvers.sales import run_sales,INSTRUCTION as SALES


def corpus(root,section,instruction,text,options,answer):
    (root/'data').mkdir();(root/'qa_bot').mkdir()
    (root/'data/questions.jsonl').write_text(json.dumps({'id':'fixture','section':section,'prompt':instruction,'text':text,'options':options})+'\n')
    with (root/'SHL_answers_all.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=['id','recommended_answer','confidence','official_answer_key']);writer.writeheader();writer.writerow({'id':'fixture','recommended_answer':answer,'confidence':'fixture','official_answer_key':'false'})

class WrittenFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_best_worst_bind_different_rows_and_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            corpus(root,'Sales Competency Test',SALES,'A synthetic situation.',['Helpful','Harmful','Neutral'],'BEST: Helpful\nWORST: Harmful')
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(headless=True)
                try:
                    page=await browser.new_page()
                    await page.set_content(f'<div>{SALES}</div><div class="sjtNormal"><div class="questionContent">A synthetic situation.</div></div><table class="renderSjt"></table><a class="currentQue">1</a><button>SUBMIT ANSWER</button>')
                    await page.evaluate('''() => {document.querySelector('table').innerHTML=['Helpful','Harmful','Neutral'].map((x,i)=>`<tr><td><label class="optionLabel">${x}</label></td><td><input type="radio" name="best" id="b${i}"><label for="b${i}" aria-label="Best">Best</label></td><td><input type="radio" name="worst" id="w${i}"><label for="w${i}" aria-label="Worst">Worst</label></td></tr>`).join('');document.querySelector('button').onclick=()=>{window.actual=[...document.querySelectorAll('input:checked')].map(n=>n.id);document.body.innerHTML='Assessment complete';};}''')
                    session=Session(page,root/'evidence');session.project_root=root/'qa_bot';session.cdp=await page.context.new_cdp_session(page)
                    await run_sales(session)
                    self.assertEqual(await page.evaluate('actual'),['b0','w1'])
                finally:await browser.close()

    async def test_email_fields_keep_paragraphs_and_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);prompt='Write a synthetic email to test@example.com.'
            body='Dear Tester,\n\n'+' '.join(['word']*35)+'\n\nRegards,\nQA'
            corpus(root,'WriteX - Email Writing',EMAIL,prompt,[],f'To: test@example.com\nSubject: QA example\n\n{body}')
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(headless=True)
                try:
                    page=await browser.new_page()
                    await page.set_content(f'<div>{EMAIL}</div><div>{prompt}</div><div>Word count: 0</div><input placeholder="To:"><input placeholder="Subject"><textarea placeholder="Compose your response"></textarea><a class="currentQue">1</a><button>SUBMIT ANSWER</button>')
                    await page.evaluate("document.querySelector('button').onclick=()=>window.actual=[...document.querySelectorAll('input,textarea')].map(n=>n.value)")
                    session=Session(page,root/'evidence');session.project_root=root/'qa_bot';session.cdp=await page.context.new_cdp_session(page)
                    await run_writex(session)
                    self.assertEqual(await page.evaluate('actual'),['test@example.com','QA example',body])
                finally:await browser.close()
