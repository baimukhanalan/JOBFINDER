import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from playwright.async_api import async_playwright
from qa_bot.live_session import Session
from qa_bot.solvers.writex import run_writex, INSTRUCTION

BODY='Dear Team,\n\n'+' '.join(['synthetic']*35)+'\n\nRegards,\nQA'
class WriteXLearningTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_structured_email_exact_replay_and_missing_address(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir();(root/'qa_bot').mkdir()
            (root/'data/questions.jsonl').write_text('');(root/'SHL_answers_all.csv').write_text('id,recommended_answer\n')
            calls=[]
            class Client:
                def __init__(self,*a,**kw):pass
                async def complete(self,q,**kw):
                    calls.append(q)
                    if 'STALE' in q['question_text']:
                        await page.locator('.currentQue').evaluate("n=>n.innerText='2'")
                    recipient='invented@example.com' if 'BAD' in q['question_text'] else 'qa@example.com'
                    return dict(question_id=q['question_id'],content_hash=q['content_hash'],status='answer',kind='text',confidence=.99,selections=[],text=f'To: {recipient}\nSubject: Synthetic report\n\n'+BODY,calculation=None)
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(headless=True)
                try:
                    for i,(prompt,replay,expected) in enumerate([
                        ('Write a synthetic report to qa@example.com about the planned meeting.',False,'candidate'),
                        ('Write a synthetic report to qa@example.com about the planned meeting.',True,'previous_exact'),
                        ('Write a synthetic report to your manager about the planned meeting.',False,'blocked'),
                        ('Write a DIFFERENT report to qa@example.com about a new issue.',True,'blocked'),
                        ('Write a BAD synthetic report to qa@example.com.',False,'blocked'),
                        ('Write a STALE synthetic report to qa@example.com.',False,'blocked'),
                    ]):
                        with self.subTest(i=i):
                            page=await browser.new_page()
                            await page.set_content(f'<div>{INSTRUCTION}</div><div>{prompt}</div><div>Word count: 0</div><input placeholder="To:"><input placeholder="Subject"><textarea placeholder="Compose your response"></textarea><a class="currentQue">1</a><button>SUBMIT ANSWER</button>')
                            await page.evaluate("() => {window.submitted=false;document.querySelector('button').onclick=()=>window.submitted=true;}")
                            session=Session(page,root/f'evidence-{i}');session.project_root=root/'qa_bot';session.answer_archive_path=root/'shared.sqlite3';session.replay_only=replay;session.cdp=await page.context.new_cdp_session(page)
                            with patch('qa_bot.solvers.writex.CodexCLIClient',Client),patch('qa_bot.solvers.writex.shutil.which',return_value='/synthetic/codex'):
                                await run_writex(session)
                            self.assertEqual(await page.evaluate('submitted'),expected!='blocked')
                            if expected=='blocked':self.assertTrue((session.output/'writex-errors.jsonl').exists())
                            else:
                                log=json.loads((session.output/'writex.jsonl').read_text());self.assertEqual(log['source'],expected)
                                self.assertEqual(await page.get_by_placeholder('Compose your response',exact=True).input_value(),BODY)
                            await page.close()
                    self.assertEqual(len(calls),3)
                    import sqlite3
                    with sqlite3.connect(root/'shared.sqlite3') as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM solutions WHERE canonical LIKE '%BAD%'").fetchone()[0],0)
                finally:await browser.close()
