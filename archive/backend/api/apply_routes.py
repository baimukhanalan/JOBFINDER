import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select, func, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.apply_models import (
    ApplyLog, ApplyQueue, ApplyStatus, QuestionBank, UserProfile,
)
from backend.models.database import get_session
from backend.models.job import Job

router = APIRouter(prefix="/apply", tags=["auto-apply"])


# === Helpers ===

async def _get_user(session: AsyncSession, user_code: str) -> UserProfile:
    """Get user by code or raise 404."""
    result = await session.execute(
        select(UserProfile).where(UserProfile.user_code == user_code)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    return user


# === Schemas ===

class ProfileCreate(BaseModel):
    full_name: str
    email: str
    phone: str
    location: str = "Remote, US"
    linkedin_url: str | None = None
    resume_path: str | None = None
    resume_text: str | None = None
    years_experience: int = 2
    desired_salary: str | None = None
    work_authorization: str = "Authorized to work in US"
    available_start: str = "Immediately"


class QueueAdd(BaseModel):
    job_ids: list[int]
    priority: int = 0


class AnswerCreate(BaseModel):
    question_text: str
    answer_text: str
    question_type: str = "text"


# === User Code Generation ===

@router.api_route("/new", methods=["GET", "POST"])
async def create_user_code(session: AsyncSession = Depends(get_session)):
    """Generate a new unique user code (admin use)."""
    code = str(uuid.uuid4())[:8]
    profile = UserProfile(
        user_code=code,
        full_name="",
        email="",
        phone="",
    )
    session.add(profile)
    await session.commit()
    return {"user_code": code}


# === Profile ===

@router.get("/profile")
async def get_profile(
    u: str = Query(..., description="User code"),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(UserProfile).where(UserProfile.user_code == u)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        return None
    return profile


@router.post("/profile")
async def save_profile(
    data: ProfileCreate,
    u: str = Query(..., description="User code"),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(UserProfile).where(UserProfile.user_code == u)
    )
    profile = result.scalar_one_or_none()
    if profile:
        for key, val in data.model_dump().items():
            setattr(profile, key, val)
    else:
        profile = UserProfile(user_code=u, **data.model_dump())
        session.add(profile)
    await session.commit()
    return profile


# === Resume Upload ===

RESUME_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads", "resumes")
os.makedirs(RESUME_DIR, exist_ok=True)


@router.post("/resume")
async def upload_resume(
    u: str = Query(..., description="User code"),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    user = await _get_user(session, u)

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files accepted")

    # Save with user code prefix to avoid collisions
    safe_name = f"{u}_{file.filename.replace('/', '_').replace(chr(92), '_')}"
    path = os.path.join(RESUME_DIR, safe_name)
    content = await file.read()

    with open(path, "wb") as f:
        f.write(content)

    # Extract text from PDF
    text = ""
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except ImportError:
        text = "(pdfplumber not installed — text extraction unavailable)"
    except Exception:
        text = "(failed to extract text)"

    # Update user profile with resume path
    user.resume_path = path
    user.resume_text = text.strip()[:5000] or None
    await session.commit()

    return {"path": path, "text": text.strip()[:5000]}


# === Queue ===

@router.get("/queue")
async def get_queue(
    u: str = Query(..., description="User code"),
    session: AsyncSession = Depends(get_session),
):
    user = await _get_user(session, u)
    result = await session.execute(
        select(ApplyQueue, Job.title, Job.company, Job.url)
        .join(Job, ApplyQueue.job_id == Job.id)
        .where(ApplyQueue.user_id == user.id)
        .order_by(ApplyQueue.priority.desc(), ApplyQueue.created_at)
    )
    rows = result.all()
    return [
        {
            "id": q.id, "job_id": q.job_id, "status": q.status.value,
            "priority": q.priority, "attempts": q.attempts,
            "error_message": q.error_message,
            "title": title, "company": company, "url": url,
            "created_at": q.created_at.isoformat() if q.created_at else None,
        }
        for q, title, company, url in rows
    ]


@router.post("/queue")
async def add_to_queue(
    data: QueueAdd,
    u: str = Query(..., description="User code"),
    session: AsyncSession = Depends(get_session),
):
    user = await _get_user(session, u)
    added = 0
    for job_id in data.job_ids:
        existing = await session.execute(
            select(ApplyQueue).where(
                ApplyQueue.job_id == job_id,
                ApplyQueue.user_id == user.id,
            )
        )
        if existing.scalar_one_or_none():
            continue
        entry = ApplyQueue(job_id=job_id, user_id=user.id, priority=data.priority)
        session.add(entry)
        added += 1
    await session.commit()
    return {"added": added, "total_requested": len(data.job_ids)}


@router.post("/queue/all")
async def add_all_to_queue(
    u: str = Query(..., description="User code"),
    session: AsyncSession = Depends(get_session),
):
    """Add all jobs that aren't already in this user's queue."""
    user = await _get_user(session, u)
    result = await session.execute(
        select(Job.id).where(
            ~Job.id.in_(
                select(ApplyQueue.job_id).where(ApplyQueue.user_id == user.id)
            )
        )
    )
    job_ids = [row[0] for row in result.all()]
    for jid in job_ids:
        session.add(ApplyQueue(job_id=jid, user_id=user.id))
    await session.commit()
    return {"added": len(job_ids)}


@router.delete("/queue/{queue_id}")
async def remove_from_queue(
    queue_id: int,
    u: str = Query(..., description="User code"),
    session: AsyncSession = Depends(get_session),
):
    user = await _get_user(session, u)
    await session.execute(
        delete(ApplyQueue).where(ApplyQueue.id == queue_id, ApplyQueue.user_id == user.id)
    )
    await session.commit()
    return {"ok": True}


@router.get("/queue/stats")
async def queue_stats(
    u: str = Query(..., description="User code"),
    session: AsyncSession = Depends(get_session),
):
    user = await _get_user(session, u)
    result = await session.execute(
        select(ApplyQueue.status, func.count(ApplyQueue.id))
        .where(ApplyQueue.user_id == user.id)
        .group_by(ApplyQueue.status)
    )
    stats = {row[0].value: row[1] for row in result.all()}
    return stats


# === Question Bank (shared across all users) ===

@router.get("/questions")
async def get_questions(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(QuestionBank).order_by(QuestionBank.times_used.desc())
    )
    return result.scalars().all()


@router.post("/questions")
async def add_answer(data: AnswerCreate, session: AsyncSession = Depends(get_session)):
    from backend.applier.question_bank import save_answer
    entry = await save_answer(session, data.question_text, data.answer_text, data.question_type)
    return entry


@router.delete("/questions/{question_id}")
async def delete_question(question_id: int, session: AsyncSession = Depends(get_session)):
    await session.execute(delete(QuestionBank).where(QuestionBank.id == question_id))
    await session.commit()
    return {"ok": True}


# === Logs ===

@router.get("/log/{job_id}")
async def get_apply_log(job_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(ApplyLog).where(ApplyLog.job_id == job_id).order_by(ApplyLog.created_at)
    )
    return result.scalars().all()
