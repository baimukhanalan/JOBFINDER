"""Strict text solver for spoken-response tasks."""
from __future__ import annotations

import hashlib


class SpokenTextSolver:
    def __init__(self, client, *, min_words: int = 3):
        if type(min_words) is not int or not 1 <= min_words <= 200:
            raise ValueError("invalid spoken answer word limit")
        self.client = client
        self.min_words = min_words

    async def __call__(self, question_text: str, transcript: str | None = None,
                       *, timeout: float = 60) -> str:
        source = question_text + ("\n" + transcript if transcript else "")
        digest = hashlib.sha256(source.encode()).hexdigest()
        payload = {
            "question_id": "spoken-" + digest[:16],
            "content_hash": digest,
            "section": "SVAR - Spoken English (U.S.)",
            "type_id": "SVAR-SPOKEN-ANSWER",
            "instruction": question_text,
            "question_text": transcript or question_text,
            "options": [],
            "tables": [],
            "context": [transcript] if transcript else [],
            "response_contract": {
                "kind": "text", "min_selections": 0, "max_selections": 0,
                "roles": [], "min_words": self.min_words,
            },
        }
        data = await self.client.complete(payload, timeout=timeout)
        required = {"question_id", "content_hash", "status", "kind", "confidence",
                    "selections", "text", "calculation"}
        if not isinstance(data, dict) or set(data) != required:
            raise ValueError("invalid spoken solver schema")
        if (data["question_id"] != payload["question_id"]
                or data["content_hash"] != digest):
            raise ValueError("stale spoken solver response")
        if (data["status"] != "answer" or data["kind"] != "text"
                or data["selections"] or data["calculation"] is not None
                or isinstance(data["confidence"], bool)
                or not isinstance(data["confidence"], (int, float))
                or data["confidence"] < 0.9):
            raise ValueError("spoken solver abstained or returned low confidence")
        text = data["text"]
        if not isinstance(text, str) or len(text.split()) < self.min_words:
            raise ValueError("spoken solver answer is incomplete")
        return " ".join(text.split())
