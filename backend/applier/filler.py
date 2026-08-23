import logging

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# Workable (and friends) hide the real radio/checkbox input (aria-hidden,
# visually styled wrapper div[role=radio|checkbox]) — Playwright's check()
# refuses hidden elements. Click the ARIA wrapper like a user would; fall back
# to a JS click on the input itself. Never un-checks an already-on control.
_FORCE_CHECK_JS = """el => {
    const w = el.closest('[role="radio"], [role="checkbox"], label');
    const isOn = () => el.checked || (w && w.getAttribute('aria-checked') === 'true');
    if (!isOn() && w) w.click();
    if (!isOn()) el.click();
    return !!isOn();
}"""


async def check_input(page: Page, selector: str) -> bool:
    """Check a radio/checkbox even when the ATS hides the real input."""
    el = page.locator(selector).first
    try:
        await el.check(timeout=3000)
        return True
    except Exception:
        pass
    try:
        # explicit timeout: a stale selector (re-rendered DOM) must fail fast,
        # not hang for the 30s locator default
        return bool(await el.evaluate(_FORCE_CHECK_JS, timeout=4000))
    except Exception as e:
        logger.debug("check_input fallback failed for %s: %s", selector, e)
        return False


_OVERLAY_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "#hs-eu-confirmation-button",
    "button[aria-label*='accept' i]",
    "button:has-text('Accept all')",
    "button:has-text('Accept All Cookies')",
    "button:has-text('Accept cookies')",
    "button:has-text('Allow all')",
    "button:has-text('I agree')",
    "button:has-text('Got it')",
    "[id*='cookie' i] button:has-text('Accept')",
    ".cookie-banner button, .cookie-consent button",
]


async def dismiss_overlays(page: Page) -> int:
    """Best-effort: close cookie/consent banners that intercept clicks on form fields
    (OneTrust, HubSpot EU, generic 'Accept'). Never raises. Returns count dismissed."""
    dismissed = 0
    for sel in _OVERLAY_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible(timeout=500):
                await loc.click(timeout=1500, force=True)
                dismissed += 1
                await page.wait_for_timeout(300)
                if dismissed >= 2:
                    break
        except Exception:
            continue
    return dismissed


async def select_dropdown(page: Page, selector: str, label: str) -> bool:
    """Fill a native <select> OR a custom (React/div) dropdown by option label.
    select_option only works on native <select>; modern ATS use custom comboboxes,
    so fall back to open-and-click-the-option, then type-to-filter + Enter."""
    loc = page.locator(selector).first
    try:                                   # 1) native <select>
        await loc.select_option(label=label, timeout=2500)
        return True
    except Exception:
        pass
    try:                                   # 2) custom dropdown: open it
        await loc.click(timeout=2500, force=True)
        await page.wait_for_timeout(400)
        for finder in (
            lambda: page.get_by_role("option", name=label, exact=True),
            lambda: page.get_by_role("option", name=label, exact=False),
            lambda: page.get_by_text(label, exact=True),
        ):
            try:
                opt = finder().first
                if await opt.count() and await opt.is_visible(timeout=900):
                    await opt.click(timeout=1500, force=True)
                    return True
            except Exception:
                continue
        # react-select style: type to filter, then pick / Enter
        await page.keyboard.type(str(label)[:40], delay=25)
        await page.wait_for_timeout(500)
        opt = page.get_by_role("option", name=label, exact=False).first
        if await opt.count() and await opt.is_visible(timeout=900):
            await opt.click(timeout=1200, force=True)
            return True
        await page.keyboard.press("Enter")
        return True
    except Exception as e:
        logger.debug("select_dropdown failed for %s=%r: %s", selector, label, e)
        return False


async def fill_field(page: Page, field: dict) -> bool:
    """Fill a single form field based on analyzer output."""
    selector = field.get("selector", "")
    action = field.get("action", "fill")
    value = field.get("value", "")

    try:
        element = page.locator(selector).first
        # Uploads and checks skip the visibility gate: ATSes (Workable) hide the
        # real <input type=file> behind a styled drop zone and the real
        # radio/checkbox behind a styled wrapper — set_input_files works on
        # hidden inputs, and check_input() clicks the wrapper.
        if action not in ("upload", "check") and not await element.is_visible(timeout=3000):
            logger.warning("Field not visible: %s", selector)
            return False

        # For Web Components (spl-input, etc.), target the inner <input>
        tag_name = await element.evaluate("el => el.tagName.toLowerCase()")
        if tag_name not in ("input", "select", "textarea") and action in ("fill", "check"):
            # For phone fields, find the actual phone number input (not country dropdown)
            inner = None
            if "phone" in selector.lower() or "tel" in (await element.get_attribute("type") or ""):
                inner_all = await element.locator('input[type="tel"], input[autocomplete="tel"]').all()
                if not inner_all:
                    inner_all = await element.locator("input").all()
                # Pick the visible one that's not the country search
                for inp in inner_all:
                    try:
                        if await inp.is_visible(timeout=500):
                            aria = await inp.get_attribute("aria-label") or ""
                            if "country" not in aria.lower() and "search" not in aria.lower():
                                inner = inp
                                break
                    except Exception:
                        continue
            if inner is None:
                try:
                    candidate = element.locator("input:not([role='combobox']), textarea, select").first
                    if await candidate.count() > 0 and await candidate.is_visible(timeout=1000):
                        inner = candidate
                except Exception:
                    pass
            if inner is None:
                try:
                    candidate = element.locator("input, textarea, select").first
                    if await candidate.count() > 0:
                        inner = candidate
                except Exception:
                    pass
            if inner:
                element = inner

        if action == "fill":
            # Lever geocode "Current location" typeahead: a plain fill leaves the hidden
            # `selectedLocation` empty and Lever rejects the value — must type then PICK a
            # dropdown suggestion (ArrowDown+Enter selects the first geocode hit).
            try:
                is_lever_loc = await element.evaluate(
                    "el => (el.name==='location' || (el.className||'').includes('location'))"
                    " && !!(el.form && el.form.querySelector('[name=\"selectedLocation\"]'))")
            except Exception:
                is_lever_loc = False
            if is_lever_loc:
                # Poll the geocode dropdown for a REAL suggestion (skip the widget's own
                # 'Loading' / 'No location found' rows) before committing. ArrowDown+Enter
                # on a fixed timer used to fire blindly: with no suggestion it selects
                # nothing AND wipes the typed text, leaving the REQUIRED field blank while
                # the caller counted it "filled" (phantom success). Only press Enter once a
                # suggestion is actually present.
                _STATE_JS = """() => {
                    const box = document.querySelector(
                        '.dropdown-location, [class*="dropdown-results"], [class*="dropdown-location"]');
                    if (!box) return 'none';
                    if (box.querySelector('[class*="no-results"]')) return 'noresults';
                    if (box.querySelector('[class*="loading"]')) return 'loading';
                    // A REAL suggestion is a LEAF element with actual location text — NOT the
                    // '.dropdown-results' container itself (which matches [class*=result] and
                    // exists even when empty, so the old selector always read 'ready' -> a
                    // phantom pick on a dead geocode). Require a childless node with >2 chars
                    // of text that isn't the widget's own 'Loading'/'No location found' state.
                    const rows = [...box.querySelectorAll('*')].filter(e => {
                        const t = (e.textContent || '').trim();
                        return e.children.length === 0 && t.length > 2
                            && !/no location found|loading/i.test(t);
                    });
                    return rows.length ? 'ready' : 'empty';
                }"""

                async def _type_and_poll(text: str) -> str:
                    await element.click(timeout=3000)
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                    await page.keyboard.type(text, delay=40)
                    state = "none"
                    for _ in range(8):  # up to ~4s for the network geocode
                        await page.wait_for_timeout(500)
                        try:
                            state = await page.evaluate(_STATE_JS)
                        except Exception:
                            state = "none"
                        if state in ("ready", "noresults"):
                            break
                    return state

                try:
                    state = await _type_and_poll(value)
                    if state != "ready" and "," in value:
                        # a bare city segment geocodes more often than 'City, Country'
                        state = await _type_and_poll(value.split(",")[0].strip())
                    if state == "ready":
                        # A genuine geocode suggestion is present — commit it.
                        await page.keyboard.press("ArrowDown")
                        await page.keyboard.press("Enter")
                        await page.wait_for_timeout(300)
                    else:
                        # Geocode never resolved (common on a datacenter IP). Best-effort
                        # JS-set both the visible input and the hidden selectedLocation as a
                        # submit aid; Lever usually clears a non-geocoded value on reconcile.
                        try:
                            await element.evaluate(
                                """(el, val) => {
                                    const set = (node, v) => {
                                        if (!node) return;
                                        const tr = node._valueTracker;
                                        if (tr) tr.setValue('');
                                        const d = Object.getOwnPropertyDescriptor(
                                            window.HTMLInputElement.prototype, 'value').set;
                                        d.call(node, v);
                                        node.dispatchEvent(new Event('input', {bubbles: true}));
                                        node.dispatchEvent(new Event('change', {bubbles: true}));
                                    };
                                    set(el, val);
                                    set(el.form && el.form.querySelector(
                                        '[name="selectedLocation"]'), val);
                                }""", value)
                        except Exception as e2:
                            logger.debug("lever location direct-set failed: %s", e2)
                    # HONEST return = False. On this datacenter IP Lever's geocode never
                    # commits a persistent selection: whatever a pick or the JS-set shows
                    # IMMEDIATELY, React clears on a LATER async reconcile (verified: the
                    # visible input and the hidden selectedLocation both end empty every time,
                    # and the clear fires >1.2s later — after any settle a single fill_field
                    # call can wait). So there is no reliable in-call signal that the value
                    # stuck; report the field unfilled so a REQUIRED 'Current location'
                    # surfaces for the human (who completes it where the geocode resolves) and
                    # is never a phantom "filled" over a blank. An OPTIONAL location isn't in
                    # failed_required, so it stays silently blank. The best-effort JS-set above
                    # still gives the form a value in the rare case Lever does keep it.
                    logger.info("Lever location geocode unreliable on this host; "
                                "reporting unfilled for the human")
                    return False
                except Exception as e:
                    # Do NOT fall through to the plain-text fill below: on a Lever geocode
                    # field a bare .fill() types text React discards (and here the click was
                    # blocked by an hCaptcha overlay), which would return True over a blank
                    # required field. Report unfilled instead so it surfaces for the human.
                    logger.debug("lever location typeahead failed: %s", e)
                    return False
            try:
                await element.clear(timeout=5000)
                await element.fill(value, timeout=5000)
            except Exception:
                # Fallback: click and type
                await element.click(timeout=3000)
                await page.keyboard.press("Control+a")
                await page.keyboard.type(value, delay=50)
            logger.info("Filled '%s' = '%s'", selector, value[:50])

        elif action == "select":
            if not await select_dropdown(page, selector, value):
                logger.warning("Could not select '%s' = '%s'", selector, value)
                return False
            logger.info("Selected '%s' = '%s'", selector, value)

        elif action == "check":
            if value.lower() in ("true", "yes", "1"):
                if not await check_input(page, selector):
                    logger.warning("Could not check '%s'", selector)
                    return False
            else:
                try:
                    await element.uncheck(timeout=3000)
                except Exception:
                    # Fallback: click the element
                    await element.click(timeout=3000)
            logger.info("Checked '%s' = '%s'", selector, value)

        elif action == "upload":
            if value:
                await element.set_input_files(value)
                logger.info("Uploaded resume to '%s'", selector)
            else:
                logger.warning("No resume path provided for upload field")
                return False

        elif action == "click":
            await element.click()
            logger.info("Clicked '%s'", selector)

        else:
            logger.warning("Unknown action: %s", action)
            return False

        return True

    except Exception as e:
        logger.error("Failed to fill '%s': %s", selector, e)
        return False


async def fill_form(page: Page, analysis: dict) -> tuple[int, int, list[str]]:
    """Fill all fields from analysis. Returns (success, fail, failed_required_labels).
    failed_required carries the human labels of REQUIRED standard fields whose fill
    returned False (e.g. a Lever 'Current location' the dead-geocode couldn't set) so the
    caller can surface them as unfilled — otherwise a required field that fails silently
    vanishes from BOTH the filled count and the unfilled list (phantom 'form complete')."""
    success = 0
    fail = 0
    failed_required: list[str] = []

    for field in analysis.get("fields", []):
        if await fill_field(page, field):
            success += 1
        else:
            fail += 1
            if field.get("required"):
                label = (field.get("label") or "").strip()
                low = label.lower()
                if not label or "loading" in low or "no location found" in low:
                    # the analyzer's display_text can absorb the geocode dropdown's own
                    # 'Loading'/'No location found' state text — give the human a clean label
                    label = ("Current location" if field.get("matched") == "_location"
                             else (field.get("matched") or "").lstrip("_").replace("_", " "))
                if label and label not in failed_required:
                    failed_required.append(label)
        # Small delay between fields to appear human
        await page.wait_for_timeout(300)

    return success, fail, failed_required


async def click_submit(page: Page, analysis: dict) -> bool:
    """Click the submit/apply button."""
    selector = analysis.get("submit_selector")
    if not selector:
        logger.warning("No submit selector found")
        return False

    try:
        button = page.locator(selector).first
        if await button.is_visible(timeout=3000):
            await button.click()
            logger.info("Clicked submit: %s", selector)
            await page.wait_for_timeout(2000)
            return True
        else:
            logger.warning("Submit button not visible: %s", selector)
            return False
    except Exception as e:
        logger.error("Failed to click submit: %s", e)
        return False
