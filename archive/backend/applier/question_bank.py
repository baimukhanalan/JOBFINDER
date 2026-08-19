import hashlib
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.apply_models import QuestionBank, QuestionType

logger = logging.getLogger(__name__)


def hash_question(text: str) -> str:
    """Normalize and hash a question for dedup."""
    normalized = text.lower().strip()
    # Remove extra whitespace and common prefixes
    for prefix in ("please ", "q: ", "question: "):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    return hashlib.sha256(normalized.encode()).hexdigest()


async def find_answer(session: AsyncSession, question_text: str) -> str | None:
    """Look up a question in the bank. Returns answer or None."""
    qhash = hash_question(question_text)
    result = await session.execute(
        select(QuestionBank).where(QuestionBank.question_hash == qhash)
    )
    entry = result.scalar_one_or_none()
    if entry:
        await session.execute(
            update(QuestionBank)
            .where(QuestionBank.id == entry.id)
            .values(times_used=entry.times_used + 1)
        )
        await session.commit()
        return entry.answer_text
    return None


async def save_answer(
    session: AsyncSession,
    question_text: str,
    answer_text: str,
    question_type: str = "text",
    options: str | None = None,
    source_domain: str | None = None,
) -> QuestionBank:
    """Save a new question-answer pair."""
    qhash = hash_question(question_text)

    # Check if exists
    existing = await session.execute(
        select(QuestionBank).where(QuestionBank.question_hash == qhash)
    )
    entry = existing.scalar_one_or_none()
    if entry:
        entry.answer_text = answer_text
        await session.commit()
        return entry

    entry = QuestionBank(
        question_hash=qhash,
        question_text=question_text,
        answer_text=answer_text,
        question_type=QuestionType(question_type) if question_type in QuestionType.__members__.values() else QuestionType.TEXT,
        options=options,
        source_domain=source_domain,
    )
    session.add(entry)
    await session.commit()
    logger.info("Saved new Q&A: '%s' → '%s'", question_text[:60], answer_text[:60])
    return entry


async def get_all_answers(session: AsyncSession) -> dict[str, str]:
    """Get all known question-answer pairs as a dict."""
    result = await session.execute(select(QuestionBank))
    entries = result.scalars().all()
    return {e.question_text: e.answer_text for e in entries}
