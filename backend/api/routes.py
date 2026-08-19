from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import get_session
from backend.models.job import ApplicationStatus, Job, JobSource
from backend.scrapers.manager import run_all_scrapers

from .schemas import (
    JobListResponse,
    JobResponse,
    ScrapeResponse,
    StatsResponse,
    StatusUpdate,
)

router = APIRouter()


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    source: JobSource | None = None,
    country: str | None = None,
    status: ApplicationStatus | None = None,
    equipment: str | None = None,
    hiring_speed: str | None = None,
    search: str | None = None,
    min_salary: int | None = None,
    sort_by: str = Query("created_at", pattern="^(created_at|score|salary_max|title)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    query = select(Job)

    if source:
        query = query.where(Job.source == source)
    if country:
        query = query.where(Job.country == country)
    if status:
        query = query.where(Job.status == status)
    if equipment:
        query = query.where(Job.equipment == equipment)
    if hiring_speed:
        query = query.where(Job.hiring_speed == hiring_speed)
    if search:
        query = query.where(
            Job.title.ilike(f"%{search}%") | Job.company.ilike(f"%{search}%")
        )
    if min_salary:
        query = query.where(Job.salary_max >= min_salary)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_query)).scalar() or 0

    sort_col = getattr(Job, sort_by)
    if sort_order == "desc":
        query = query.order_by(sort_col.desc().nullslast())
    else:
        query = query.order_by(sort_col.asc().nullsfirst())

    query = query.limit(limit).offset(offset)
    result = await session.execute(query)
    jobs = result.scalars().all()

    return JobListResponse(
        jobs=[JobResponse.model_validate(j) for j in jobs],
        total=total,
    )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)):
    job = await session.get(Job, job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, "Job not found")
    return JobResponse.model_validate(job)


@router.patch("/jobs/{job_id}/status", response_model=JobResponse)
async def update_status(
    job_id: int,
    body: StatusUpdate,
    session: AsyncSession = Depends(get_session),
):
    job = await session.get(Job, job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, "Job not found")

    job.status = body.status
    if body.status == ApplicationStatus.APPLIED:
        from datetime import datetime
        job.applied_at = datetime.utcnow()

    await session.commit()
    await session.refresh(job)
    return JobResponse.model_validate(job)


@router.get("/stats", response_model=StatsResponse)
async def get_stats(session: AsyncSession = Depends(get_session)):
    total = (await session.execute(select(func.count(Job.id)))).scalar() or 0
    new = (await session.execute(
        select(func.count(Job.id)).where(Job.status == ApplicationStatus.NEW)
    )).scalar() or 0
    applied = (await session.execute(
        select(func.count(Job.id)).where(Job.status == ApplicationStatus.APPLIED)
    )).scalar() or 0
    interviews = (await session.execute(
        select(func.count(Job.id)).where(Job.status == ApplicationStatus.INTERVIEW)
    )).scalar() or 0
    offers = (await session.execute(
        select(func.count(Job.id)).where(Job.status == ApplicationStatus.OFFER)
    )).scalar() or 0

    by_source_rows = (await session.execute(
        select(Job.source, func.count(Job.id)).group_by(Job.source)
    )).all()
    by_source = {row[0].value: row[1] for row in by_source_rows}

    by_country_rows = (await session.execute(
        select(Job.country, func.count(Job.id)).group_by(Job.country)
    )).all()
    by_country = {row[0]: row[1] for row in by_country_rows}

    return StatsResponse(
        total_jobs=total,
        new_jobs=new,
        applied=applied,
        interviews=interviews,
        offers=offers,
        by_source=by_source,
        by_country=by_country,
    )


@router.post("/scrape", response_model=ScrapeResponse)
async def trigger_scrape(session: AsyncSession = Depends(get_session)):
    new_count = await run_all_scrapers(session)
    return ScrapeResponse(new_jobs=new_count, message=f"Found {new_count} new jobs")
