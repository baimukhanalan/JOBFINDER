import asyncio
import logging
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.applier.analyzer import analyze_page
from backend.applier.browser import BrowserManager
from backend.applier.captcha import detect_captcha, wait_for_captcha_resolution
from backend.applier.filler import click_submit, fill_form
from backend.applier.question_bank import find_answer, get_all_answers, save_answer
from backend.config import settings
from backend.models.apply_models import ApplyLog, ApplyQueue, ApplyStatus, UserProfile
from backend.models.database import async_session
from backend.models.job import Job

logger = logging.getLogger(__name__)

# Telegram notify function (set from outside)
_notify_func = None


def set_notify_func(func):
    global _notify_func
    _notify_func = func
    from backend.applier import captcha
    captcha.set_notify_func(func)


async def _log_action(session: AsyncSession, queue_id: int, job_id: int, action: str, details: dict | None = None):
    entry = ApplyLog(queue_id=queue_id, job_id=job_id, action=action, details=details)
    session.add(entry)
    await session.commit()


async def _get_profile(session: AsyncSession, user_id: int) -> dict | None:
    result = await session.execute(select(UserProfile).where(UserProfile.id == user_id))
    profile = result.scalar_one_or_none()
    if not profile:
        return None
    return {
        "full_name": profile.full_name,
        "email": profile.email,
        "phone": profile.phone,
        "location": profile.location,
        "linkedin_url": profile.linkedin_url,
        "years_experience": profile.years_experience,
        "desired_salary": profile.desired_salary,
        "work_authorization": profile.work_authorization,
        "available_start": profile.available_start,
        "resume_path": profile.resume_path,
    }


async def _click_apply_button(page) -> bool:
    """Try to click Apply/I'm interested button to get to the form."""
    selectors = [
        'button:has-text("Apply")',
        'a:has-text("Apply")',
        'button:has-text("I\'m interested")',
        'a:has-text("I\'m interested")',
        'button:has-text("Apply Now")',
        'a:has-text("Apply Now")',
        'button:has-text("Apply for this")',
        'a:has-text("Apply for this")',
        '[data-qa="btn-apply"]',
        '#apply-button',
        '.apply-button',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                logger.info("Clicked apply button: %s", sel)
                return True
        except Exception:
            continue
    return False


async def process_job(queue_entry: ApplyQueue, job: Job) -> bool:
    """Process a single job application. Returns True if successful."""
    async with async_session() as session:
        profile = await _get_profile(session, queue_entry.user_id)
        if not profile:
            logger.error("No user profile configured")
            return False

        known_answers = await get_all_answers(session)

        bm = await BrowserManager.get_instance()
        page = await bm.new_page()

        try:
            # Use direct apply URL if available
            target_url = job.apply_url if (job.apply_url and job.apply_url != "NOT_FOUND") else job.url
            await _log_action(session, queue_entry.id, job.id, "navigate", {"url": target_url})
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Check for CAPTCHA
            if await detect_captcha(page):
                await _update_status(queue_entry.id, ApplyStatus.WAITING_CAPTCHA)
                await _log_action(session, queue_entry.id, job.id, "captcha_detected")
                resolved = await wait_for_captcha_resolution(page)
                if not resolved:
                    await _log_action(session, queue_entry.id, job.id, "captcha_timeout")
                    return False

            # Try clicking "Apply" / "I'm interested" button if on job listing page
            await _click_apply_button(page)
            await page.wait_for_timeout(2000)

            # Analyze the page
            analysis = await analyze_page(page, profile, job.cover_letter or "", known_answers)
            await _log_action(session, queue_entry.id, job.id, "analyze", {
                "page_type": analysis.get("page_type"),
                "fields_count": len(analysis.get("fields", [])),
                "unknown_count": len(analysis.get("unknown_questions", [])),
            })

            page_type = analysis.get("page_type", "unknown")

            # Handle special page types
            if page_type == "expired":
                logger.info("Job expired/closed: %s", target_url)
                await _log_action(session, queue_entry.id, job.id, "expired")
                return False

            if page_type == "login_required":
                logger.info("Login required for %s — skipping", target_url)
                await _log_action(session, queue_entry.id, job.id, "login_required")
                return False

            if page_type == "captcha":
                await _update_status(queue_entry.id, ApplyStatus.WAITING_CAPTCHA)
                resolved = await wait_for_captcha_resolution(page)
                if not resolved:
                    return False
                # Re-analyze after CAPTCHA
                analysis = await analyze_page(page, profile, job.cover_letter or "", known_answers)

            if page_type == "redirect":
                # Follow redirect and re-analyze
                await page.wait_for_timeout(3000)
                analysis = await analyze_page(page, profile, job.cover_letter or "", known_answers)

            # Handle unknown questions — ask user via Telegram
            unknown = analysis.get("unknown_questions", [])
            if unknown:
                await _handle_unknown_questions(session, queue_entry, job, unknown)

            # Fill the form
            success, fail = await fill_form(page, analysis)
            await _log_action(session, queue_entry.id, job.id, "fill_form", {
                "success": success, "fail": fail,
            })

            if success == 0 and len(analysis.get("fields", [])) > 0:
                logger.warning("No fields filled for %s", job.url)
                return False

            # Take pre-submit screenshot
            screenshot_path = f"/tmp/apply_{job.id}_pre.png"
            await page.screenshot(path=screenshot_path)

            # Submit
            submitted = await click_submit(page, analysis)
            if submitted:
                await page.wait_for_timeout(3000)
                # Post-submit screenshot
                await page.screenshot(path=f"/tmp/apply_{job.id}_post.png")
                await _log_action(session, queue_entry.id, job.id, "submitted")

                if _notify_func:
                    await _notify_func(
                        f"✅ Applied: {job.title} @ {job.company}\n{job.url}"
                    )
                return True
            else:
                await _log_action(session, queue_entry.id, job.id, "submit_failed")
                return False

        except Exception as e:
            logger.error("Error processing job %d: %s", job.id, e)
            await _log_action(session, queue_entry.id, job.id, "error", {"error": str(e)})
            return False

        finally:
            await page.close()


async def _handle_unknown_questions(session, queue_entry, job, unknown_questions):
    """Notify about unknown questions and wait for answers."""
    for q in unknown_questions:
        q_text = q.get("question_text", "")
        if not q_text:
            continue

        # Check if we already have an answer
        answer = await find_answer(session, q_text)
        if answer:
            continue

        # Ask via Telegram
        if _notify_func:
            options_str = ""
            if q.get("options"):
                options_str = "\nOptions: " + ", ".join(q["options"])
            await _notify_func(
                f"❓ New question for: {job.title} @ {job.company}\n\n"
                f"Q: {q_text}{options_str}\n\n"
                f"Reply with the answer."
            )

        await _update_status(queue_entry.id, ApplyStatus.WAITING_USER)
        # TODO: implement Telegram reply handler that saves to question_bank
        # For now, skip unknown questions
        logger.info("Unknown question (no answer): %s", q_text[:80])


async def _update_status(queue_id: int, status: ApplyStatus):
    async with async_session() as session:
        await session.execute(
            update(ApplyQueue)
            .where(ApplyQueue.id == queue_id)
            .values(status=status, updated_at=datetime.utcnow())
        )
        await session.commit()


async def run_apply_worker():
    """Main worker loop — process apply queue."""
    logger.info("Auto-apply worker started")

    while True:
        if not settings.apply_enabled:
            await asyncio.sleep(10)
            continue

        async with async_session() as session:
            # Get next pending job
            result = await session.execute(
                select(ApplyQueue)
                .where(ApplyQueue.status == ApplyStatus.PENDING)
                .order_by(ApplyQueue.priority.desc(), ApplyQueue.created_at)
                .limit(1)
            )
            queue_entry = result.scalar_one_or_none()

            if not queue_entry:
                await asyncio.sleep(10)
                continue

            # Get job details
            job_result = await session.execute(
                select(Job).where(Job.id == queue_entry.job_id)
            )
            job = job_result.scalar_one_or_none()
            if not job:
                queue_entry.status = ApplyStatus.FAILED
                queue_entry.error_message = "Job not found"
                await session.commit()
                continue

            # Skip jobs without direct apply URL
            if not job.apply_url or job.apply_url == "NOT_FOUND":
                queue_entry.status = ApplyStatus.SKIPPED
                queue_entry.error_message = "No direct apply URL"
                await session.commit()
                continue

            # Update status
            queue_entry.status = ApplyStatus.IN_PROGRESS
            queue_entry.started_at = datetime.utcnow()
            queue_entry.attempts += 1
            await session.commit()

        # Process outside of session
        try:
            success = await process_job(queue_entry, job)
        except Exception as e:
            logger.error("Worker error: %s", e)
            success = False

        # Update final status
        async with async_session() as session:
            new_status = ApplyStatus.COMPLETED if success else ApplyStatus.FAILED
            values = {
                "status": new_status,
                "completed_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            if not success and queue_entry.attempts < queue_entry.max_attempts:
                values["status"] = ApplyStatus.PENDING  # retry later

            await session.execute(
                update(ApplyQueue)
                .where(ApplyQueue.id == queue_entry.id)
                .values(**values)
            )

            if success:
                await session.execute(
                    update(Job)
                    .where(Job.id == job.id)
                    .values(status="applied", applied_at=datetime.utcnow())
                )

            await session.commit()

        # Delay between applications
        logger.info("Waiting %ds before next application...", settings.apply_delay_seconds)
        await asyncio.sleep(settings.apply_delay_seconds)
