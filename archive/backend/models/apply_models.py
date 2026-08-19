import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.database import Base


class ApplyStatus(str, PyEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    WAITING_CAPTCHA = "waiting_captcha"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class QuestionType(str, PyEnum):
    TEXT = "text"
    SELECT = "select"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    TEXTAREA = "textarea"


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_code: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuid.uuid4())[:8])
    full_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(50))
    location: Mapped[str] = mapped_column(String(200), default="Remote, US")
    linkedin_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resume_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resume_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    years_experience: Mapped[int] = mapped_column(Integer, default=5)
    desired_salary: Mapped[str | None] = mapped_column(String(100), nullable=True)
    work_authorization: Mapped[str] = mapped_column(String(100), default="Authorized to work in US")
    available_start: Mapped[str] = mapped_column(String(100), default="Immediately")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ApplyQueue(Base):
    __tablename__ = "apply_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user_profiles.id"), nullable=False, index=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id"))
    status: Mapped[ApplyStatus] = mapped_column(Enum(ApplyStatus), default=ApplyStatus.PENDING)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class QuestionBank(Base):
    __tablename__ = "question_bank"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_hash: Mapped[str] = mapped_column(String(64), unique=True)
    question_text: Mapped[str] = mapped_column(Text)
    answer_text: Mapped[str] = mapped_column(Text)
    question_type: Mapped[QuestionType] = mapped_column(
        Enum(QuestionType), default=QuestionType.TEXT
    )
    options: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list for select/radio
    source_domain: Mapped[str | None] = mapped_column(String(200), nullable=True)
    times_used: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ApplyLog(Base):
    __tablename__ = "apply_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    queue_id: Mapped[int] = mapped_column(Integer, ForeignKey("apply_queue.id"))
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id"))
    action: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
