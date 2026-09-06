import unittest
from unittest.mock import AsyncMock

from qa_bot.solvers.spoken import SpokenTextSolver


class SpokenSolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_strict_current_text_answer(self):
        client = AsyncMock()

        async def complete(payload, **_kwargs):
            return {
                "question_id": payload["question_id"],
                "content_hash": payload["content_hash"],
                "status": "answer", "kind": "text", "confidence": 0.99,
                "selections": [], "text": "Please contact customer support today.",
                "calculation": None,
            }

        client.complete = complete
        solver = SpokenTextSolver(client, min_words=4)
        answer = await solver("Answer the customer.", "What should I do next?")
        self.assertEqual(answer, "Please contact customer support today.")

    async def test_rejects_stale_low_confidence_and_short_answers(self):
        for change in (
            {"content_hash": "stale"},
            {"confidence": 0.5},
            {"text": "Too short"},
        ):
            client = AsyncMock()

            async def complete(payload, _change=change, **_kwargs):
                result = {
                    "question_id": payload["question_id"],
                    "content_hash": payload["content_hash"],
                    "status": "answer", "kind": "text", "confidence": 0.99,
                    "selections": [], "text": "A complete spoken answer is here.",
                    "calculation": None,
                }
                result.update(_change)
                return result

            client.complete = complete
            with self.assertRaises(ValueError):
                await SpokenTextSolver(client, min_words=4)("Question", None)
