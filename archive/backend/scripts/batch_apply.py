"""Batch auto-apply script.

Navigates to each job, takes a screenshot, fills form, submits.
Outputs structured JSON for each step so the caller (Claude) can review.
"""
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
os.environ["DISPLAY"] = ":99"

from sqlalchemy import select, text, update
from backend.applier.browser import BrowserManager
from backend.models.database import async_session
from backend.models.job import Job

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

PROFILE = {
    "full_name": "Dana Devlin",
    "email": "dana.devlin.80@outlook.com",
    "phone": "7737658628",
    "resume_path": "/home/projects/jobfinder/uploads/resumes/609338c4_Dana_Devlin_CV.pdf",
}


async def apply_greenhouse(page, job, profile):
    """Apply to a Greenhouse job board listing."""
    url = job.apply_url
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    # Greenhouse has standard form fields
    # First check if this is a job board page (needs click) or direct application
    apply_btn = page.locator('a:has-text("Apply for this job")').first
    try:
        if await apply_btn.is_visible(timeout=2000):
            await apply_btn.click()
            await page.wait_for_timeout(3000)
    except Exception:
        pass

    # Screenshot the form
    await page.screenshot(path=f"/tmp/apply_{job.id}_form.png", full_page=True)

    # Greenhouse standard selectors
    filled = 0
    failed = 0

    # Name fields
    for sel, val in [
        ("#first_name", profile["full_name"].split()[0]),
        ("#last_name", profile["full_name"].split()[-1]),
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                await el.fill(val, timeout=3000)
                filled += 1
        except Exception:
            failed += 1

    # Email
    try:
        el = page.locator("#email").first
        if await el.is_visible(timeout=2000):
            await el.fill(profile["email"], timeout=3000)
            filled += 1
    except Exception:
        failed += 1

    # Phone
    try:
        el = page.locator("#phone").first
        if await el.is_visible(timeout=2000):
            await el.fill(profile["phone"], timeout=3000)
            filled += 1
    except Exception:
        failed += 1

    # Resume upload
    try:
        # Greenhouse uses a hidden file input near "Attach" link
        file_input = page.locator('input[type="file"]').first
        if await file_input.count() > 0:
            await file_input.set_input_files(profile["resume_path"], timeout=5000)
            filled += 1
    except Exception:
        failed += 1

    # LinkedIn (optional, skip)
    # Location
    try:
        loc = page.locator('input[name="job_application[location]"]').first
        if await loc.is_visible(timeout=1000):
            await loc.fill("Remote, US", timeout=3000)
            filled += 1
    except Exception:
        pass

    await page.wait_for_timeout(1000)
    await page.screenshot(path=f"/tmp/apply_{job.id}_filled.png", full_page=True)

    return {"filled": filled, "failed": failed}


async def apply_lever(page, job, profile):
    """Apply to a Lever job listing."""
    url = job.apply_url
    # Ensure /apply at the end
    if not url.endswith("/apply"):
        if url.endswith("/"):
            url += "apply"
        else:
            url += "/apply"

    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    await page.screenshot(path=f"/tmp/apply_{job.id}_form.png", full_page=True)

    filled = 0
    failed = 0

    # Lever standard form fields
    for sel, val in [
        ('input[name="name"]', profile["full_name"]),
        ('input[name="email"]', profile["email"]),
        ('input[name="phone"]', profile["phone"]),
        ('input[name="org"]', ""),  # Current company - optional
        ('input[name="urls[LinkedIn]"]', ""),
    ]:
        if not val:
            continue
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                await el.fill(val, timeout=3000)
                filled += 1
        except Exception:
            failed += 1

    # Resume
    try:
        file_input = page.locator('input[type="file"]').first
        if await file_input.count() > 0:
            await file_input.set_input_files(profile["resume_path"], timeout=5000)
            filled += 1
            await page.wait_for_timeout(2000)
    except Exception:
        failed += 1

    await page.wait_for_timeout(1000)
    await page.screenshot(path=f"/tmp/apply_{job.id}_filled.png", full_page=True)

    return {"filled": filled, "failed": failed}


async def apply_generic(page, job, profile):
    """Generic form filler — uses heuristics to find and fill fields."""
    url = job.apply_url
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(4000)

    # Try to click Apply button if on listing page
    for sel in [
        'button:has-text("Apply")', 'a:has-text("Apply")',
        'button:has-text("Apply Now")', 'a:has-text("Apply Now")',
        'button:has-text("I\'m interested")', 'a:has-text("I\'m interested")',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                await page.wait_for_timeout(3000)
                break
        except Exception:
            continue

    await page.screenshot(path=f"/tmp/apply_{job.id}_form.png", full_page=True)

    filled = 0
    failed = 0

    # Find all visible input fields
    inputs = await page.locator('input:visible, textarea:visible, select:visible').all()

    for inp in inputs:
        try:
            inp_type = (await inp.get_attribute("type") or "text").lower()
            name = (await inp.get_attribute("name") or "").lower()
            inp_id = (await inp.get_attribute("id") or "").lower()
            placeholder = (await inp.get_attribute("placeholder") or "").lower()
            label_text = ""
            try:
                label_text = await inp.evaluate("""el => {
                    let l = el.closest('label');
                    if (l) return l.innerText;
                    if (el.id) {
                        let lb = document.querySelector('label[for="' + el.id + '"]');
                        if (lb) return lb.innerText;
                    }
                    return '';
                }""")
                label_text = label_text.lower()
            except Exception:
                pass

            combined = f"{name} {inp_id} {placeholder} {label_text}"

            if inp_type == "file":
                await inp.set_input_files(profile["resume_path"], timeout=5000)
                filled += 1
                continue

            if inp_type in ("hidden", "submit", "button", "checkbox", "radio"):
                continue

            # Match fields
            if any(x in combined for x in ["first_name", "firstname", "first name"]):
                await inp.fill(profile["full_name"].split()[0], timeout=3000)
                filled += 1
            elif any(x in combined for x in ["last_name", "lastname", "last name", "surname"]):
                await inp.fill(profile["full_name"].split()[-1], timeout=3000)
                filled += 1
            elif any(x in combined for x in ["full_name", "fullname", "full name", "your name"]):
                await inp.fill(profile["full_name"], timeout=3000)
                filled += 1
            elif "name" in combined and not any(x in combined for x in ["company", "org", "user"]):
                # Might be name field
                current = await inp.input_value()
                if not current:
                    await inp.fill(profile["full_name"], timeout=3000)
                    filled += 1
            elif "email" in combined:
                await inp.fill(profile["email"], timeout=3000)
                filled += 1
            elif any(x in combined for x in ["phone", "tel", "mobile"]):
                await inp.fill(profile["phone"], timeout=3000)
                filled += 1
            elif any(x in combined for x in ["location", "city", "address"]):
                await inp.fill("Remote, US", timeout=3000)
                filled += 1

        except Exception as e:
            failed += 1

    await page.wait_for_timeout(1000)
    await page.screenshot(path=f"/tmp/apply_{job.id}_filled.png", full_page=True)

    return {"filled": filled, "failed": failed}


async def try_submit(page, job_id):
    """Try to find and click the submit button."""
    for sel in [
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
        'input[type="submit"]',
        'button[type="submit"]',
        'button:has-text("Apply")',
        'button:has-text("Send Application")',
        '#submit_app',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                await btn.click(timeout=5000)
                await page.wait_for_timeout(5000)
                await page.screenshot(path=f"/tmp/apply_{job_id}_submitted.png", full_page=True)
                return True
        except Exception:
            continue
    return False


async def check_success(page):
    """Check if the application was submitted successfully."""
    text = (await page.inner_text("body")).lower()
    success_indicators = [
        "thank you", "thanks for applying", "application received",
        "application submitted", "successfully submitted", "we received your application",
        "thanks for your interest", "application has been submitted",
    ]
    for indicator in success_indicators:
        if indicator in text:
            return True
    return False


async def process_job(page, job):
    """Process a single job application."""
    url = (job.apply_url or "").lower()

    try:
        if "greenhouse" in url:
            result = await apply_greenhouse(page, job, PROFILE)
        elif "lever" in url:
            result = await apply_lever(page, job, PROFILE)
        else:
            result = await apply_generic(page, job, PROFILE)

        if result["filled"] == 0:
            return {"status": "no_fields", **result}

        # Try submit
        submitted = await try_submit(page, job.id)
        if not submitted:
            return {"status": "no_submit_button", **result}

        # Check success
        success = await check_success(page)
        return {"status": "success" if success else "submitted_unknown", **result}

    except Exception as e:
        await page.screenshot(path=f"/tmp/apply_{job.id}_error.png", full_page=True)
        return {"status": "error", "error": str(e)[:200]}


async def main():
    # Get jobs with apply URLs
    async with async_session() as session:
        result = await session.execute(text("""
            SELECT id, title, company, apply_url
            FROM jobs
            WHERE apply_url IS NOT NULL
            AND apply_url != 'NOT_FOUND'
            ORDER BY id
        """))
        jobs_data = result.fetchall()

    print(f"Total jobs with apply URLs: {len(jobs_data)}")

    # Reset browser
    if BrowserManager._instance:
        try:
            await BrowserManager._instance.close()
        except Exception:
            pass
        BrowserManager._instance = None
        BrowserManager._browser = None
        BrowserManager._context = None

    bm = await BrowserManager.get_instance()

    results = {"success": 0, "failed": 0, "skipped": 0}

    for job_id, title, company, apply_url in jobs_data:
        # Get full job object
        async with async_session() as session:
            job_result = await session.execute(select(Job).where(Job.id == job_id))
            job = job_result.scalar_one_or_none()
            if not job:
                continue

        page = await bm.new_page()
        try:
            print(f"\n--- #{job.id} {title[:50]} @ {company} ---")
            result = await process_job(page, job)
            print(f"Result: {result}")

            if result["status"] in ("success", "submitted_unknown"):
                results["success"] += 1
                # Mark as applied in DB
                async with async_session() as session:
                    await session.execute(
                        text("UPDATE jobs SET apply_url = apply_url || ' [APPLIED]' WHERE id = :id"),
                        {"id": job.id},
                    )
                    await session.commit()
            elif result["status"] == "error":
                results["failed"] += 1
            else:
                results["skipped"] += 1

        except Exception as e:
            print(f"Error: {e}")
            results["failed"] += 1
        finally:
            await page.close()

        # Delay between applications
        await asyncio.sleep(5)

    print(f"\n=== FINAL: {results} ===")


if __name__ == "__main__":
    asyncio.run(main())
