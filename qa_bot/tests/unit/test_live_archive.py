import tempfile
import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from qa_bot.domain.answer import AnswerProposal,Selection
from qa_bot.domain.question import AssetRef,OptionSpec,QuestionSpec,ResponseContract,ResponseKind
from qa_bot.knowledge.bank import QuestionBank,fingerprint
from qa_bot.knowledge.live_archive import LiveAnswerArchive
from qa_bot.solvers.engine import AnswerEngine


def question(identity='one', screenshot='a', figure='b'):
    q=QuestionSpec(identity,'test','Basic Analytical Ability','ANALYTICAL-MCQ','Which diagram matches?',ResponseContract(ResponseKind.SINGLE_CHOICE,1,1),'pending',
        options=(OptionSpec('a',1,'First'),OptionSpec('b',2,'Second')),
        assets=(AssetRef('question-image','image/png','timer.png',screenshot*64),AssetRef('figure-1','image/png','figure.png',figure*64)),completeness=True,extraction_confidence=1)
    return replace(q,content_hash=fingerprint(q)[0])


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_ten_concurrent_runs_share_one_reasoning_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'bank.sqlite3';q=question();client=AsyncMock()
            async def solve(payload,**kwargs):
                await asyncio.sleep(.1)
                return {'question_id':payload['question_id'],'content_hash':payload['content_hash'],'status':'answer','kind':'single_choice','confidence':.99,'selections':[{'option_id':'b','role':None}],'text':None,'calculation':None}
            client.complete.side_effect=solve
            async def worker(i):
                with LiveAnswerArchive(path,source_test='run-'+str(i),ignored_observation_assets=('question-image',)) as archive,QuestionBank(':memory:') as bank:
                    return await AnswerEngine(bank,client,archive=archive).propose(q,authorized_qa=True)
            results=await asyncio.gather(*(worker(i) for i in range(10)))
            self.assertEqual(client.complete.await_count,1)
            self.assertEqual(sum(r.source=='previous_exact' for r in results),9)
            self.assertTrue(all(r.proposal.selections[0].option_id=='b' for r in results))

    async def test_replay_only_unknown_question_cannot_call_model(self):
        client=AsyncMock();q=question()
        with LiveAnswerArchive(':memory:',source_test='first') as archive,QuestionBank(':memory:') as bank:
            result=await AnswerEngine(bank,client,archive=archive).propose(q,authorized_qa=True,allow_model=False)
            self.assertEqual(result.reason,'unknown_question_in_replay_mode')
            self.assertEqual(archive.db.execute('SELECT count(*) FROM observations').fetchone()[0],1)
        client.complete.assert_not_called()

    async def test_repeat_across_runs_and_timer_change_never_calls_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'bank.sqlite3';q=question()
            a=AnswerProposal(q.question_id,q.content_hash,ResponseKind.SINGLE_CHOICE,.98,selections=(Selection('b'),))
            with LiveAnswerArchive(path,source_test='first',ignored_observation_assets=('question-image',)) as archive:
                archive.observe(q);archive.save(q,a,'candidate')
            changed=question('new-question',screenshot='c')
            client=AsyncMock();client.complete.side_effect=AssertionError('model must not run')
            with LiveAnswerArchive(path,source_test='second',ignored_observation_assets=('question-image',)) as archive,QuestionBank(':memory:') as bank:
                result=await AnswerEngine(bank,client,archive=archive).propose(changed,authorized_qa=True)
                self.assertEqual(result.source,'previous_exact')
                self.assertEqual(result.proposal.question_id,'new-question')
                self.assertEqual(result.proposal.content_hash,changed.content_hash)
                self.assertEqual(archive.db.execute('SELECT count(*) FROM observations').fetchone()[0],2)
                self.assertIsNone(archive.lookup(question(figure='d')))
                self.assertIsNone(archive.lookup(replace(changed,question_text='Which diagram does NOT match?')))
            client.complete.assert_not_called()

    async def test_conflicting_answers_are_not_reused(self):
        q=question();a=AnswerProposal(q.question_id,q.content_hash,ResponseKind.SINGLE_CHOICE,.98,selections=(Selection('a'),))
        with LiveAnswerArchive(':memory:',source_test='one') as archive:
            archive.save(q,a,'candidate')
            archive.save(q,replace(a,selections=(Selection('b'),)),'candidate')
            self.assertIsNone(archive.lookup(q))

    async def test_qa_archive_not_used_without_qa_authorization(self):
        q=question();archive=AsyncMock();client=AsyncMock()
        with QuestionBank(':memory:') as bank:
            result=await AnswerEngine(bank,client,archive=archive).propose(q)
        self.assertIsNone(result.proposal)
        archive.observe.assert_not_called();archive.lookup.assert_not_called();client.complete.assert_not_called()
