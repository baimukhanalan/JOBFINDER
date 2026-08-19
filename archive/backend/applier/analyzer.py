"""Rule-based form analyzer — no API key needed.

Extracts form fields from page DOM and maps them to user profile data
using pattern matching on labels, names, placeholders, and aria-labels.
"""
import json
import logging
import re

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# Field patterns: (regex_pattern, profile_key_or_value, action)
# Checked top-to-bottom, first match wins
FIELD_PATTERNS = [
    # Name fields
    (r"(?i)(full.?name|your.?name|^name$|applicant.?name|candidate.?name|legal.?name)", "full_name", "fill"),
    (r"(?i)(first.?name|given.?name|fname)", "_first_name", "fill"),
    (r"(?i)(last.?name|family.?name|surname|lname)", "_last_name", "fill"),

    # Contact
    (r"(?i)(e.?mail|email.?address|your.?email)", "email", "fill"),
    (r"(?i)(phone|mobile|cell|telephone|tel$)", "phone", "fill"),

    # Location
    (r"(?i)(city|location|where.?located|current.?location|address)", "_location", "fill"),
    (r"(?i)(state|province)", "_state", "fill"),
    (r"(?i)(zip|postal|postcode)", "_zip", "fill"),
    (r"(?i)(country)", "_country", "fill"),

    # Resume/CV upload
    (r"(?i)(resume|cv|curriculum)", "_resume", "upload"),

    # Cover letter
    (r"(?i)(cover.?letter|letter.?of.?interest)", "_cover_letter", "fill"),

    # LinkedIn
    (r"(?i)(linkedin)", "_linkedin", "fill"),

    # Experience
    (r"(?i)(years?.?of?.?experience|experience.?years|how.?many.?years)", "_experience", "fill"),

    # Salary
    (r"(?i)(salary|compensation|pay.?expect|desired.?pay)", "_salary", "fill"),

    # Work authorization
    (r"(?i)(authorized?.?to.?work|work.?auth|legally.?auth|right.?to.?work|eligible.?to.?work|require.?sponsor|visa.?sponsor)", "_work_auth", "select_or_fill"),

    # Start date / availability
    (r"(?i)(start.?date|available.?to.?start|earliest.?start|when.?can.?you.?start|availability)", "_start_date", "fill"),

    # Remote / relocation
    (r"(?i)(willing.?to.?relocate|relocation|open.?to.?relocation)", "_no", "select_or_fill"),
    (r"(?i)(remote|work.?from.?home|wfh|work.?location.?pref)", "_yes", "select_or_fill"),

    # How did you hear
    (r"(?i)(how.?did.?you.?(hear|find|learn)|source|referr)", "_how_heard", "fill"),

    # Website / portfolio
    (r"(?i)(website|portfolio|personal.?url|github)", "_skip", None),

    # Gender/demographics (optional, skip)
    (r"(?i)(gender|race|ethnicity|veteran|disability|demographic)", "_skip", None),
]

# Submit button patterns
SUBMIT_PATTERNS = [
    r"(?i)(submit.?application|apply.?now|apply.?for|submit$|apply$|send.?application|submit.?resume)",
    r"(?i)(continue|next|proceed)",
]


def _match_field(text: str) -> tuple[str, str] | None:
    """Match field text against known patterns. Returns (profile_key, action) or None."""
    for pattern, key, action in FIELD_PATTERNS:
        if re.search(pattern, text):
            return key, action
    return None


def _resolve_value(key: str, profile: dict, cover_letter: str, known_answers: dict) -> str | None:
    """Resolve a matched key to an actual value."""
    if key == "full_name":
        return profile.get("full_name", "")
    if key == "_first_name":
        name = profile.get("full_name", "")
        return name.split()[0] if name else ""
    if key == "_last_name":
        name = profile.get("full_name", "")
        parts = name.split()
        return parts[-1] if len(parts) > 1 else ""
    if key == "email":
        return profile.get("email", "")
    if key == "phone":
        return profile.get("phone", "")
    if key == "_location":
        return "Remote"
    if key == "_state":
        return ""
    if key == "_zip":
        return ""
    if key == "_country":
        return "United States"
    if key == "_cover_letter":
        return cover_letter or ""
    if key == "_linkedin":
        return ""  # No LinkedIn
    if key == "_experience":
        return "5"
    if key == "_salary":
        return ""  # Skip salary
    if key == "_work_auth":
        return "Yes"
    if key == "_start_date":
        return "Immediately"
    if key == "_no":
        return "No"
    if key == "_yes":
        return "Yes"
    if key == "_how_heard":
        return "Online job search"
    if key == "_resume":
        return "__UPLOAD__"
    if key == "_skip":
        return None
    return profile.get(key, "")


async def extract_form_fields(page: Page) -> list[dict]:
    """Extract all form fields using Playwright locators (pierces Shadow DOM)."""
    fields = []

    for tag in ["input", "select", "textarea"]:
        elements = await page.locator(tag).all()
        for el in elements:
            try:
                el_type = (await el.get_attribute("type") or "").lower()
                if el_type in ("hidden", "submit", "button", "image", "reset"):
                    continue

                visible = await el.is_visible()
                if not visible and el_type != "file":
                    continue

                el_id = await el.get_attribute("id") or ""
                el_name = await el.get_attribute("name") or ""
                aria_label = await el.get_attribute("aria-label") or ""
                placeholder = await el.get_attribute("placeholder") or ""
                title = await el.get_attribute("title") or ""
                required = await el.get_attribute("required") is not None or await el.get_attribute("aria-required") == "true"

                # Try to find label text
                label = ""
                try:
                    if el_id:
                        label_el = page.locator(f'label[for="{el_id}"]').first
                        if await label_el.is_visible(timeout=500):
                            label = (await label_el.inner_text()).strip()
                except Exception:
                    pass

                # Try nearby text via parent
                nearby = ""
                if not label:
                    try:
                        nearby = await el.evaluate("""el => {
                            const parent = el.closest('.field, .form-group, .form-field, [class*="field"], [class*="question"], label, .MuiFormControl-root, .spl-form-element, div[class*="input"]');
                            if (parent) {
                                const lbl = parent.querySelector('label, .label, [class*="label"], legend, h3, h4, span');
                                if (lbl && lbl !== el) return lbl.textContent?.trim() || '';
                            }
                            return '';
                        }""")
                    except Exception:
                        pass

                # Build selector
                selector = ""
                if el_id:
                    selector = f"#{el_id}"
                elif el_name:
                    selector = f'[name="{el_name}"]'
                elif aria_label:
                    selector = f'[aria-label="{aria_label}"]'
                if not selector:
                    continue

                # Get options for select
                options = []
                if tag == "select":
                    try:
                        options = await el.evaluate("""el =>
                            Array.from(el.options).map(o => ({value: o.value, text: o.text.trim()}))
                        """)
                    except Exception:
                        pass

                fields.append({
                    "selector": selector,
                    "tag": tag,
                    "type": el_type or tag,
                    "label": label,
                    "ariaLabel": aria_label,
                    "placeholder": placeholder,
                    "name": el_name,
                    "id": el_id,
                    "title": title,
                    "nearbyText": nearby,
                    "required": required,
                    "options": options,
                    "value": "",
                })

            except Exception as e:
                logger.debug("Error extracting field: %s", e)
                continue

    return fields


async def find_submit_button(page: Page) -> str | None:
    """Find the submit/apply button on the page using Playwright locators."""
    patterns = [
        'button:has-text("Submit Application")',
        'button:has-text("Apply Now")',
        'button:has-text("Apply With")',
        'button:has-text("Apply for")',
        'button:has-text("Submit")',
        'button:has-text("Apply")',
        'button:has-text("Send Application")',
        'button:has-text("Submit Resume")',
        'input[type="submit"]',
        'button[type="submit"]',
        'button:has-text("Continue")',
        'button:has-text("Next")',
    ]
    for sel in patterns:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1000):
                return sel
        except Exception:
            continue
    return None


async def detect_page_type(page: Page) -> str:
    """Detect what kind of page we're on using Playwright locators."""
    url = page.url.lower()
    try:
        text = (await page.inner_text("body"))[:5000].lower()
    except Exception:
        text = ""

    if ("sign in" in text and "password" in text) or "/login" in url or "/signin" in url:
        return "login_required"

    if "captcha" in text or "verify you are human" in text or "just a moment" in text:
        return "captcha"

    if any(kw in text for kw in ["page not found", "no longer available", "job has been filled",
                                  "position has been closed", "position is no longer", "expired"]):
        return "expired"

    # Check visible inputs using Playwright (pierces Shadow DOM)
    visible_count = 0
    for tag in ["input", "select", "textarea"]:
        elements = await page.locator(tag).all()
        for el in elements:
            try:
                el_type = (await el.get_attribute("type") or "").lower()
                if el_type in ("hidden", "submit", "button"):
                    continue
                if await el.is_visible():
                    visible_count += 1
                    if visible_count >= 2:
                        return "application_form"
            except Exception:
                continue

    if "apply" in text and visible_count < 2:
        return "job_listing"

    return "unknown"


async def analyze_page(
    page: Page,
    profile: dict,
    cover_letter: str = "",
    known_answers: dict | None = None,
) -> dict:
    """Analyze the page and return field mapping using rule-based matching."""
    known_answers = known_answers or {}

    page_type = await detect_page_type(page)
    logger.info("Page type: %s", page_type)

    if page_type in ("login_required", "captcha", "expired"):
        return {
            "fields": [],
            "unknown_questions": [],
            "submit_selector": None,
            "page_type": page_type,
        }

    # Extract fields
    raw_fields = await extract_form_fields(page)
    logger.info("Found %d form fields", len(raw_fields))

    fields = []
    unknown = []

    for f in raw_fields:
        # Combine all text for matching
        match_text = " ".join(filter(None, [
            f["label"], f["ariaLabel"], f["placeholder"],
            f["name"], f["id"], f["title"], f["nearbyText"],
        ]))

        # Check known answers first
        for q_text, answer in known_answers.items():
            if any(part.lower() in match_text.lower() for part in q_text.split() if len(part) > 3):
                fields.append({
                    "selector": f["selector"],
                    "action": "select" if f["tag"] == "select" else "fill",
                    "value": answer,
                    "matched": f"known_answer:{q_text[:30]}",
                })
                break
        else:
            # Try pattern matching
            match = _match_field(match_text)
            if match:
                key, action = match
                value = _resolve_value(key, profile, cover_letter, known_answers)

                if value is None:  # skip
                    continue

                if value == "__UPLOAD__":
                    resume_path = profile.get("resume_path") or ""
                    if resume_path:
                        fields.append({
                            "selector": f["selector"],
                            "action": "upload",
                            "value": resume_path,
                            "matched": "resume",
                        })
                    continue

                # Handle select fields
                if f["tag"] == "select" and f["options"]:
                    best_option = _match_select_option(f["options"], value, key)
                    if best_option:
                        fields.append({
                            "selector": f["selector"],
                            "action": "select",
                            "value": best_option,
                            "matched": key,
                        })
                    continue

                if action == "select_or_fill":
                    if f["tag"] == "select" and f["options"]:
                        best = _match_select_option(f["options"], value, key)
                        if best:
                            fields.append({
                                "selector": f["selector"],
                                "action": "select",
                                "value": best,
                                "matched": key,
                            })
                    elif f["type"] in ("checkbox", "radio"):
                        fields.append({
                            "selector": f["selector"],
                            "action": "check",
                            "value": "true",
                            "matched": key,
                        })
                    else:
                        fields.append({
                            "selector": f["selector"],
                            "action": "fill",
                            "value": value,
                            "matched": key,
                        })
                    continue

                if value:
                    fields.append({
                        "selector": f["selector"],
                        "action": action,
                        "value": value,
                        "matched": key,
                    })
            else:
                # Unknown field
                if f["required"] or f["tag"] == "select":
                    unknown.append({
                        "question_text": match_text[:200],
                        "selector": f["selector"],
                        "type": f["type"],
                        "options": [o["text"] for o in f.get("options", [])],
                    })

    submit = await find_submit_button(page)

    logger.info("Mapped %d fields, %d unknown, submit: %s", len(fields), len(unknown), submit)

    return {
        "fields": fields,
        "unknown_questions": unknown,
        "submit_selector": submit,
        "page_type": page_type,
    }


def _match_select_option(options: list[dict], desired: str, key: str) -> str | None:
    """Find the best matching option in a select dropdown."""
    desired_lower = desired.lower()

    # For yes/no questions
    if key in ("_work_auth", "_yes"):
        for o in options:
            t = o["text"].lower()
            if t in ("yes", "yes, i am", "authorized", "yes - authorized"):
                return o["text"]
        for o in options:
            if "yes" in o["text"].lower():
                return o["text"]

    if key == "_no":
        for o in options:
            if "no" == o["text"].lower().strip():
                return o["text"]

    if key == "_country":
        for o in options:
            t = o["text"].lower()
            if "united states" in t or t == "us" or t == "usa":
                return o["text"]

    # Generic: find closest text match
    for o in options:
        if o["text"].lower().strip() == desired_lower:
            return o["text"]

    for o in options:
        if desired_lower in o["text"].lower():
            return o["text"]

    return None
