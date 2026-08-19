import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import router
from backend.api.apply_routes import apply_router
from backend.config import settings
from backend.models.database import async_session, init_db
from backend.scrapers.manager import run_all_scrapers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="JobFinder", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")
app.include_router(apply_router, prefix="/api/apply")

scheduler = AsyncIOScheduler()


async def scheduled_scrape():
    async with async_session() as session:
        new_count = await run_all_scrapers(session)
        logger.info(f"Scheduled scrape: {new_count} new jobs")


@app.on_event("startup")
async def startup():
    await init_db()
    scheduler.add_job(
        scheduled_scrape,
        "interval",
        minutes=settings.scrape_interval_minutes,
    )
    scheduler.start()
    logger.info(f"Scraping every {settings.scrape_interval_minutes} minutes")


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
