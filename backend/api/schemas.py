from datetime import datetime

from pydantic import BaseModel

from backend.models.job import ApplicationStatus, JobSource


class JobResponse(BaseModel):
    id: int
    title: str
    company: str
    url: str
    salary_min: int | None
    salary_max: int | None
    salary_text: str | None
    location: str
    country: str
    source: JobSource
    description: str | None
    tags: str | None
    score: float | None
    equipment: str
    hiring_speed: str
    status: ApplicationStatus
    cover_letter: str | None
    applied_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int


class StatusUpdate(BaseModel):
    status: ApplicationStatus


class StatsResponse(BaseModel):
    total_jobs: int
    new_jobs: int
    applied: int
    interviews: int
    offers: int
    by_source: dict[str, int]
    by_country: dict[str, int]


class ScrapeResponse(BaseModel):
    new_jobs: int
    message: str
