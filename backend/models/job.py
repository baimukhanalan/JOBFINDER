from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.database import Base


class JobSource(str, PyEnum):
    INDEED = "indeed"
    REMOTEOK = "remoteok"
    WEWORKREMOTELY = "weworkremotely"
    LINKEDIN = "linkedin"
    ZIPRECRUITER = "ziprecruiter"
    GLASSDOOR = "glassdoor"


class EquipmentType(str, PyEnum):
    BYOD = "byod"
    CORPORATE = "corporate"
    STIPEND = "stipend"
    UNKNOWN = "unknown"


class ApplicationStatus(str, PyEnum):
    NEW = "new"
    SAVED = "saved"
    APPLIED = "applied"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(500))
    company: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(2000), unique=True)
    salary_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    location: Mapped[str] = mapped_column(String(300), default="Remote")
    country: Mapped[str] = mapped_column(String(50))  # US or CA
    source: Mapped[JobSource] = mapped_column(Enum(JobSource))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[ApplicationStatus] = mapped_column(
        Enum(ApplicationStatus), default=ApplicationStatus.NEW
    )
    equipment: Mapped[str] = mapped_column(
        String(20), default="unknown"
    )
    hiring_speed: Mapped[str] = mapped_column(
        String(20), default="unknown"
    )
    apply_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_letter: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
