"""Smart auto-apply: extracts form structure, matches answers, fills, submits.

Each job is processed in stages:
  1. navigate & extract form fields
  2. match fields to known answers
  3. fill all fields
  4. submit
"""
import asyncio
import json
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
os.environ["DISPLAY"] = ":99"

from sqlalchemy import select, text
from backend.applier.browser import BrowserManager
from backend.models.database import async_session
from backend.models.job import Job

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
    "desired_salary": "",
    "country": "United States",
    "gender": "Decline to self identify",
    "veteran_status": "I am not a protected veteran",
    "disability_status": "I do not want to answer",
}

# Standard answer bank for common questions
ANSWER_BANK = {
    # Work authorization
    r"authorized.*work|work.*authorization|legally.*work|eligible.*work|right to work|employment eligibility": "Yes",
    r"require.*sponsor|visa.*sponsor|need.*sponsor": "No",
    r"require.*visa": "No",
    # Availability
    r"how soon.*start|start date|earliest.*start|when.*start|available.*start": "Immediately",
    r"available.*work.*hours|available.*schedule|work.*schedule": "Yes",
    r"available.*remote|work.*remote|comfortable.*remote": "Yes",
    # Experience
    r"years.*experience|experience.*years|how many years|how long.*experience": "5",
    r"previous.*experience|relevant.*experience|do you have.*experience": "Yes",
    # Education
    r"highest.*education|education.*level|degree": "Bachelor's Degree",
    # Location
    r"where.*located|current.*location|city|state": "Chicago, IL",
    r"time.?zone": "Eastern",
    r"willing.*relocate|open.*relocation": "No",
    # Salary
    r"salary.*expect|desired.*salary|compensation.*expect|salary.*requirement|pay.*range|desired.*pay": "Negotiable",
    # General Yes
    r"18.*years.*old|over.*18|at least 18": "Yes",
    r"background.*check|consent.*background": "Yes",
    r"acknowledge|agree|confirm|consent": "Yes",
    r"have.*computer|reliable.*internet|own.*computer": "Yes",
    # Referral
    r"hear about|how.*find|source|referr": "Job Board",
    # Customer service specific
    r"define.*customer.*experience|excellent.*customer": "An excellent customer experience means anticipating needs, resolving issues efficiently, communicating clearly, and leaving every interaction better than it started. It involves empathy, active listening, and a genuine desire to help.",
    r"experience.*shopify|experience.*zendesk|experience.*tools": "Yes, all of the above",
    r"accommodat|disability.*interview": "No",
    r"criminal.*check|credit.*check": "Yes",
    # Additional common
    r"additional.*information|anything.*else|comments": "Thank you for considering my application. I look forward to the opportunity to contribute to your team.",
    r"portfolio|github|website": "",
    r"current.*company|current.*employer|organization": "",
    # Cover letter / why interested — generic but enthusiastic
    r"cover.?letter|why.*interested|why.*apply|why.*join|tell.*about.*yourself|why.*want|what excites|what draws|what motivates": "I am excited about this opportunity because it aligns perfectly with my professional experience and career goals. I bring 5 years of relevant experience and am passionate about contributing to a team that values excellence and innovation. I am confident my skills and dedication would make me a strong addition to your team.",
}


async def extract_form_fields(page) -> list[dict]:
    """Extract all form fields with their labels and options."""
    fields = await page.evaluate(r"""() => {
        const results = [];

        // Helper to get label text for an element
        function getLabel(el) {
            // Check for wrapping label
            let label = el.closest('label');
            if (label) return label.innerText.trim();

            // Check for label[for=id]
            if (el.id) {
                let lb = document.querySelector('label[for="' + el.id + '"]');
                if (lb) return lb.innerText.trim();
            }

            // Check previous sibling or parent text
            let prev = el.previousElementSibling;
            if (prev && (prev.tagName === 'LABEL' || prev.tagName === 'SPAN' || prev.tagName === 'DIV')) {
                return prev.innerText.trim();
            }

            // Check parent's text content (for custom question cards)
            let parent = el.parentElement;
            for (let i = 0; i < 5 && parent; i++) {
                let text = '';
                for (let child of parent.childNodes) {
                    if (child.nodeType === 3) text += child.textContent;
                    if (child.tagName === 'LABEL' || child.tagName === 'SPAN' || child.tagName === 'P' || child.tagName === 'DIV') {
                        if (!child.querySelector('input, select, textarea')) {
                            text += child.innerText;
                        }
                    }
                }
                text = text.trim();
                if (text && text.length > 2 && text.length < 200) return text;
                parent = parent.parentElement;
            }

            return el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
        }

        // Find all inputs, selects, textareas
        const elements = document.querySelectorAll('input, select, textarea');

        for (const el of elements) {
            if (!el.offsetParent && el.type !== 'file' && el.type !== 'hidden') continue;
            if (el.type === 'hidden') continue;

            const field = {
                tag: el.tagName.toLowerCase(),
                type: el.type || 'text',
                name: el.name || '',
                id: el.id || '',
                value: el.value || '',
                placeholder: el.placeholder || '',
                required: el.required || el.getAttribute('aria-required') === 'true',
                label: getLabel(el),
            };

            // CSS selector for this element
            if (el.id) {
                field.selector = '#' + CSS.escape(el.id);
            } else if (el.name) {
                field.selector = el.tagName.toLowerCase() + '[name="' + el.name + '"]';
            } else {
                // Use nth-of-type
                const parent = el.parentElement;
                const siblings = parent ? Array.from(parent.querySelectorAll(el.tagName)) : [];
                const idx = siblings.indexOf(el);
                field.selector = '';  // skip - hard to target
            }

            // For select, get options
            if (el.tagName === 'SELECT') {
                field.options = Array.from(el.options).map(o => ({
                    text: o.text.trim(),
                    value: o.value,
                })).filter(o => o.value);
            }

            // For radio/checkbox groups
            if (el.type === 'radio' || el.type === 'checkbox') {
                field.radioValue = el.value;
                field.checked = el.checked;
            }

            results.push(field);
        }

        // Also find Lever-style custom question cards
        const cards = document.querySelectorAll('.application-question, .custom-question');
        for (const card of cards) {
            const textarea = card.querySelector('textarea');
            const input = card.querySelector('input:not([type="hidden"])');
            const select = card.querySelector('select');
            const el = textarea || input || select;
            if (!el) continue;

            const label = card.querySelector('label, .question-text, .question-label');
            if (label) {
                // Update the existing field's label
                for (const f of results) {
                    if (f.name === el.name || f.id === el.id) {
                        if (!f.label || f.label.length < label.innerText.trim().length) {
                            f.label = label.innerText.trim();
                        }
                    }
                }
            }
        }

        // Find radio button groups (Lever uses these for multiple choice)
        const radioGroups = {};
        document.querySelectorAll('input[type="radio"]').forEach(r => {
            const name = r.name;
            if (!radioGroups[name]) {
                radioGroups[name] = {
                    tag: 'radio-group',
                    name: name,
                    label: getLabel(r),
                    options: [],
                    selector: '',
                };
            }
            const labelEl = r.nextElementSibling || r.parentElement;
            radioGroups[name].options.push({
                text: labelEl ? labelEl.innerText.trim() : r.value,
                value: r.value,
            });
        });

        // Add radio groups to results (replace individual radios)
        const radioNames = new Set(Object.keys(radioGroups));
        const filtered = results.filter(f => !(f.type === 'radio' && radioNames.has(f.name)));
        filtered.push(...Object.values(radioGroups));

        return filtered;
    }""")

    return fields


def match_answer(label: str, field: dict, profile: dict) -> str | None:
    """Match a form field to the right answer based on its label."""
    label_lower = label.lower().strip()

    if not label_lower:
        return None

    # File upload — check type FIRST before label matching
    if field.get("type") == "file":
        return profile["resume_path"]

    # Direct profile field matches
    profile_patterns = {
        r"first.?name": profile["first_name"],
        r"last.?name|surname|family.?name": profile["last_name"],
        r"full.?name|your.?name|^name\b": profile["full_name"],
        r"\bemail\b": profile["email"],
        r"\bphone\b|mobile|telephone|tel\b": profile["phone"],
        r"linkedin": profile["linkedin"],
        r"country": profile["country"],
        r"location|city": profile["location"],
    }

    for pattern, value in profile_patterns.items():
        if re.search(pattern, label_lower):
            return value

    # Answer bank matches
    for pattern, answer in ANSWER_BANK.items():
        if re.search(pattern, label_lower):
            if answer == "":  # Needs per-job generation
                return None
            return answer

    # For select/radio with options - try to pick the best
    options = field.get("options", [])
    if options:
        option_texts = [o.get("text", "").lower() for o in options]

        # Try common positive answers
        for preferred in ["yes", "united states", "immediately", "decline", "prefer not"]:
            for i, opt in enumerate(option_texts):
                if preferred in opt:
                    return options[i]["text"]

    # For checkboxes that say "agree" or "acknowledge"
    if field.get("type") == "checkbox":
        if any(x in label_lower for x in ["agree", "acknowledge", "consent", "confirm", "accept"]):
            return "true"

    return None


async def fill_and_submit(page, job) -> dict:
    """Extract fields, match answers, fill, and submit."""
    result = {"job_id": job.id, "title": job.title, "company": job.company}

    # Extract form structure
    fields = await extract_form_fields(page)
    result["total_fields"] = len(fields)

    # Filter to actionable fields
    actionable = [f for f in fields if f.get("selector") and f.get("tag") != "radio-group" or f.get("tag") == "radio-group"]

    filled = 0
    skipped = 0
    unanswered = []

    for field in actionable:
        label = field.get("label", "")
        selector = field.get("selector", "")
        ftype = field.get("type", "text")
        tag = field.get("tag", "")

        # Skip if already filled
        if field.get("value") and ftype not in ("select-one", "radio"):
            continue

        # Skip submit buttons, hidden fields
        if ftype in ("submit", "button", "hidden", "search"):
            continue

        answer = match_answer(label, field, PROFILE)

        if answer is None:
            if field.get("required") and label:
                unanswered.append({"label": label[:100], "type": ftype})
            skipped += 1
            continue

        try:
            if ftype == "file":
                el = page.locator(selector).first if selector else page.locator('input[type="file"]').first
                await el.set_input_files(answer, timeout=5000)
                filled += 1
                await page.wait_for_timeout(2000)
            elif ftype == "checkbox":
                el = page.locator(selector).first
                if answer.lower() in ("true", "yes"):
                    await el.check(timeout=3000)
                filled += 1
            elif tag == "radio-group":
                # Click the right radio option
                for opt in field.get("options", []):
                    if opt["text"].lower() == answer.lower():
                        radio = page.locator(f'input[name="{field["name"]}"][value="{opt["value"]}"]').first
                        await radio.click(timeout=3000)
                        filled += 1
                        break
            elif ftype == "select-one" or tag == "select":
                el = page.locator(selector).first
                try:
                    await el.select_option(label=answer, timeout=3000)
                except Exception:
                    # Try by value
                    for opt in field.get("options", []):
                        if answer.lower() in opt["text"].lower():
                            await el.select_option(value=opt["value"], timeout=3000)
                            break
                filled += 1
            else:
                el = page.locator(selector).first
                try:
                    await el.fill(answer, timeout=5000)
                except Exception:
                    await el.click(timeout=3000)
                    await page.keyboard.press("Control+a")
                    await page.keyboard.type(answer, delay=30)
                filled += 1

            await page.wait_for_timeout(200)
        except Exception as e:
            logger.warning(f"Failed to fill {label[:40]}: {str(e)[:60]}")
            skipped += 1

    result["filled"] = filled
    result["skipped"] = skipped
    result["unanswered_required"] = unanswered

    # Take pre-submit screenshot
    await page.screenshot(path=f"/tmp/apply_{job.id}_prefill.png", full_page=True)

    # Scroll to bottom before looking for submit button
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(1000)

    # Submit
    submitted = False
    for sel in [
        'button:has-text("Submit Application")',
        'button:has-text("Submit application")',
        'button:has-text("SUBMIT APPLICATION")',
        'a:has-text("SUBMIT APPLICATION")',
        'button:has-text("Submit")',
        '#submit_app',
        'input[type="submit"]',
        'button[type="submit"]',
        'input[value="Submit Application"]',
        'input[value="Submit"]',
        'button:has-text("Apply")',
        'button:has-text("Send Application")',
        'button:has-text("Complete Application")',
        '.postings-btn-submit',
        '.template-btn-submit',
        'button.postings-btn.template-btn-submit',
        '[data-qa="btn-submit"]',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1500):
                await btn.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)
                try:
                    await btn.click(timeout=5000)
                except Exception:
                    # Force click if something overlaps
                    await btn.click(force=True, timeout=5000)
                submitted = True
                break
        except Exception:
            continue

    if submitted:
        await page.wait_for_timeout(6000)
        body = (await page.inner_text("body")).lower()
        success_words = ["thank you", "thanks", "received", "submitted", "successfully"]
        result["success"] = any(w in body for w in success_words)
        result["status"] = "success" if result["success"] else "submitted_unclear"
    else:
        result["status"] = "no_submit_button"
        result["success"] = False

    await page.screenshot(path=f"/tmp/apply_{job.id}_result.png", full_page=True)
    return result


async def process_one(bm, job) -> dict:
    """Full pipeline for one job."""
    page = await bm.new_page()
    try:
        url = job.apply_url

        # For Lever - go directly to /apply
        if "lever.co" in url and "/apply" not in url:
            url = url.rstrip("/") + "/apply"

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        # Try clicking Apply button if on listing page (Ashby, Lever, company sites)
        apply_selectors = [
            'a:has-text("Apply for this job")',
            'button:has-text("Apply for this job")',
            'a:has-text("Apply Now")',
            'button:has-text("Apply Now")',
            'a:has-text("Apply for this position")',
            'button:has-text("Apply")',
            'a:has-text("Apply")',
            'a[href*="/apply"]',
            '[data-qa="btn-apply"]',
            '.postings-btn-apply',
            'a.ashby-apply-button',
            '.ashby-job-posting-brief-info a',
        ]
        for sel in apply_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    await page.wait_for_timeout(4000)
                    # Check if we navigated to a form page
                    has_form = await page.locator("form input, form select, form textarea, input[type='file']").count()
                    if has_form > 0:
                        break
            except Exception:
                continue

        # For Ashby — if still no form, try scrolling down (form might be below)
        if "ashbyhq.com" in url:
            has_form = await page.locator("form input, form textarea").count()
            if has_form == 0:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000)

        # Close popups/modals
        for sel in [
            'button:has-text("Accept")', 'button:has-text("Close")',
            '#onetrust-accept-btn-handler', 'button:has-text("I Accept")',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    await page.wait_for_timeout(500)
            except Exception:
                continue

        # Check if job is expired/closed
        body_text = (await page.inner_text("body")).lower()
        expired_phrases = [
            "no longer available", "position has been filled", "no longer accepting",
            "this job has been closed", "expired", "this position is closed",
            "job is no longer", "posting has been removed", "no longer open",
        ]
        if any(p in body_text for p in expired_phrases):
            return {"job_id": job.id, "title": job.title, "company": job.company,
                    "status": "expired", "success": False}

        return await fill_and_submit(page, job)

    except Exception as e:
        return {"job_id": job.id, "status": "error", "error": str(e)[:200], "success": False}
    finally:
        await page.close()


# Platforms that require account creation — skip in auto mode
SKIP_PLATFORMS = ["myworkdayjobs.com", "icims.com", "taleo.net", "ultipro.com", "governmentjobs.com", "usajobs.gov"]


async def record_result(job_id: int, status: str, error: str = ""):
    """Save apply result to database."""
    async with async_session() as session:
        exists = await session.execute(
            text("SELECT id FROM apply_results WHERE job_id = :j"), {"j": job_id}
        )
        if not exists.first():
            await session.execute(
                text("INSERT INTO apply_results (job_id, status, error) VALUES (:j, :s, :e)"),
                {"j": job_id, "s": status, "e": error},
            )
            await session.commit()


async def run_batch(limit=None, platform_filter=None, skip_account_platforms=True):
    """Process a batch of jobs with auto-recording results."""
    async with async_session() as session:
        query = """
            SELECT id FROM jobs
            WHERE apply_url IS NOT NULL
            AND apply_url != 'NOT_FOUND'
            AND apply_url NOT LIKE '%indeed.com%'
            AND apply_url NOT LIKE '%linkedin.com%'
            AND id NOT IN (SELECT job_id FROM apply_results)
        """
        if skip_account_platforms:
            for sp in SKIP_PLATFORMS:
                query += f" AND apply_url NOT LIKE '%{sp}%'"
        if platform_filter:
            query += f" AND apply_url LIKE '%{platform_filter}%'"
        query += " ORDER BY id LIMIT :limit"

        result = await session.execute(text(query), {"limit": limit or 9999})
        job_ids = [r[0] for r in result]

    print(f"Processing {len(job_ids)} jobs...")

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

    stats = {"success": 0, "submitted_unclear": 0, "expired": 0, "fail": 0, "error": 0}

    for i, job_id in enumerate(job_ids):
        async with async_session() as session:
            job_result = await session.execute(select(Job).where(Job.id == job_id))
            job = job_result.scalar_one_or_none()
            if not job:
                continue

        print(f"\n[{i+1}/{len(job_ids)}] {'='*50}")
        print(f"#{job.id} {job.title[:50]} @ {job.company}")
        print(f"URL: {job.apply_url[:80]}")

        result = await process_one(bm, job)

        status = result.get("status", "unknown")
        filled = result.get("filled", 0)
        print(f"Status: {status} | Filled: {filled} | Skipped: {result.get('skipped', 0)}")

        if result.get("unanswered_required"):
            print(f"Unanswered: {[q['label'][:50] for q in result['unanswered_required']]}")

        # Auto-record results
        if status == "expired":
            await record_result(job_id, "expired", "auto_apply_expired")
            stats["expired"] += 1
        elif result.get("success"):
            await record_result(job_id, "submitted", "auto_apply_success")
            stats["success"] += 1
        elif status == "submitted_unclear" and filled >= 3:
            # Filled 3+ fields and hit submit — likely went through
            await record_result(job_id, "submitted", "auto_apply_unclear")
            stats["submitted_unclear"] += 1
        elif status == "error":
            stats["error"] += 1
        else:
            stats["fail"] += 1

        # Print running totals every 10 jobs
        if (i + 1) % 10 == 0:
            print(f"\n--- Progress: {stats} ---\n")

        await asyncio.sleep(2)

    print(f"\n{'='*60}")
    print(f"FINAL RESULTS: {stats}")
    total_submitted = stats["success"] + stats["submitted_unclear"]
    print(f"Total submitted: {total_submitted} / {len(job_ids)}")
    return stats


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--platform", type=str, default=None)
    parser.add_argument("--include-account", action="store_true",
                        help="Include platforms requiring account (Workday, iCIMS, etc.)")
    args = parser.parse_args()

    asyncio.run(run_batch(
        limit=args.limit,
        platform_filter=args.platform,
        skip_account_platforms=not args.include_account,
    ))
