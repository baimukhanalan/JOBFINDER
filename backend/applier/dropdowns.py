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
    r"(?i)(gender|race|ethnicit|veteran|disabilit|demographic|hispanic|latin[ox]?\b|"
    r"pronoun|sexual orientation|transgender|lgbtq)")


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
            if await c.query_selector(".select__single-value"):
                continue  # already answered
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
        # type to filter; 60 chars is enough to disambiguate and keeps typing fast
        await page.keyboard.type(option_text[:60], delay=15)
        # poll for options to render (remote search is slow; a fixed wait dropped hits)
        opts = []
        for _ in range(8):
            await page.wait_for_timeout(250)
            opts = await page.query_selector_all(".select__option")
            if opts:
                break
        want = option_text.strip().lower()
        opt = None
        for o in opts:
            t = (await o.inner_text()).strip().lower()
            if t == want:
                opt = o
                break
            if opt is None and t.startswith(want[:40]):
                opt = o
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


async def _resolve_multi_options(page, container_index: int, joined_value: str) -> list[str]:
    """For a 'select all that apply' react-select: open it, read the real option texts, and
    return those that appear (as a substring) in the known joined value. Substring — never a
    naive comma-split — because legit options contain commas ('Yes, as a full-time employee')."""
    want = (joined_value or "").lower()
    picks: list[str] = []
    try:
        containers = await page.query_selector_all(".select__container")
        if not 0 <= container_index < len(containers):
            return []
        control = await containers[container_index].query_selector(".select__control")
        if not control:
            return []
        await control.click()
        for _ in range(8):
            await page.wait_for_timeout(250)
            opts = await page.query_selector_all(".select__option")
            if opts:
                break
        for o in opts:
            t = (await o.inner_text()).strip()
            if t and t.lower() in want and t not in picks:
                picks.append(t)
        await page.keyboard.press("Escape")
    except Exception as e:
        logger.debug("multi-option resolve failed at #%d: %s", container_index, e)
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
    return picks


async def apply_react_select_multi(page, container_index: int, option_texts: list[str]) -> bool:
    """Pick MULTIPLE options in a react-select ('select all that apply'). React-select multi
    keeps the menu open and re-filters after each pick; confirm via >=1 .select__multi-value
    chip (a multi container never shows .select__single-value)."""
    try:
        containers = await page.query_selector_all(".select__container")
        if not 0 <= container_index < len(containers):
            return False
        c = containers[container_index]
        control = await c.query_selector(".select__control")
        if not control:
            return False
        await control.click()
        for want in option_texts:
            await page.wait_for_timeout(150)
            await page.keyboard.type(want[:60], delay=12)
            picked = None
            for _ in range(8):
                await page.wait_for_timeout(200)
                opts = await page.query_selector_all(".select__option")
                if opts:
                    break
            wl = want.strip().lower()
            for o in opts:
                if ((await o.inner_text()) or "").strip().lower() == wl:
                    picked = o
                    break
            if picked:
                await picked.click()
                await page.wait_for_timeout(150)
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
                # "select all that apply": the known value is a comma-joined string, but each
                # real option is individual — resolve wanted options by SUBSTRING (never a raw
                # split, since legit options contain commas) and pick each chip.
                wants = await _resolve_multi_options(page, idx, str(val))
                if wants and await apply_react_select_multi(page, idx, wants):
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


async def fill_comboboxes_known(page, known: dict) -> dict:
    """Fill Ashby-style input[role=combobox] dropdowns/typeaheads (What brought you…,
    Current Location) from explicit known answers: type the value, click the matching
    [role=option] (exact/prefix), else the first result (geo typeahead). These widgets
    are NOT native <select>, NOT Greenhouse .select__container, and NOT button groups —
    nothing else in the prefill path fills them. Demographics stay blank by policy."""
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
            if not want or _DEMOGRAPHIC.search(key):
                continue
            await box.click()
            await page.wait_for_timeout(250)
            await page.keyboard.type(str(want)[:60], delay=15)
            await page.wait_for_timeout(650)
            wl = str(want).strip().lower()
            opts = await page.query_selector_all("[role='option']")
            opt = None
            for o in opts:
                t = ((await o.inner_text()) or "").strip().lower()
                if t == wl:
                    opt = o
                    break
                if opt is None and wl and t.startswith(wl[:30]):
                    opt = o
            if opt is None and opts:
                opt = opts[0]  # geo/typeahead: best first result
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
            if await c.query_selector(".select__single-value"):
                continue
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
