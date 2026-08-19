"""Semi-automatic apply: fills forms, user submits via noVNC + Telegram.

Flow per job:
  1. Navigate to apply page
  2. Auto-fill all detected fields
  3. Send unknown questions to Telegram (user answers via buttons/reply)
  4. Send screenshot + "Ready" notification to Telegram
  5. User reviews via noVNC, fills missing, solves CAPTCHA, clicks Submit
  6. User clicks "Submitted" / "Skip" / "Stop" button in Telegram
  7. Next job

Usage:
  python3 backend/scripts/semi_auto_apply.py [--start-from ID] [--platform FILTER]
"""
import asyncio
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
os.environ["DISPLAY"] = ":99"

from sqlalchemy import select, text
from backend.applier.browser import BrowserManager
from backend.applier.question_bank import find_answer, save_answer
from backend.models.database import async_session
from backend.models.job import Job
from bot.apply_bot import (
    get_answer,
    poll_updates,
    send_form_ready,
    send_message,
    send_question,
    send_status,
    wait_for_user_action,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

PROFILE = {
    "full_name": "Dana Devlin",
    "first_name": "Dana",
    "last_name": "Devlin",
    "email": "dana.devlin.80@outlook.com",
    "phone": "7737658628",
    "location": "Chicago, IL",
    "linkedin": "https://linkedin.com/in/dana-devlin",
    "resume_path": "/home/projects/jobfinder/uploads/resumes/609338c4_Dana_Devlin_CV.pdf",
    "years_experience": "5",
    "work_authorization": "Yes",
    "available_start": "Immediately",
    "country": "United States",
}

ANSWER_BANK = {
    r"authorized.*work|work.*authorization|legally.*work|eligible.*work|right to work": "Yes",
    r"require.*sponsor|visa.*sponsor|need.*sponsor": "No",
    r"how soon.*start|start date|earliest.*start|when.*start|available.*start": "Immediately",
    r"available.*work.*hours|available.*schedule|work.*schedule": "Yes",
    r"available.*remote|work.*remote|comfortable.*remote": "Yes",
    r"years.*experience|experience.*years|how many years": "5",
    r"previous.*experience|relevant.*experience|do you have.*experience": "Yes",
    r"highest.*education|education.*level|degree": "Bachelor's Degree",
    r"where.*located|current.*location": "Chicago, IL",
    r"time.?zone": "Eastern",
    r"willing.*relocate": "No",
    r"salary.*expect|desired.*salary|compensation.*expect|salary.*requirement|desired.*pay": "Negotiable",
    r"18.*years.*old|over.*18|at least 18": "Yes",
    r"background.*check|consent.*background|criminal.*check": "Yes",
    r"acknowledge|agree|confirm|consent": "Yes",
    r"have.*computer|reliable.*internet": "Yes",
    r"hear about|how.*find|source|referr": "Job Board",
    r"cover.?letter|why.*interested|why.*apply|why.*join|why.*want": (
        "I am excited about this opportunity because it aligns perfectly with my professional "
        "experience and career goals. I bring 5 years of relevant experience and am passionate "
        "about contributing to a team that values excellence and innovation."
    ),
    r"define.*customer.*experience|excellent.*customer": (
        "An excellent customer experience means anticipating needs, resolving issues efficiently, "
        "communicating clearly, and leaving every interaction better than it started."
    ),
    r"accommodat|disability.*interview": "No",
    r"additional.*information|anything.*else": "Thank you for considering my application.",
}


def match_answer(label: str, field: dict) -> str | None:
    """Match form field to answer."""
    label_lower = label.lower().strip()
    if not label_lower:
        return None

    if field.get("type") == "file":
        return PROFILE["resume_path"]

    profile_patterns = {
        r"first.?name": PROFILE["first_name"],
        r"last.?name|surname": PROFILE["last_name"],
        r"full.?name|your.?name|^name\b": PROFILE["full_name"],
        r"\bemail\b": PROFILE["email"],
        r"\bphone\b|mobile|tel\b": PROFILE["phone"],
        r"linkedin": PROFILE["linkedin"],
        r"country": PROFILE["country"],
        r"location|city": PROFILE["location"],
    }
    for pattern, value in profile_patterns.items():
        if re.search(pattern, label_lower):
            return value

    for pattern, answer in ANSWER_BANK.items():
        if re.search(pattern, label_lower):
            return answer

    options = field.get("options", [])
    if options:
        for preferred in ["yes", "united states", "immediately", "decline", "prefer not"]:
            for opt in options:
                if preferred in opt.get("text", "").lower():
                    return opt["text"]

    if field.get("type") == "checkbox":
        if any(x in label_lower for x in ["agree", "acknowledge", "consent", "accept"]):
            return "true"

    return None


async def extract_fields(page) -> list[dict]:
    """Extract visible form fields."""
    return await page.evaluate(r"""() => {
        const results = [];
        function getLabel(el) {
            let label = el.closest('label');
            if (label) return label.innerText.trim();
            if (el.id) {
                let lb = document.querySelector('label[for="' + el.id + '"]');
                if (lb) return lb.innerText.trim();
            }
            let prev = el.previousElementSibling;
            if (prev && ['LABEL','SPAN','DIV','P'].includes(prev.tagName)) return prev.innerText.trim();
            let parent = el.parentElement;
            for (let i = 0; i < 3 && parent; i++) {
                let text = '';
                for (let child of parent.childNodes) {
                    if (child.nodeType === 3) text += child.textContent;
                    if (['LABEL','SPAN','P','DIV'].includes(child.tagName) && !child.querySelector('input,select,textarea')) {
                        text += child.innerText;
                    }
                }
                text = text.trim();
                if (text && text.length > 2 && text.length < 200) return text;
                parent = parent.parentElement;
            }
            return el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
        }
        const elements = document.querySelectorAll('input, select, textarea');
        for (const el of elements) {
            if (!el.offsetParent && el.type !== 'file' && el.type !== 'hidden') continue;
            if (el.type === 'hidden') continue;
            const field = {
                tag: el.tagName.toLowerCase(), type: el.type || 'text',
                name: el.name || '', id: el.id || '', value: el.value || '',
                placeholder: el.placeholder || '',
                required: el.required || el.getAttribute('aria-required') === 'true',
                label: getLabel(el),
            };
            if (el.id) field.selector = '#' + CSS.escape(el.id);
            else if (el.name) field.selector = el.tagName.toLowerCase() + '[name="' + el.name + '"]';
            else field.selector = '';
            if (el.tagName === 'SELECT') {
                field.options = Array.from(el.options).map(o => ({text: o.text.trim(), value: o.value})).filter(o => o.value);
            }
            results.push(field);
        }
        return results;
    }""")


async def auto_fill(page, job_id: int) -> dict:
    """Fill all detected fields. Returns stats."""
    fields = await extract_fields(page)
    filled = 0
    skipped = 0
    unanswered = []

    # Check question bank for saved answers
    async with async_session() as session:
        for field in fields:
            label = field.get("label", "")
            if label:
                saved = await find_answer(session, label)
                if saved:
                    field["_saved_answer"] = saved

    for field in fields:
        sel = field.get("selector", "")
        if not sel:
            continue
        ftype = field.get("type", "text")
        if ftype in ("submit", "button", "hidden", "search"):
            continue
        if field.get("value") and ftype != "select-one":
            continue

        label = field.get("label", "")

        # Priority: saved answer from bank > pattern match
        answer = field.get("_saved_answer") or match_answer(label, field)

        if answer is None:
            if field.get("required") and label and len(label) > 3:
                unanswered.append({
                    "label": label[:100],
                    "type": ftype,
                    "selector": sel,
                    "options": [o["text"] for o in field.get("options", [])],
                })
            skipped += 1
            continue

        try:
            if ftype == "file":
                await page.locator(sel).first.set_input_files(answer, timeout=5000)
                await page.wait_for_timeout(2000)
            elif ftype == "checkbox":
                if answer.lower() in ("true", "yes"):
                    await page.locator(sel).first.check(timeout=3000)
            elif ftype in ("select-one",):
                el = page.locator(sel).first
                try:
                    await el.select_option(label=answer, timeout=3000)
                except Exception:
                    for opt in field.get("options", []):
                        if answer.lower() in opt["text"].lower():
                            await el.select_option(value=opt["value"], timeout=3000)
                            break
            else:
                el = page.locator(sel).first
                try:
                    await el.fill(answer, timeout=5000)
                except Exception:
                    await el.click(timeout=3000)
                    await page.keyboard.press("Control+a")
                    await page.keyboard.type(answer, delay=30)
            filled += 1
            await page.wait_for_timeout(200)
        except Exception:
            skipped += 1

    return {"filled": filled, "skipped": skipped, "unanswered": unanswered}


async def process_job(bm, job, stats: dict) -> str:
    """Navigate, fill, send Telegram notification, wait for user."""
    page = await bm.new_page()

    try:
        url = job.apply_url
        if "lever.co" in url and "/apply" not in url:
            url = url.rstrip("/") + "/apply"

        logger.info("\n%s", "=" * 60)
        logger.info("#%d %s @ %s", job.id, job.title, job.company)
        logger.info("URL: %s", url)

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        # Check expired
        body = (await page.inner_text("body")).lower()
        expired_phrases = [
            "no longer available", "position has been filled", "no longer accepting",
            "this job has been closed", "this position is closed",
            "job is no longer", "no longer open",
        ]
        if any(p in body for p in expired_phrases):
            logger.info("EXPIRED — skipping")
            await page.close()
            return "expired"

        # Click Apply button if needed
        for sel in [
            'button:has-text("Apply for this job")', 'a:has-text("Apply for this job")',
            'button:has-text("Apply")', 'a:has-text("Apply")',
            'button:has-text("Apply Now")', 'a:has-text("Apply Now")',
            'button:has-text("I\'m interested")',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1000):
                    await btn.click()
                    logger.info("Clicked: %s", sel)
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                continue

        # Close popups
        for sel in ['button:has-text("Accept")', '#onetrust-accept-btn-handler',
                     'button:has-text("Close")', 'button:has-text("Allow")']:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    await page.wait_for_timeout(500)
            except Exception:
                continue

        # Auto-fill
        result = await auto_fill(page, job.id)
        logger.info("Filled: %d | Skipped: %d | Unanswered: %d",
                     result['filled'], result['skipped'], len(result['unanswered']))

        # If no form found at all — skip automatically
        if result["filled"] == 0 and len(result["unanswered"]) == 0:
            logger.info("No form found — auto-skipping")
            await send_message(
                f"⏭ <b>No form found</b>\n#{job.id} {job.title} @ {job.company}\nAuto-skipping..."
            )
            await page.close()
            return "no_form"

        # Send unknown questions to Telegram
        for q in result["unanswered"]:
            # Send to Telegram and wait for answer
            await send_question(
                question_text=q["label"],
                job_title=job.title,
                company=job.company,
                options=q["options"] if q["options"] else None,
                job_id=job.id,
            )

        # Take screenshot
        screenshot = await page.screenshot(full_page=False)

        # Send notification to Telegram
        await send_form_ready(
            job_id=job.id,
            job_title=job.title,
            company=job.company,
            filled=result["filled"],
            unanswered=[q["label"] for q in result["unanswered"]],
            screenshot=screenshot,
        )

        # Wait for user action via Telegram buttons
        logger.info("Waiting for user action via Telegram...")
        action = await wait_for_user_action(timeout=180)

        if action == "submitted":
            logger.info("User confirmed submission")
            stats["submitted"] = stats.get("submitted", 0) + 1

            # Drain answer queue — save user answers to QuestionBank
            from bot.apply_bot import _answer_queue
            while not _answer_queue.empty():
                ans = _answer_queue.get_nowait()
                if ans.get("question_text") == "_skipped":
                    continue
                async with async_session() as session:
                    await save_answer(session, ans["question_text"], ans["answer_text"])
                logger.info("Saved answer: %s -> %s", ans["question_text"][:40], ans["answer_text"][:40])

            return "submitted"

        elif action == "skip_job":
            logger.info("User skipped job")
            return "skipped"

        elif action == "stop":
            logger.info("User stopped")
            return "stop"

        else:
            logger.info("Timeout — moving to next")
            return "timeout"

    except Exception as e:
        logger.error("Error: %s", e)
        await send_message(f"❌ Error on #{job.id}: {str(e)[:100]}")
        return "error"

    finally:
        try:
            await page.close()
        except Exception:
            pass


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-from", type=int, default=0)
    parser.add_argument("--platform", type=str, default=None)
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()

    # Get jobs
    async with async_session() as session:
        query = """
            SELECT id FROM jobs
            WHERE apply_url IS NOT NULL AND apply_url != 'NOT_FOUND'
            AND id > :start_from
            ORDER BY id LIMIT :limit
        """
        params = {"start_from": args.start_from, "limit": args.limit}
        if args.platform:
            query = query.replace("ORDER BY", f"AND apply_url LIKE :platform ORDER BY")
            params["platform"] = f"%{args.platform}%"

        result = await session.execute(text(query), params)
        job_ids = [r[0] for r in result]

    logger.info("Semi-auto apply: %d jobs", len(job_ids))
    logger.info("Send /start to @autocaptcherbot in Telegram")

    # Start Telegram polling in background
    poll_task = asyncio.create_task(poll_updates())

    # Wait for chat_id
    await send_message("🚀 Semi-auto apply started! Processing %d jobs." % len(job_ids))
    await asyncio.sleep(2)

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

    stats = {"submitted": 0, "expired": 0, "error": 0, "skipped": 0}

    for job_id in job_ids:
        async with async_session() as session:
            job_result = await session.execute(select(Job).where(Job.id == job_id))
            job = job_result.scalar_one_or_none()
            if not job:
                continue

        status = await process_job(bm, job, stats)

        if status == "expired":
            stats["expired"] += 1
        elif status == "no_form":
            stats["no_form"] = stats.get("no_form", 0) + 1
        elif status == "error":
            stats["error"] += 1
        elif status == "skipped":
            stats["skipped"] += 1
        elif status == "stop":
            break

    # Final stats
    await send_status(stats)
    logger.info("DONE: %s", stats)

    poll_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
