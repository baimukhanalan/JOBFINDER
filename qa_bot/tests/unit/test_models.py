import unittest
from dataclasses import FrozenInstanceError
from qa_bot.domain.question import QuestionSpec, OptionSpec, ResponseContract, ResponseKind, TableSpec
from qa_bot.domain.answer import AnswerProposal, Selection
from qa_bot.domain.actions import Action, ActionKind, ActionPlan, ActionResult
from qa_bot.domain.state import RunState


class ModelTests(unittest.TestCase):
    def question(self, **overrides):
        values = dict(question_id="q1", attempt_id="a1", section="Analytical",
                      type_id="ANA-01", question_text="Synthetic fixture",
                      content_hash="fixture-hash",
                      options=(OptionSpec("o1", 1, "A"), OptionSpec("o2", 2, "B")),
                      response_contract=ResponseContract(ResponseKind.SINGLE_CHOICE, 1, 1))
        values.update(overrides)
        return QuestionSpec(**values)

    def test_question_is_frozen(self):
        question = self.question()
        with self.assertRaises(FrozenInstanceError):
            question.question_id = "changed"

    def test_duplicate_option_rejected(self):
        with self.assertRaises(ValueError):
            self.question(options=(OptionSpec("o1", 1, "A"), OptionSpec("o1", 2, "B")))

    def test_insufficient_options_rejected(self):
        with self.assertRaises(ValueError):
            self.question(options=())

    def test_rectangular_table_required(self):
        with self.assertRaises(ValueError):
            TableSpec(("a", "b"), (("1",),))

    def test_choice_requires_exactly_one_selection(self):
        with self.assertRaises(ValueError):
            AnswerProposal("q", "h", ResponseKind.SINGLE_CHOICE, .8,
                           selections=(Selection("a"), Selection("b")))

    def test_sales_roles_and_ids_distinct(self):
        answer = AnswerProposal("q", "h", ResponseKind.MULTI_CHOICE, .8,
                                selections=(Selection("a", "best"), Selection("b", "worst")))
        self.assertEqual(len(answer.selections), 2)
        with self.assertRaises(ValueError):
            AnswerProposal("q", "h", ResponseKind.MULTI_CHOICE, .8,
                           selections=(Selection("a", "best"), Selection("a", "worst")))

    def test_mixed_payload_rejected(self):
        with self.assertRaises(ValueError):
            AnswerProposal("q", "h", ResponseKind.TEXT, .5, text="Hi", audio_ref="audio")

    def test_nan_confidence_rejected(self):
        with self.assertRaises(ValueError):
            AnswerProposal("q", "h", ResponseKind.TEXT, float("nan"), text="Hi")

    def test_plan_duplicate_actions_rejected(self):
        action = Action("a", ActionKind.CLICK, "target")
        with self.assertRaises(ValueError):
            ActionPlan("p", "q", "h", "obs", (action, action))

    def test_unexecuted_result_cannot_claim_confirmation(self):
        with self.assertRaises(ValueError):
            ActionResult("p", submission_confirmed=True)
        self.assertIsNone(ActionResult("p").correctness)

    def test_invalid_counter_rejected(self):
        with self.assertRaises(ValueError):
            RunState("r", processed_count=-1)
