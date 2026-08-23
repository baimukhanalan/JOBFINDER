"""Custom-widget closed-question engine: form controls the DOM analyzer can't see
because they aren't native <input>/<select>/<textarea>.

Two widget families:
- Greenhouse React-Select dropdowns (`.select__container` div comboboxes).
  `fill_react_selects` auto-answers ONLY the deterministic identity/eligibility ones
  (work-auth=Yes, sponsorship=No, country=United States, 18+, background-check,
  reliable-equipment). The rest are harvested as CLOSED questions
  (`harvest_react_selects`) for the constrained-choice engine, applied via
  `apply_react_select_choice`.
- Ashby-style <button> toggle groups (Yes/No screeners rendered as button pairs,
  not radio inputs): `harvest_button_groups` / `apply_button_choice`.

Role-specific screeners are answered only through the constrained-choice engine
(validated option index, [review]-gated when unbacked) — never free-typed.
"""
import logging
import re

from backend.applier.analyzer import _clean_text

logger = logging.getLogger(__name__)

# (label regex, value-to-type). First match wins.
_RULES: list[tuple[str, str]] = [
    (r"(authoriz.*work|legally.*work|work.*authoriz|right to work|eligible to work)", "Yes"),
    (r"(require|need|will you).{0,30}sponsor|sponsor.{0,20}(ship|now or in the future)|visa sponsor", "No"),
    (r"(^|\b)country(\b|$)", "United States"),
    (r"(at least 18|over 18|18 ?years|are you 18)", "Yes"),
    (r"(willing|consent|agree|comfortable|authoriz)\w*.{0,40}(background|criminal).{0,15}(check|screen)", "Yes"),
    (r"(reliable.*(internet|computer)|high.?speed internet|own equipment|wired.*internet)", "Yes"),
]
# A work-auth question naming a non-US country must NOT be auto-"Yes".
_FOREIGN = re.compile(
    r"(?i)work in (?:the )?(ireland|uk|united kingdom|england|canada|australia|germany|france|"
    r"spain|netherlands|india|philippines|singapore|europe|the eu)")


def _value_for(label: str) -> str | None:
    t = (label or "").lower().strip()
    if not t:
        return None
    for pat, val in _RULES:
        if re.search(pat, t):
            if val == "Yes" and "work" in t and _FOREIGN.search(t):
                return None  # don't claim authorization in a foreign country
            return val
    return None


async def fill_react_selects(page) -> dict:
    """Fill Greenhouse react-select dropdowns for the deterministic eligibility questions.

    Returns {"filled": n, "handled": [labels]}. No-op on non-Greenhouse pages (the
    .select__container class is Greenhouse-specific), so safe to call for any ATS.
    """
    handled: list[str] = []
    try:
        containers = await page.query_selector_all(".select__container")
    except Exception:
        return {"filled": 0, "handled": []}

    for c in containers:
        label = ""
        try:
            # skip ones already answered (react-select shows a single-value span)
            if await c.query_selector(".select__single-value"):
                continue
            label_el = await c.query_selector("label, .select__label")
            label = (await label_el.inner_text()).strip() if label_el else ""
            value = _value_for(label)
            if not value:
                continue
            control = await c.query_selector(".select__control")
            if not control:
                continue
            await control.click()
            await page.wait_for_timeout(300)
            await page.keyboard.type(value, delay=20)
            await page.wait_for_timeout(700)
            # Click the option that matches by TEXT (more reliable than Enter, which picks
            # whatever's highlighted — that gave a wrong "+1" on the Country field).
            opt = None
            try:
                for o in await page.query_selector_all(".select__option"):
                    t = (await o.inner_text()).strip().lower()
                    if t == value.lower():
                        opt = o
                        break
                    if opt is None and t.startswith(value.lower()):
                        opt = o
            except Exception:
                opt = None
            if opt:
                await opt.click()
            else:
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(300)
            # confirm it took
            if await c.query_selector(".select__single-value"):
                handled.append(label[:50])
            else:  # close the menu if it didn't select
                await page.keyboard.press("Escape")
        except Exception as e:
            logger.debug("react-select fill failed for %r: %s", label[:40], e)
            continue
    if handled:
        logger.info("react-select filled %d: %s", len(handled), handled)
    return {"filled": len(handled), "handled": handled}


# ---------------------------------------------------------------------------
# D1/D2 — React-Select harvest + apply (constrained-choice engine input)
# ---------------------------------------------------------------------------

MAX_RS_OPTIONS = 50  # cap options read per dropdown (huge country lists etc.)

# EEOC/demographic survey questions are intentionally left blank (same policy as
# the analyzer's `_skip` rule) — never auto-answered, never reported as unfilled.
_DEMOGRAPHIC = re.compile(
    r"(?i)(gender|rac(e|ial)|ethnic|veteran|military|armed\s*forces|disabilit|demographic|"
    r"hispanic|latin[ox]?\b(?!\s*americ)|pronoun|sexual orientation|transgender|lgbtq|neurodiverg|"
    r"your (?:current )?age\b|age (?:range|group|bracket)|date of birth|\bdob\b)")


def shape_react_select(label: str, options: list[str],
                       container_index: int) -> dict | None:
    """Pure shaping of one harvested react-select into a choice-engine question.
    Returns None when there's no usable question text, fewer than 2 options, or
    the question is a demographic survey item (intentionally left blank)."""
    question = _clean_text(label or "").strip(" *")
    opts = [o.strip() for o in options if o and o.strip()][:MAX_RS_OPTIONS]
    if len(question) < 4 or len(opts) < 2 or _DEMOGRAPHIC.search(question):
        return None
    return {"question_text": question[:200], "options": opts,
            "container_index": container_index}


async def harvest_react_selects(page) -> list[dict]:
    """Collect UNANSWERED react-select dropdowns as closed questions: open each
    menu, read the option texts, close it again (Escape). Containers already
    showing `.select__single-value` (answered by `fill_react_selects` or a prior
    run) are skipped — keep `fill_react_selects` running BEFORE this.

    Returns [{question_text, options: [str], container_index}].
    """
    out: list[dict] = []
    try:
        containers = await page.query_selector_all(".select__container")
    except Exception:
        return out
    for idx, c in enumerate(containers):
        try:
            if await c.query_selector(".select__single-value") or await c.query_selector(".select__multi-value"):
                continue  # already answered (single OR a multi already given chips)
            label_el = await c.query_selector("label, .select__label")
            label = (await label_el.inner_text()).strip() if label_el else ""
            # pre-filter on the label so we don't open menus we'll discard anyway
            if shape_react_select(label, ["_", "_"], idx) is None:
                continue
            control = await c.query_selector(".select__control")
            if not control:
                continue
            await control.click()
            await page.wait_for_timeout(400)
            options = []
            for o in (await page.query_selector_all(".select__option"))[:MAX_RS_OPTIONS]:
                t = (await o.inner_text()).strip()
                if t:
                    options.append(t)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(150)
            q = shape_react_select(label, options, idx)
            if q:
                out.append(q)
        except Exception as e:
            logger.debug("react-select harvest failed at #%d: %s", idx, e)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
    if out:
        logger.info("react-select harvested %d closed questions", len(out))
    return out


# Degree-level mapping: a résumé degree string ('MSc Bioinformatics') -> the level word,
# then to the option label ('Master's Degree') on a fixed Degree react-select. Only strong
# degree signals so a non-degree value never trips it.
_DEGREE_LEVEL = [
    (re.compile(r"(?i)\b(ph\.?d|doctora?te?|dphil|d\.?phil)\b"), "doctor"),
    (re.compile(r"(?i)\b(m\.?sc|m\.?eng|m\.?phil|mba|master'?s?|masters)\b"), "master"),
    (re.compile(r"(?i)\b(b\.?sc|b\.?eng|b\.?tech|bachelor'?s?|bachelors|undergrad\w*)\b"), "bachelor"),
    (re.compile(r"(?i)\b(associate'?s?|associates|\ba\.?a\.?\b|\ba\.?s\.?\b)\b"), "associate"),
    (re.compile(r"(?i)\b(high[- ]school|secondary|\bged\b|diploma)\b"), "high_school"),
]
_OPT_LEVEL = {
    "doctor": re.compile(r"(?i)doctor|ph\.?d|d\.?phil"),
    "master": re.compile(r"(?i)master|graduate"),
    "bachelor": re.compile(r"(?i)bachelor|undergrad"),
    "associate": re.compile(r"(?i)associate"),
    "high_school": re.compile(r"(?i)high[- ]school|secondary|diploma|\bged\b"),
}


def _degree_level(text: str) -> str | None:
    for rx, lvl in _DEGREE_LEVEL:
        if rx.search(text or ""):
            return lvl
    return None


# The explicit NON-DISCLOSURE option on an EEO/diversity self-ID field. Selecting it answers
# a (often required) demographic WITHOUT ever claiming a protected characteristic.
_DECLINE_RE = re.compile(
    r"(?i)prefer not to (?:answer|say|disclose|respond|state|identify)"
    r"|decline to (?:self.?identif|answer|state|respond|disclose)"
    r"|(?:don'?t|do not) (?:wish|want) to (?:answer|disclose|self.?identif|respond|provide|say)"
    r"|choose not to (?:answer|disclose|identify)"
    r"|i (?:prefer|choose) not to\b|rather not (?:say|answer)|not to disclose")

_LABEL_JS = r"""el => {
  const clean = s => (s||'').replace(/\s+/g,' ').trim();
  const a = el.getAttribute('aria-labelledby');
  if (a) { const l = document.getElementById(a.split(' ')[0]); if (l) return clean(l.innerText); }
  if (el.id) { const l = document.querySelector('label[for="'+CSS.escape(el.id)+'"]'); if (l) return clean(l.innerText); }
  const c = el.closest('label,fieldset,li,.field,[class*="field"],[class*="question"]');
  return c ? clean(c.innerText).slice(0,120) : clean((el.getAttribute('aria-label')||el.name||''));
}"""


async def fill_demographics_decline(page) -> dict:
    """EEO/diversity self-ID fields are answered with the explicit NON-DISCLOSURE option
    ('Prefer not to answer' / 'Decline to self-identify') when the field offers one, rather
    than left blank — so a REQUIRED demographic (e.g. Affirm 'Pronouns *') completes without
    ever claiming a protected characteristic. Handles react-selects, native <select> and radio
    groups; a demographic with no decline option stays blank (nothing safe to pick)."""
    filled: list[str] = []
    # 1) react-select demographic dropdowns
    try:
        conts = await page.query_selector_all(".select__container")
    except Exception:
        conts = []
    for c in conts:
        try:
            if await c.query_selector(".select__single-value") or await c.query_selector(".select__multi-value"):
                continue
            le = await c.query_selector("label, .select__label")
            label = (await le.inner_text()).strip() if le else ""
            if not _DEMOGRAPHIC.search(_clean_text(label).lower()):
                continue
            control = await c.query_selector(".select__control")
            if not control:
                continue
            await control.click()
            await page.wait_for_timeout(300)
            picked = None
            for o in await page.query_selector_all(".select__option"):
                if _DECLINE_RE.search((await o.inner_text()) or ""):
                    picked = o
                    break
            if picked:
                await picked.click()
                await page.wait_for_timeout(200)
                if await c.query_selector(".select__single-value"):
                    filled.append(label[:40])
            else:
                await page.keyboard.press("Escape")
        except Exception:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
    # 2) native <select> demographic
    try:
        sels = await page.query_selector_all("select")
    except Exception:
        sels = []
    for s in sels:
        try:
            if (await s.evaluate("el=>el.value") or "").strip():
                continue
            label = await s.evaluate(_LABEL_JS)
            if not _DEMOGRAPHIC.search((label or "").lower()):
                continue
            val = await s.evaluate(
                "el=>{for(const o of el.options){if(/prefer not|decline|wish to|choose not|"
                "rather not|not to (answer|say|disclose)/i.test(o.text)) return o.value;}return null;}")
            if val is not None:
                await s.select_option(val)
                filled.append((label or "")[:40])
        except Exception:
            continue
    # 3) radio-group demographic — check the decline radio
    try:
        radios = await page.query_selector_all("input[type=radio]")
    except Exception:
        radios = []
    for r in radios:
        try:
            if await r.is_checked():
                continue
            own = await r.evaluate(_LABEL_JS)
            if not (own and _DECLINE_RE.search(own)):
                continue
            grp = await r.evaluate(
                "el=>{const g=el.closest('fieldset,[role=radiogroup],[class*=question],li,.field');"
                " return g?g.innerText.slice(0,160):'';}")
            if not _DEMOGRAPHIC.search((grp or "").lower()):
                continue
            try:
                await r.check(timeout=2500)
            except Exception:
                await r.evaluate("el=>{const w=el.closest('label,[role=radio]'); (w||el).click();}")
            filled.append((grp or "")[:40])
        except Exception:
            continue
    # 4) Workable-style input[role=combobox] demographic (e.g. 'Preferred pronouns') — NOT a
    #    .select__container / native <select> / radio, so 1-3 miss it. Runs AFTER
    #    fill_comboboxes_known (base.prefill order), so opening these can't disrupt the already
    #    filled skill/availability comboboxes. Open, pick the decline option, else leave blank.
    try:
        combos = await page.query_selector_all(_COMBO_SEL)
    except Exception:
        combos = []
    for box in combos:
        try:
            # Close any listbox left open by the previous combobox, AND remove the Workable
            # `data-ui="backdrop"` overlay it leaves behind — that backdrop intercepts the next
            # combobox's click (why a 2nd demographic like 'pronouns' stayed blank after
            # 'gender' was declined: "<div data-ui=backdrop> intercepts pointer events").
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(120)
            try:
                await page.evaluate(
                    "() => document.querySelectorAll('[data-ui=\"backdrop\"]')"
                    ".forEach(e => e.remove())")
            except Exception:
                pass
            if (await box.evaluate("el=>el.value") or "").strip():
                continue  # already answered
            label = _clean_text(await _combo_label(box)).strip(" *").lower()
            if not _DEMOGRAPHIC.search(label):
                continue
            await box.click()
            await page.wait_for_timeout(300)
            picked = None
            for o in await page.query_selector_all("[role='option']"):
                if _DECLINE_RE.search((await o.inner_text()) or ""):
                    picked = o
                    break
            if picked:
                await picked.click()
                await page.wait_for_timeout(200)
                if (await box.evaluate("el=>el.value") or "").strip():
                    filled.append(label[:40])
                    continue
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(120)
        except Exception:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
    if filled:
        logger.info("demographics declined (%d): %s", len(filled), filled)
    return {"filled": len(filled), "handled": filled}


async def apply_react_select_choice(page, container_index: int,
                                    option_text: str, allow_first: bool = False) -> bool:
    """Pick `option_text` in the react-select at `container_index` (harvest order):
    open the menu, type to filter, click the exact/prefix matching option, and
    confirm `.select__single-value` appeared.

    `allow_first` (only the KNOWN-typeahead callers set it — never the screener engine):
    when no option exact/prefix-matches, accept the FIRST result. A remote-search typeahead
    (e.g. Greenhouse School) returns canonical names ('The University of Texas at Austin')
    that don't prefix-match the résumé's exact wording, so the strict match leaves a required
    field blank; the top hit is the right pick, mirroring fill_comboboxes_known's geo fallback."""
    try:
        containers = await page.query_selector_all(".select__container")
        if not 0 <= container_index < len(containers):
            return False
        c = containers[container_index]
        if await c.query_selector(".select__single-value"):
            return True  # already answered
        control = await c.query_selector(".select__control")
        if not control:
            return False
        await control.click()
        await page.wait_for_timeout(300)

        async def _type_and_poll(q: str):
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(q[:60], delay=15)
            for _ in range(8):
                await page.wait_for_timeout(250)
                found = await page.query_selector_all(".select__option")
                if found:
                    return found
            return []

        opts = await _type_and_poll(option_text)
        # A search typeahead returns NOTHING for an over-specific query (e.g. 'University of
        # Texas School of Law' -> 0 hits, but 'University of Texas' -> the list); a fixed
        # client-filtered taxonomy (Greenhouse 'Discipline') returns 0 for a value not in it
        # ('Bioinformatics') but a shorter prefix surfaces the near-label ('Bio' -> Biology).
        # When allowed the first hit, retry with fewer words, then shorter PREFIXES of the
        # first token (down to 1 word / a 3-char stem) until options appear.
        if not opts and allow_first:
            words = option_text.split()
            for n in range(len(words) - 1, 0, -1):            # reach a single word
                opts = await _type_and_poll(" ".join(words[:n]))
                if opts:
                    break
            if not opts and words:
                first = words[0]
                for k in (8, 6, 4, 3):                        # shorter prefixes of the first word
                    if len(first) > k:
                        opts = await _type_and_poll(first[:k])
                        if opts:
                            break
        want = option_text.strip().lower()
        otexts = [(await o.inner_text()).strip() for o in opts]
        opt = None
        for o, t in zip(opts, otexts):
            tl = t.lower()
            if tl == want:
                opt = o
                break
            if opt is None and (tl.startswith(want[:40]) or (len(want) > 4 and want in tl)):
                opt = o
        if opt is None and opts:
            # A degree field ('Degree' = 'MSc Bioinformatics') is a NON-searchable react-select
            # (typing doesn't filter), so a blind first-option pick would claim the WRONG level.
            # Match the known value's degree LEVEL against the option labels instead.
            lvl = _degree_level(want)
            if lvl:
                rx = _OPT_LEVEL[lvl]
                for o, t in zip(opts, otexts):
                    if rx.search(t):
                        opt = o
                        break
        if opt is None and allow_first and opts:
            opt = opts[0]           # remote-search typeahead: the top hit is the answer
        if opt is None:
            await page.keyboard.press("Escape")
            return False
        await opt.click()
        await page.wait_for_timeout(300)
        return bool(await c.query_selector(".select__single-value"))
    except Exception as e:
        logger.debug("react-select apply failed at #%d (%r): %s",
                     container_index, option_text[:40], e)
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return False


async def apply_react_select_multi(page, container_index: int, joined_value: str) -> bool:
    """Pick MULTIPLE options in a 'select all that apply' react-select from a comma-joined known
    value. Reads the real options once (resolve which appear — as a SUBSTRING — in the value;
    never a comma split, legit options contain commas), then picks each by RE-OPENING the control
    before every pick (react-select multi may close the menu on select) and CLEARING the filter
    each time (else the 2nd query types onto the 1st and matches nothing). Confirm via a chip."""
    want = (joined_value or "").lower()
    try:
        containers = await page.query_selector_all(".select__container")
        if not 0 <= container_index < len(containers):
            return False
        c = containers[container_index]
        control = await c.query_selector(".select__control")
        if not control:
            return False
        # Open ONCE and read all options (empty filter) to resolve which the value asks for.
        # react-select multi keeps the menu open after a pick and auto-clears the filter, so we
        # must NOT re-open the control (a 2nd click TOGGLES the menu shut) nor clear by hand.
        await control.click()
        opts = []
        for _ in range(8):
            await page.wait_for_timeout(250)
            opts = await page.query_selector_all(".select__option")
            if opts:
                break
        wanted = []
        for o in opts:
            t = (await o.inner_text()).strip()
            if t and t.lower() in want and t not in wanted:
                wanted.append(t)
        if not wanted:
            await page.keyboard.press("Escape")
            return False
        for text in wanted:
            await page.keyboard.type(text[:60], delay=12)   # filter (input is empty after each pick)
            menu = []
            for _ in range(8):
                await page.wait_for_timeout(200)
                menu = await page.query_selector_all(".select__option")
                if menu:
                    break
            tl = text.strip().lower()
            clicked = False
            for o in menu:
                if ((await o.inner_text()) or "").strip().lower() == tl:
                    await o.click()
                    clicked = True
                    break
            if not clicked and menu:
                await page.keyboard.press("Enter")   # fallback: pick the highlighted top hit
            await page.wait_for_timeout(250)
        await page.keyboard.press("Escape")
        return bool(await c.query_selector(".select__multi-value"))
    except Exception as e:
        logger.debug("react-select multi apply failed at #%d: %s", container_index, e)
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return False


async def fill_react_selects_known(page, known: dict) -> dict:
    """Fill react-select TYPEAHEADS from explicit known answers (e.g. School ->
    the candidate's university). These remote-search widgets list no options until
    something is typed, so `harvest_react_selects` (which needs >=2 options) skips
    them — but when we already hold the answer we can type it and pick. Additive and
    safe: only acts on a container whose label matches a known answer; demographics
    stay blank by policy."""
    filled: list[str] = []
    if not known:
        return {"filled": 0, "handled": []}
    norm = {_clean_text(k).strip(" *").lower(): v
            for k, v in known.items() if v and str(v).strip()}
    try:
        containers = await page.query_selector_all(".select__container")
    except Exception:
        return {"filled": 0, "handled": []}
    for idx, c in enumerate(containers):
        try:
            if await c.query_selector(".select__single-value") or await c.query_selector(".select__multi-value"):
                continue  # already answered (single or multi)
            label_el = await c.query_selector("label, .select__label")
            label = (await label_el.inner_text()).strip() if label_el else ""
            key = _clean_text(label).strip(" *").lower()
            val = norm.get(key)
            if not val or _DEMOGRAPHIC.search(key):
                continue
            is_multi = bool(await c.query_selector(".select__value-container--is-multi"))
            if is_multi:
                if await apply_react_select_multi(page, idx, str(val)):
                    filled.append(label[:50])
            elif await apply_react_select_choice(page, idx, str(val), allow_first=True):
                filled.append(label[:50])
        except Exception as e:
            logger.debug("react-select known-fill failed at #%d: %s", idx, e)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
    if filled:
        logger.info("react-select known-filled %d: %s", len(filled), filled)
    return {"filled": len(filled), "handled": filled}


_COMBO_SEL = "input[role='combobox'], .ashby-application-form-input-autocomplete"


async def _combo_label(box) -> str:
    """Question label for an Ashby-style combobox input (aria-labelledby, else the
    field-entry's stable question-title / .application-label / legend)."""
    try:
        return await box.evaluate(r"""el=>{
          const clean=s=>(s||'').replace(/\s+/g,' ').trim();
          const a=el.getAttribute('aria-labelledby');
          if(a){const l=document.getElementById(a.split(' ')[0]); if(l) return clean(l.innerText);}
          const c=el.closest('.ashby-application-form-field-entry,[class*="_fieldEntry_"],.application-question,fieldset,li');
          if(c){const t=c.querySelector('.ashby-application-form-question-title,.application-label,legend');
            if(t){const cl=t.cloneNode(true);cl.querySelectorAll('input,select,textarea,svg,button').forEach(n=>n.remove());return clean(cl.innerText);}}
          return '';
        }""")
    except Exception:
        return ""


_CEFR_MAP = {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6}


def _lang_option_rank(text: str) -> int:
    """Rank a language-proficiency OPTION label 1..6 (0 = not a language descriptor).
    Handles both CEFR labels and Workable's descriptive sentences ('...speak fluently')."""
    t = (text or "").lower()
    if any(w in t for w in ("native", "mother tongue", "bilingual")):
        return 6
    m = re.search(r"\b([abc][12])\b", t)
    if m:
        return _CEFR_MAP[m.group(1)]
    if "not speak" in t or "cannot speak" in t or "can't speak" in t:
        return 2
    if ("fluent" in t or "fluently" in t) and "not fluent" not in t:
        return 5
    if "not fluent" in t:          # can speak, but not fluently
        return 3
    if "advanced" in t:
        return 5
    if "speak" in t:
        return 3
    if "intermediate" in t:
        return 3
    if "beginner" in t or "basic" in t or "elementary" in t:
        return 1
    return 0


def _cand_lang_rank(want: str) -> int:
    """Candidate's proficiency rank parsed from the drafted answer text; defaults to
    5 (fluent) when unstated — the target market is English-language remote US/CA roles,
    and the co-pilot draft is human-reviewed before any submit (never claims Native
    unless the answer says so)."""
    r = _lang_option_rank(str(want))
    return r or 5


def _looks_language_options(texts: list[str]) -> bool:
    """The popover options collectively read as a language-proficiency scale (so we pick
    by candidate level, NOT the first/worst option). Requires ≥2 ranked language rows and
    NO Yes/No pair (those are screeners, not a proficiency scale)."""
    ranked = [t for t in texts if _lang_option_rank(t)]
    lowered = {(t or "").strip().lower() for t in texts}
    if {"yes", "no"} & lowered and len(texts) <= 3:
        return False
    return len(ranked) >= 2


def _geo_shorten(want: str) -> list[str]:
    """Progressively-shorter geo queries for a typeahead that returned no options on the
    full string: 'Denver, Colorado, United States.' -> 'Denver, Colorado' -> 'Denver'.
    A geocode often fails on an over-qualified query (trailing punctuation, full country)
    but resolves the city alone."""
    base = str(want or "").strip().rstrip(" .,")
    parts = [p.strip() for p in base.split(",") if p.strip()]
    variants = [base] + [", ".join(parts[:k]) for k in range(len(parts) - 1, 0, -1)]
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


# An availability / start-date select whose options are durations, not the drafted prose
# ("I am available to start immediately" matches no option). A synthetic persona is available
# soonest, so pick the "immediately"/"now" option, else the first (these lists are ordered
# soonest-first by convention).
_AVAILABILITY_RE = re.compile(
    r"(?i)availab|when.{0,10}start|earliest.{0,10}(start|date)|notice period|start date")
_IMMEDIATE_RE = re.compile(
    r"(?i)immediat|right away|\basap\b|\bnow\b|available now|less than a week|within a week"
    r"|1 week|one week|1-2 week")


def _availability_pick_index(otexts: list[str]) -> int:
    """Index of the soonest availability option: an explicit immediate/now option, else 0
    (availability option lists are conventionally ordered soonest-first)."""
    for i, t in enumerate(otexts):
        if _IMMEDIATE_RE.search(t or ""):
            return i
    return 0


async def fill_comboboxes_known(page, known: dict) -> dict:
    """Fill Ashby-style input[role=combobox] dropdowns/typeaheads (What brought you…,
    Current Location) from explicit known answers: type the value, click the matching
    [role=option] (exact/prefix), else the first result (geo typeahead). These widgets
    are NOT native <select>, NOT Greenhouse .select__container, and NOT button groups —
    nothing else in the prefill path fills them. Demographics stay blank by policy.

    Language-proficiency comboboxes (Workable 'English Level', whose options are
    descriptive sentences like '…speak fluently' / 'Native', never the drafted prose)
    are matched by CANDIDATE LEVEL, not the first option — the first is the WORST
    ('cannot speak'), so the geo `opts[0]` fallback would silently claim no English."""
    filled: list[str] = []
    if not known:
        return {"filled": 0, "handled": []}
    norm = {_clean_text(k).strip(" *").lower(): v
            for k, v in known.items() if v and str(v).strip()}
    try:
        boxes = await page.query_selector_all(_COMBO_SEL)
    except Exception:
        return {"filled": 0, "handled": []}
    for box in boxes:
        try:
            v0 = await box.evaluate("el=>el.value")
            if v0 and v0.strip():
                continue  # already answered
            label = await _combo_label(box)
            key = _clean_text(label).strip(" *").lower()
            want = norm.get(key)
            # Demographics are handled by fill_demographics_decline (its own combobox pass) —
            # opening them in THIS known-answer flow disrupted the following comboboxes.
            if not want or _DEMOGRAPHIC.search(key):
                continue
            await box.click()
            await page.wait_for_timeout(300)
            try:
                readonly = bool(await box.evaluate(
                    "el=>el.readOnly || el.getAttribute('readonly')!=null"))
            except Exception:
                readonly = False
            # A readonly combobox is a FIXED-LIST select (Workable 'English Level'): typing
            # type-ahead-jumps to the wrong row and can filter the open listbox to empty, so
            # the old blind opts[0] fallback then picked the WORST option. Only type into a
            # real typeahead/geo input; for a readonly select just read the open listbox.
            if not readonly:
                await page.keyboard.type(str(want)[:60], delay=15)
                await page.wait_for_timeout(650)
            wl = str(want).strip().lower()
            opts = await page.query_selector_all("[role='option']")
            # Geo typeahead that returned nothing for the full string (over-qualified query:
            # trailing punctuation / full country name) — retry progressively shorter queries
            # ('Denver, Colorado, United States.' -> 'Denver, Colorado' -> 'Denver') until the
            # geocode resolves. Only for a real typeahead (not a readonly fixed list).
            if not readonly and not opts:
                for variant in _geo_shorten(want):
                    try:
                        await box.click()
                        await page.keyboard.press("Control+A")
                        await page.keyboard.press("Backspace")
                        await page.keyboard.type(variant[:60], delay=15)
                        await page.wait_for_timeout(650)
                        opts = await page.query_selector_all("[role='option']")
                    except Exception:
                        opts = []
                    if opts:
                        wl = variant.strip().lower()
                        break
            otexts = [((await o.inner_text()) or "").strip() for o in opts]
            opt = None
            for o, t in zip(opts, otexts):
                tl = t.lower()
                if tl == wl:
                    opt = o
                    break
                if opt is None and wl and tl.startswith(wl[:30]):
                    opt = o
            if opt is None and opts:
                if _looks_language_options(otexts):
                    # language-proficiency scale: pick the highest option that does not
                    # exceed the candidate's level (never the first/worst 'cannot speak').
                    cand = _cand_lang_rank(want)
                    best_i, best_r = None, -1
                    for i, t in enumerate(otexts):
                        r = _lang_option_rank(t)
                        if 0 < r <= cand and r > best_r:
                            best_i, best_r = i, r
                    if best_i is None:  # candidate below every option -> the lowest one
                        best_i = min(range(len(otexts)),
                                     key=lambda i: _lang_option_rank(otexts[i]) or 99)
                    opt = opts[best_i]
                elif readonly and _AVAILABILITY_RE.search(key):
                    # availability/start-date select: the drafted answer is prose that
                    # matches no option — pick the soonest (immediate) one truthfully.
                    opt = opts[_availability_pick_index(otexts)]
                elif not readonly:
                    opt = opts[0]  # geo/typeahead: best first result
                # readonly non-language select with no match -> leave for the human
                # (Escape below) rather than pick an arbitrary wrong row.
            if opt is None:
                await page.keyboard.press("Escape")
                continue
            await opt.click()
            await page.wait_for_timeout(250)
            v = await box.evaluate("el=>el.value")
            if v and v.strip():
                filled.append(label[:50])
        except Exception as e:
            logger.debug("combobox known-fill failed: %s", e)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
    if filled:
        logger.info("combobox known-filled %d: %s", len(filled), filled)
    return {"filled": len(filled), "handled": filled}


async def list_unanswered_react_selects(page) -> list[str]:
    """Cleaned labels of react-selects still showing the placeholder. The analyzer
    skips their inner typeahead inputs, so without this scan the report and the
    submit gate would never see an unanswered required dropdown (e.g. non-draft
    runs, or a harvest/apply failure)."""
    out: list[str] = []
    try:
        containers = await page.query_selector_all(".select__container")
    except Exception:
        return out
    for c in containers:
        try:
            if await c.query_selector(".select__single-value") or await c.query_selector(".select__multi-value"):
                continue  # single OR a filled multi (>=1 chip) is answered — not "unanswered"
            label_el = await c.query_selector("label, .select__label")
            label = (await label_el.inner_text()).strip() if label_el else ""
            q = _clean_text(label).strip(" *")[:200]
            if len(q) >= 4 and not _DEMOGRAPHIC.search(q):
                out.append(q)  # demographics stay blank by policy — not "unfilled"
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# D3/D4 — Ashby-style <button> toggle groups (Yes/No screeners)
# ---------------------------------------------------------------------------

# Action/navigation button texts that must never be treated as answer options.
_BTN_EXCLUDE = re.compile(
    r"(?i)(submit|apply|next|continue|back|cancel|upload|attach|add|remove|"
    r"clear|save|sign|log)")

MAX_BTN_TEXT = 30      # answer options are short (Yes / No / Maybe / 0-1 years)
MIN_GROUP, MAX_GROUP = 2, 5

# Collect candidate toggle groups: sibling <button> runs under a question label.
# Stamps every group member with data-aa-btn="<seq>" so Python gets a stable,
# unique selector per button. Exclusion by TEXT happens in Python (testable).
_HARVEST_BTN_JS = """
() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const groups = new Map();
  for (const btn of document.querySelectorAll('button')) {
    if (!visible(btn)) continue;
    if ((btn.getAttribute('type') || '').toLowerCase() === 'submit') continue;
    if (btn.closest('nav, header, footer, [role="navigation"], [class*="cookie"], [id*="cookie"]')) continue;
    const txt = (btn.textContent || '').trim();
    if (!txt || txt.length >= %(max_text)d) continue;
    const p = btn.parentElement;
    if (!p) continue;
    if (!groups.has(p)) groups.set(p, []);
    groups.get(p).push(btn);
  }
  window.__aaBtnSeq = window.__aaBtnSeq || 0;
  const out = [];
  for (const [container, btns] of groups) {
    if (btns.length < %(min_n)d || btns.length > %(max_n)d) continue;
    // a toggle group is ONLY buttons — mixed containers are toolbars/menus
    const allBtns = Array.from(container.children).filter(ch => ch.tagName === 'BUTTON');
    if (allBtns.length !== btns.length) continue;
    // skip groups already answered (prior run / replay)
    const answered = btns.some(b =>
      b.getAttribute('aria-pressed') === 'true' || b.getAttribute('aria-checked') === 'true' ||
      b.getAttribute('aria-selected') === 'true' ||
      /(^|[_\\s-])(selected|active|checked)/i.test(b.className || ''));
    if (answered) continue;
    // nearest preceding question label: climb ancestors, scan preceding siblings
    let q = '';
    let node = container;
    for (let depth = 0; depth < 6 && node && !q; depth++) {
      let sib = node.previousElementSibling;
      while (sib && !q) {
        let cand = null;
        if (sib.matches('label, legend, [class*="question"], [class*="label"]')) cand = sib;
        else cand = sib.querySelector('label, legend, [class*="question"], [class*="label"]');
        if (cand) {
          const t = (cand.textContent || '').trim();
          // skip Ashby's 'ashby-application-form-question-description' sub-line (its class
          // contains 'question' so it would win over the real title) — keep scanning to the
          // actual question-title, else drafted_answers (keyed by title) never replays.
          if (t.length > 10 && !/description/i.test(cand.className || '')) q = t;
        }
        sib = sib.previousElementSibling;
      }
      node = node.parentElement;
    }
    if (!q) continue;
    const opts = [], sels = [];
    for (const b of btns) {
      const i = window.__aaBtnSeq++;
      b.setAttribute('data-aa-btn', String(i));
      opts.push((b.textContent || '').trim());
      sels.push('[data-aa-btn="' + i + '"]');
    }
    out.push({question_text: q, options: opts, selectors: sels});
  }
  return out;
}
""" % {"max_text": MAX_BTN_TEXT, "min_n": MIN_GROUP, "max_n": MAX_GROUP}


def shape_button_group(raw: dict) -> dict | None:
    """Pure shaping/validation of one harvested button group.

    Drops action/nav buttons (_BTN_EXCLUDE) keeping options/selectors aligned;
    requires a real question (len > 10 after cleaning) and 2-5 surviving short
    options. Returns {question_text, options, selectors} or None."""
    question = _clean_text(raw.get("question_text") or "").strip(" *")
    if len(question) <= 10 or _DEMOGRAPHIC.search(question):
        return None
    options, selectors = [], []
    for o, s in zip(raw.get("options") or [], raw.get("selectors") or []):
        o = (o or "").strip()
        if not o or len(o) >= MAX_BTN_TEXT or _BTN_EXCLUDE.search(o) or not s:
            continue
        options.append(o)
        selectors.append(s)
    if not MIN_GROUP <= len(options) <= MAX_GROUP:
        return None
    return {"question_text": question[:200], "options": options,
            "selectors": selectors}


async def harvest_button_groups(page) -> list[dict]:
    """Find Ashby-style toggle groups (2-5 short sibling <button>s under a question
    label) and return them as closed questions:
    [{question_text, options: [str], selectors: [one unique selector per button]}].
    Read-only apart from stamping data-aa-btn attributes."""
    try:
        raw = await page.evaluate(_HARVEST_BTN_JS)
    except Exception as e:
        logger.debug("button-group harvest failed: %s", e)
        return []
    out = [g for g in (shape_button_group(r) for r in raw or []) if g]
    if out:
        logger.info("button groups harvested %d closed questions: %s",
                    len(out), [g["question_text"][:40] for g in out])
    return out


async def apply_button_choice(page, selector: str) -> bool:
    """Click one toggle button (selector from harvest_button_groups). Returns True
    when the button reports a selected state (aria-pressed/checked/selected or a
    *selected*/*active* class — Ashby sets a css-module `_option_..._selected`
    class) or, best effort, when the click itself succeeded."""
    try:
        btn = page.locator(selector).first
        await btn.click(timeout=4000)
        await page.wait_for_timeout(250)
        try:
            state = await btn.evaluate(
                "b => b.getAttribute('aria-pressed') === 'true'"
                " || b.getAttribute('aria-checked') === 'true'"
                " || b.getAttribute('aria-selected') === 'true'"
                " || /(^|[_\\s-])(selected|active|checked)/i.test(b.className || '')")
            if state:
                return True
            logger.debug("button %s clicked but no selected state detected", selector)
        except Exception:
            pass
        return True  # clicked without error; some widgets expose no state attr
    except Exception as e:
        logger.debug("button choice click failed for %s: %s", selector, e)
        return False


# Workable renders a YES/NO screener as a <fieldset> of <div role="radio"> options with a
# hidden <input type=radio> — NOT <button> (harvest_button_groups misses it) and the hidden
# input is invisible to the analyzer, so nothing else fills these. Harvest [role=radio] groups
# with their question label and a click selector per option.
_HARVEST_ROLE_RADIO_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const groups = new Map();
  for (const r of document.querySelectorAll('[role=radio]')) {
    const p = r.parentElement; if (!p) continue;
    if (!groups.has(p)) groups.set(p, []);
    groups.get(p).push(r);
  }
  window.__aaRrSeq = window.__aaRrSeq || 0;
  const out = [];
  for (const [par, rs] of groups) {
    if (rs.length < 2 || rs.length > 5) continue;
    if (rs.some(r => r.getAttribute('aria-checked') === 'true')) continue;  // already answered
    let q = '', node = par;
    for (let d = 0; d < 6 && node && !q; d++) {
      const lab = node.querySelector ? node.querySelector('[id$="_label"], label, legend') : null;
      if (lab) { const t = clean(lab.textContent); if (t.length > 6) q = t; }
      if (!q) { let sib = node.previousElementSibling;
        while (sib && !q) { const t = clean(sib.textContent);
          if (t.length > 6 && t.length < 90) q = t; sib = sib.previousElementSibling; } }
      node = node.parentElement;
    }
    if (!q) continue;
    const opts = [], sels = [];
    for (const r of rs) {
      const i = window.__aaRrSeq++;
      r.setAttribute('data-aa-rr', String(i));
      opts.push(clean(r.textContent));
      sels.push('[data-aa-rr="' + i + '"]');
    }
    out.push({question: q, options: opts, selectors: sels});
  }
  return out;
}
"""


async def fill_role_radio_known(page, known: dict) -> dict:
    """Fill Workable-style [role=radio] toggle groups (YES/NO screeners) from known answers:
    click the option whose text matches the drafted answer. Skips demographics (declined
    elsewhere). Isolated from the analyzer/combobox paths; only touches [role=radio] groups
    whose question matches a drafted key."""
    if not known:
        return {"filled": 0, "handled": []}
    norm = {_clean_text(k).strip(" *").lower(): str(v).strip().lower()
            for k, v in known.items() if v and str(v).strip()}
    try:
        groups = await page.evaluate(_HARVEST_ROLE_RADIO_JS)
    except Exception as e:
        logger.debug("role-radio harvest failed: %s", e)
        return {"filled": 0, "handled": []}
    filled: list[str] = []
    for g in groups or []:
        key = _clean_text(g.get("question") or "").strip(" *").lower()
        want = norm.get(key)
        if not want or _DEMOGRAPHIC.search(key):
            continue
        for opt, sel in zip(g.get("options") or [], g.get("selectors") or []):
            ol = (opt or "").strip().lower()
            if not ol:
                continue
            if ol == want or ol.startswith(want[:6]) or want.startswith(ol[:6]):
                try:
                    await page.locator(sel).first.click(timeout=3000)
                    await page.wait_for_timeout(150)
                    filled.append(key[:40])
                except Exception:
                    pass
                break
    if filled:
        logger.info("role-radio known-filled %d: %s", len(filled), filled)
    return {"filled": len(filled), "handled": filled}
