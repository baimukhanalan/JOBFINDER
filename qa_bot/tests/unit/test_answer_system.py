import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from qa_bot.domain.answer import AnswerProposal, Selection
from qa_bot.domain.question import ResponseKind
from qa_bot.domain.question import ResponseContract
from qa_bot.execution.validator import validate_answer
from qa_bot.knowledge.bank import QuestionBank, fingerprint
from qa_bot.solvers.calculator import calculate, match_number, convert_unit
from qa_bot.solvers.engine import AnswerEngine
from qa_bot.orchestration.answer_pipeline import AnswerPipeline
from pathlib import Path
from qa_bot.domain.observation import BrowserSnapshot
from qa_bot.perception.extractor import QuestionExtractor


def spec(name="ANA-01"):
    path = Path(__file__).resolve().parents[1] / "fixtures/questions" / (name + ".html")
    return QuestionExtractor().extract(BrowserSnapshot(
        "obs", "http://127.0.0.1:8000/question.html",
        question_html=path.read_text(encoding="utf-8"))).spec


def answer(q):
    return AnswerProposal(q.question_id, q.content_hash, q.response_contract.kind,
                          0.99, selections=(Selection(q.options[0].id),))


class AnswerSystemTests(unittest.IsolatedAsyncioTestCase):
    async def test_best_effort_preserves_low_confidence_without_any_solution_access(self):
        q=spec();bank=MagicMock();archive=MagicMock();historical=MagicMock();client=AsyncMock()
        client.complete.return_value={'question_id':q.question_id,'content_hash':q.content_hash,'status':'answer','kind':'single_choice','confidence':0.25,'selections':[{'option_id':q.options[0].id,'role':None}],'text':None,'calculation':None}
        result=await AnswerEngine(bank,client,archive=archive,historical=historical).propose(q,authorized_qa=True,best_effort=True)
        self.assertEqual(result.source,'best_effort_unverified')
        self.assertEqual(result.proposal.confidence,0.25)
        self.assertEqual(bank.mock_calls,[])
        self.assertEqual(archive.mock_calls,[])
        self.assertEqual(historical.mock_calls,[])
        bank.lookup.return_value=None
        ordinary=await AnswerEngine(bank,client).propose(q,authorized_qa=True)
        self.assertIsNone(ordinary.proposal)
        self.assertIn('low_confidence',ordinary.reason)
        bank.save_candidate.assert_not_called()

    async def test_best_effort_retains_validation_and_missing_data_abstention(self):
        q=spec();bank=MagicMock();client=AsyncMock()
        valid={'question_id':q.question_id,'content_hash':q.content_hash,'status':'answer','kind':'single_choice','confidence':0,'selections':[{'option_id':q.options[0].id,'role':None}],'text':None,'calculation':None}
        for changes in ({'question_id':'stale'},{'content_hash':'stale'},{'confidence':-0.1},{'confidence':True},
                        {'selections':[{'option_id':'absent','role':None}]},{'unexpected':1},{'status':'abstain'},
                        {'calculation':{'op':'invented','args':['999999','1']}}):
            with self.subTest(changes=changes):
                client.complete.return_value={**valid,**changes}
                result=await AnswerEngine(bank,client).propose(q,authorized_qa=True,best_effort=True)
                self.assertIsNone(result.proposal)
        client.reset_mock()
        result=await AnswerEngine(bank,client).propose(replace(q,completeness=False),authorized_qa=True,best_effort=True)
        self.assertEqual(result.reason,'incomplete_question')
        client.complete.assert_not_called()
        for kwargs in ({'allow_model':False,'authorized_qa':True},{}):
            result=await AnswerEngine(bank,client).propose(q,best_effort=True,**kwargs)
            self.assertIsNone(result.proposal)
        self.assertEqual(bank.mock_calls,[])

    async def test_arithmetic_with_named_options_keeps_explicit_choice(self):
        from qa_bot.domain.question import OptionSpec
        q=replace(spec(),options=(OptionSpec('a',1,'Alice and Ben'),OptionSpec('b',2,'Carl and Dana')))
        q=replace(q,content_hash=fingerprint(q)[0])
        client=AsyncMock()
        client.complete.return_value={'question_id':q.question_id,'content_hash':q.content_hash,'status':'answer','kind':'single_choice','confidence':1,'selections':[{'option_id':'b','role':None}],'text':None,'calculation':{'op':'add','args':['4','5']}}
        with QuestionBank(':memory:') as bank:
            result=await AnswerEngine(bank,client).propose(q,authorized_qa=True)
        self.assertEqual(result.proposal.selections[0].option_id,'b')
        client.complete.return_value['selections']=[{'option_id':'missing','role':None}]
        with QuestionBank(':memory:') as bank:
            result=await AnswerEngine(bank,client).propose(q,authorized_qa=True)
        self.assertIsNone(result.proposal)

    async def test_currency_and_grouped_options_remain_exact(self):
        from decimal import Decimal
        from qa_bot.domain.question import OptionSpec
        q=replace(spec(),options=(OptionSpec('a',1,'$950'),OptionSpec('b',2,'$1,500.25')))
        self.assertEqual(match_number(q,Decimal('1500.25')),'b')
        for label,error in (('€1,500.25','mixed'),('$1,50.25','unsupported')):
            with self.assertRaisesRegex(ValueError,error):
                match_number(replace(q,options=(q.options[0],OptionSpec('b',2,label))),Decimal('1500.25'))

    async def test_clock_options_use_minutes_since_midnight(self):
        from decimal import Decimal
        from qa_bot.domain.question import OptionSpec
        q=replace(spec(),options=tuple(OptionSpec(str(i),i,label) for i,label in enumerate(('12:00 AM','12:00 PM','4:30 PM'),1)))
        for value,identity in ((0,'1'),(720,'2'),(990,'3')):
            self.assertEqual(match_number(q,Decimal(value)),identity)
        with self.assertRaisesRegex(ValueError,'absent'):
            match_number(q,Decimal('4.5'))
        mixed=replace(q,options=(q.options[0],OptionSpec('4',4,'990 min')))
        with self.assertRaisesRegex(ValueError,'mixed'):
            match_number(mixed,Decimal(990))

    async def test_calculation_binds_unit_option_without_second_text_payload(self):
        from qa_bot.domain.question import OptionSpec
        q=replace(spec(),options=(OptionSpec('a',1,'55.75 inches'),OptionSpec('b',2,'57.75 inches')))
        q=replace(q,content_hash=fingerprint(q)[0])
        client=AsyncMock()
        client.complete.return_value={'question_id':q.question_id,'content_hash':q.content_hash,'status':'answer','kind':'single_choice','confidence':1,'selections':[{'option_id':'b','role':None}],'text':'57.75','calculation':{'op':'mul','args':['21','2.75']}}
        with QuestionBank(':memory:') as bank:
            result=await AnswerEngine(bank,client).propose(q,authorized_qa=True)
        self.assertIsNotNone(result.proposal)
        self.assertEqual(result.proposal.selections[0].option_id,'b')
        self.assertIsNone(result.proposal.text)

    async def test_numeric_options_allow_shared_units_and_reject_mixed_units(self):
        from decimal import Decimal
        from qa_bot.domain.question import OptionSpec
        q=replace(spec(),options=(OptionSpec('a',1,'55.75 inches'),OptionSpec('b',2,'57.75 inches')))
        self.assertEqual(match_number(q,Decimal('57.75')),'b')
        mixed=replace(q,options=(q.options[0],OptionSpec('b',2,'57.75 cm')))
        with self.assertRaisesRegex(ValueError,'mixed'):
            match_number(mixed,Decimal('57.75'))

    async def test_exact_history_is_ready_without_model_call(self):
        q, bank, client, historical = spec(), MagicMock(), AsyncMock(), MagicMock()
        bank.lookup.return_value = None
        historical.lookup = lambda _question: SimpleNamespace(
            proposal=answer(q), occurrence_count=4, official_answer_key=False)
        engine = AnswerEngine(bank, client, historical=historical)
        result = await engine.propose(q, synthetic=False)
        self.assertEqual(result.source, "historical_exact")
        self.assertIn("occurrences=4", result.reason)
        client.complete.assert_not_called()
        pipeline = AnswerPipeline(engine)
        ready = await pipeline.process(q, synthetic=False, apply=False)
        self.assertEqual(ready.status, "ready")
        self.assertEqual(ready.source, "historical_exact")

    async def test_pipeline_candidate_stops_before_action(self):
        from qa_bot.solvers.engine import EngineResult
        engine, executor = AsyncMock(), AsyncMock()
        engine.propose.return_value = EngineResult(answer(spec()), "candidate")
        pipeline = AnswerPipeline(engine, executor=executor)
        result = await pipeline.process(spec(), synthetic=True, apply=True)
        self.assertEqual(result.status, "review_required")
        executor.apply.assert_not_called()

    async def test_approved_not_overwritten_and_similar_is_only_suggestion(self):
        q = spec()
        with QuestionBank(":memory:") as bank:
            bank.save_candidate(q, answer(q))
            bank.approve(q, reviewer="human", reason="checked")
            with self.assertRaises(ValueError):
                bank.save_candidate(q, answer(q))
            changed = replace(q, question_text=q.question_text + "?")
            self.assertTrue(bank.similar(changed))
            self.assertIsNone(bank.lookup(changed))

    async def test_media_hash_changes_fingerprint(self):
        q = spec("ANA-03")
        first = replace(q, assets=(replace(q.assets[0], sha256="a" * 64),))
        other = replace(q, assets=(replace(q.assets[0], sha256="b" * 64),))
        self.assertNotEqual(fingerprint(first)[0], fingerprint(other)[0])

    async def test_corpus_candidates_never_approved(self):
        with QuestionBank(":memory:") as bank:
            bank.import_corpus([{"id": "raw1", "text": "Question", "options": []}], {"raw1": "draft"})
            found = bank.corpus_candidates("Question")[0]
            self.assertEqual(found["status"], "candidate")
            self.assertEqual(found["similarity"], 1)
            self.assertEqual(bank.db.execute("SELECT count(*) FROM answers").fetchone()[0], 0)

    async def test_text_limits_and_multiple_roles(self):
        q = spec("WRITEX-01")
        a = AnswerProposal(q.question_id, q.content_hash, ResponseKind.TEXT, 0.99, text="short")
        self.assertIn("too_few_words", validate_answer(q, a))
        q = spec("SALES-01")
        a = AnswerProposal(q.question_id, q.content_hash, ResponseKind.MULTI_CHOICE, 0.99,
                           selections=(Selection("o1"), Selection("o2")))
        self.assertIn("selection_roles", validate_answer(q, a))

    async def test_only_exact_historical_ui_plan_passes_plan_gate(self):
        q = spec("COMP-01")
        model_plan = AnswerProposal(q.question_id, q.content_hash, ResponseKind.UI_ACTION,
                                    0.99, plan_id="model:anything")
        self.assertIn("ui_plans_not_accepted_from_model", validate_answer(q, model_plan))
        historical = replace(
            model_plan, plan_id="historical:" + "a" * 64,
            evidence=("exact_historical_match", "source:TP-008-COMPUTER-001"),
        )
        self.assertNotIn("ui_plans_not_accepted_from_model", validate_answer(q, historical))

    async def test_bank_candidate_approval_and_rebinding(self):
        q = spec()
        with QuestionBank(":memory:") as bank:
            bank.save_candidate(q, answer(q))
            self.assertIsNone(bank.lookup(q))
            bank.approve(q, reviewer="human", reason="checked synthetic fixture")
            changed_id = replace(q, question_id="next-instance")
            self.assertEqual(bank.lookup(changed_id).question_id, "next-instance")
            self.assertIsNone(bank.lookup(replace(q, question_text=q.question_text + " NOT")))

    async def test_exact_media_and_order(self):
        q = spec()
        self.assertNotEqual(fingerprint(q)[0],
                            fingerprint(replace(q, options=tuple(reversed(q.options))))[0])
        with self.assertRaises(ValueError):
            fingerprint(spec("ANA-03"))  # source bytes not hashed yet

    async def test_validator_identity_confidence_and_options(self):
        q = spec()
        self.assertEqual(validate_answer(q, answer(q)), ())
        for proposal in (replace(answer(q), question_id="wrong"),
                         replace(answer(q), confidence=0.2),
                         replace(answer(q), selections=(Selection("absent"),))):
            self.assertTrue(validate_answer(q, proposal))

    async def test_calculation_and_units(self):
        self.assertEqual(calculate({'op':'div','args':[{'op':'add','args':['100','200','300']},'3']}),200)
        self.assertEqual(calculate({'op':'mul','args':['2','3','4']}),24)
        for operation in ('add','mul'):
            for values in (['1'],['1']*33):
                with self.assertRaises(ValueError):calculate({'op':operation,'args':values})
        self.assertEqual(calculate({"op": "percent", "args": ["200", "15"]}), 30)
        self.assertEqual(calculate({"op": "proportion", "args": ["3", "12", "5"]}), 20)
        self.assertEqual(convert_unit("1", "km", "m"), 1000)
        for node in ({"op": "eval", "args": ["bad"]},
                     {"op": "div", "args": ["1", "0"]}):
            with self.assertRaises(ValueError):
                calculate(node)

    async def test_engine_unknown_then_approved_cache(self):
        q, client = spec(), AsyncMock()
        client.complete.return_value = {
            "question_id": q.question_id, "content_hash": q.content_hash,
            "status": "answer", "kind": "single_choice", "confidence": 0.99,
            "selections": [{"option_id": "o1", "role": None}], "text": None,
            "calculation": None,
        }
        with QuestionBank(":memory:") as bank:
            engine = AnswerEngine(bank, client)
            result = await engine.propose(q, synthetic=True)
            self.assertIsNotNone(result.proposal, result.reason)
            bank.approve(q, reviewer="human", reason="verified")
            client.reset_mock()
            self.assertEqual((await engine.propose(q, synthetic=True)).source, "approved")
            client.complete.assert_not_called()

    async def test_authorized_qa_allows_proposal_without_fake_approval(self):
        q, client = spec(), AsyncMock()
        client.complete.return_value = {
            'question_id':q.question_id,'content_hash':q.content_hash,
            'status':'answer','kind':'single_choice','confidence':.99,
            'selections':[{'option_id':'o1','role':None}],'text':None,'calculation':None,
        }
        with QuestionBank(':memory:') as bank:
            engine=AnswerEngine(bank,client)
            self.assertIsNone((await engine.propose(q)).proposal)
            client.complete.assert_not_called()
            result=await engine.propose(q,authorized_qa=True)
            self.assertEqual(result.source,'candidate')
            self.assertIsNotNone(result.proposal)
            self.assertIsNone(bank.lookup(q))

    async def test_engine_abstain_and_stale(self):
        q, client = spec(), AsyncMock()
        client.complete.return_value = {"status": "abstain"}
        with QuestionBank(":memory:") as bank:
            engine = AnswerEngine(bank, client)
            self.assertIsNone((await engine.propose(q, synthetic=True)).proposal)
            client.complete.reset_mock()
            self.assertIsNone((await engine.propose(q, synthetic=False)).proposal)
            client.complete.assert_not_called()
