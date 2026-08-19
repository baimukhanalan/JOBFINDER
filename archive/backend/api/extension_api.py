"""API endpoints for the Chrome extension Quick Apply."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.applier.question_bank import find_answer, save_answer, get_all_answers
from backend.models.database import get_session
from backend.models.job import Job

ext_router = APIRouter(prefix="/ext", tags=["extension"])


class NextJobResponse(BaseModel):
    job_id: int
    title: str
    company: str
    apply_url: str
    description: str | None = None


class AnswerSave(BaseModel):
    question: str
    answer: str
    domain: str | None = None


class ApplyResult(BaseModel):
    job_id: int
    status: str  # submitted, skipped, expired, no_form, error
    error: str | None = None


# Profile data for auto-fill
PROFILE = {
    "full_name": "Dana Devlin",
    "first_name": "Dana",
    "last_name": "Devlin",
    "email": "dana.devlin.80@outlook.com",
    "phone": "7737658628",
    "location": "Chicago, IL",
    "linkedin": "https://linkedin.com/in/dana-devlin",
    "years_experience": "5",
    "work_authorization": "Yes",
    "available_start": "Immediately",
    "country": "United States",
    "resume_url": "/api/ext/resume",
}


@ext_router.get("/profile")
async def get_profile():
    """Get profile data for form auto-fill."""
    return PROFILE


@ext_router.get("/next")
async def get_next_job(
    after: int = 0,
    platform: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Get next job to apply to."""
    # Ensure apply_results table exists
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS apply_results (
            id SERIAL PRIMARY KEY,
            job_id INTEGER REFERENCES jobs(id),
            status VARCHAR(50) NOT NULL,
            error TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    await session.commit()

    query = """
        SELECT j.id, j.title, j.company, j.apply_url, j.description
        FROM jobs j
        WHERE j.apply_url IS NOT NULL
          AND j.apply_url != 'NOT_FOUND'
          AND j.id > :after
          AND j.id NOT IN (
            SELECT job_id FROM apply_results
          )
    """
    params = {"after": after}
    if platform:
        query += " AND j.apply_url LIKE :platform"
        params["platform"] = f"%{platform}%"
    query += " ORDER BY j.id LIMIT 1"

    result = await session.execute(text(query), params)
    row = result.first()
    if not row:
        return {"done": True, "message": "No more jobs"}

    return {
        "done": False,
        "job_id": row[0],
        "title": row[1],
        "company": row[2],
        "apply_url": row[3],
        "description": (row[4] or "")[:500],
    }


@ext_router.get("/answers")
async def get_answers(session: AsyncSession = Depends(get_session)):
    """Get all known Q&A pairs for auto-fill."""
    answers = await get_all_answers(session)
    return {"answers": answers}


@ext_router.post("/answers")
async def save_answer_endpoint(
    body: AnswerSave,
    session: AsyncSession = Depends(get_session),
):
    """Save a new question-answer pair."""
    entry = await save_answer(session, body.question, body.answer, source_domain=body.domain)
    return {"ok": True, "id": entry.id}


@ext_router.post("/result")
async def save_result(
    body: ApplyResult,
    session: AsyncSession = Depends(get_session),
):
    """Save apply result for a job."""
    # Create table if not exists (simple approach)
    await session.execute(text("""
        CREATE TABLE IF NOT EXISTS apply_results (
            id SERIAL PRIMARY KEY,
            job_id INTEGER REFERENCES jobs(id),
            status VARCHAR(50) NOT NULL,
            error TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    await session.execute(
        text("INSERT INTO apply_results (job_id, status, error) VALUES (:job_id, :status, :error)"),
        {"job_id": body.job_id, "status": body.status, "error": body.error},
    )
    await session.commit()
    return {"ok": True}


@ext_router.get("/stats")
async def get_stats(session: AsyncSession = Depends(get_session)):
    """Get apply stats."""
    try:
        result = await session.execute(text("""
            SELECT status, COUNT(*) FROM apply_results GROUP BY status
        """))
        stats = {row[0]: row[1] for row in result}
    except Exception:
        stats = {}

    total = await session.execute(text("""
        SELECT COUNT(*) FROM jobs
        WHERE apply_url IS NOT NULL AND apply_url != 'NOT_FOUND'
    """))
    stats["total_jobs"] = total.scalar() or 0
    return stats


@ext_router.get("/download")
async def download_extension():
    """Download extension archive."""
    from fastapi.responses import FileResponse
    return FileResponse(
        "/home/projects/jobfinder/extension/quick-apply-extension.tar.gz",
        filename="quick-apply-extension.tar.gz",
        media_type="application/gzip",
    )


@ext_router.get("/resume")
async def get_resume():
    """Serve resume file."""
    import os
    from fastapi.responses import FileResponse
    path = "/home/projects/jobfinder/uploads/resumes/609338c4_Dana_Devlin_CV.pdf"
    if os.path.exists(path):
        return FileResponse(path, filename="Dana_Devlin_CV.pdf", media_type="application/pdf")
    return {"error": "Resume not found"}
