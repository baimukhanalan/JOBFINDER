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
    # Name fields. '^name\b' (not just '^name$'): Ashby labels the full-name field
    # bare "Name" (field name '_systemfield_name'), which matched NOTHING after the
    # display-text cleanup -> candidate name left empty on every Ashby run. Anchored
    # at the start so "Company name"/"Manager name" stay out, and "Name of ..."
    # (referrer/employer) is excluded explicitly.
    (r"(?i)(full.?name|your.?name|^name\b(?![\s_-]+of\b)|applicant.?name|candidate.?name|legal.?name|_systemfield_name\b)", "full_name", "fill"),
    (r"(?i)(first.?name|given.?name|fname)", "_first_name", "fill"),
    (r"(?i)(last.?name|family.?name|surname|lname)", "_last_name", "fill"),

    # Contact
    (r"(?i)(e.?mail|email.?address|your.?email)", "email", "fill"),
    (r"(?i)(phone|mobile|cell|telephone|tel$)", "phone", "fill"),

    # Work-auth / sponsorship — checked BEFORE location so a sponsorship question that
    # mentions "location" (e.g. "...sponsorship to remain in your location?") isn't mis-read.
    (r"(?i)((require|need|will you|provide|commence|seek|obtain|maintain).{0,80}sponsor|sponsor.{0,30}(require|need|visa|immigration|employ)|visa sponsor|immigration (case|assistance|support|sponsorship))", "_sponsorship", "select_or_fill"),
    (r"(?i)((authori[sz]\w*).{0,15}work|work.{0,15}(authori[sz]\w*)|legally.?(auth|eligible)|right.?to.?work|eligible.?to.?work)", "_work_auth", "select_or_fill"),
    # Non-compete / restrictive agreement preventing this job -> No.
    (r"(?i)(non.?compete|non.compete)", "_no", "select_or_fill"),
    # I-9 status attestations with a clear universal answer (nuanced ones like
    # "admitted under a nonimmigrant visa" are deliberately left for the human).
    (r"(?i)(alien (illegally|unlawfully|residing unlawfully)|illegally.{0,20}united states|renounced.{0,20}citizenship)", "_no", "select_or_fill"),

    # --- fact-sheet driven (rules v2): values come from backend/data/facts/<profile>.json.
    # A missing fact resolves to None -> the question stays "unknown" and is handled by
    # the constrained-choice LLM (flagged [review]) or the human. Never guessed here.
    (r"(?i)(night.?shift|overnight.{0,15}(shift|work|hour)|graveyard|work (at )?nights)", "_fact:shifts_nights", "select_or_fill"),
    (r"(?i)(work\s+(on\s+)?(weekend|saturday|sunday)|(weekend|saturday|sunday).{0,25}(shift|work|hour|availab|schedul))", "_fact:shifts_weekends", "select_or_fill"),
    (r"(?i)(overtime|extra hours)", "_fact:overtime", "select_or_fill"),
    (r"(?i)(time.?zone)", "_fact:timezone", "fill"),
    (r"(?i)(typing.?(speed|test)|\bwpm\b|words.?per.?minute)", "_fact:typing_wpm", "fill"),
    (r"(?i)(languages?.{0,30}(speak|fluent|proficien)|(spoken|primary).{0,10}languages?|languages?.{0,15}skill|bilingual|multilingual)", "_fact:languages", "fill"),
    (r"(?i)(highest.{0,25}education|education.{0,12}level|highest.{0,12}degree)", "_fact:education_level", "select_or_fill"),
    (r"(?i)(hourly.?(rate|pay|wage)|pay.{0,10}per.?hour|rate.?per.?hour)", "_fact:salary_hourly", "fill"),
    (r"(?i)(drug.?(test|screen))", "_fact:drug_test_ok", "select_or_fill"),
    (r"(?i)(driver'?s?.?licen[sc]e)", "_fact:drivers_license", "select_or_fill"),
    (r"(?i)((quiet|dedicated|distraction.?free).{0,25}(work.?space|work.?area|home.?office))", "_fact:quiet_workspace", "select_or_fill"),

    # Location. `city` is word-bounded: bare `city` substring-matched INSIDE
    # "Ethni(city)" (and capacity/authenticity/velocity/…), so a "Race/Ethnicity"
    # demographic radio group resolved to _location — checked BEFORE the demographic
    # _skip rule below — and leaked into unknown_questions as a false "1 left for the
    # human" instead of being silently skipped like Gender/Veteran/Disability.
    (r"(?i)(\bcity\b|location|where.?located|current.?location|address)", "_location", "fill"),
    # "state" as a NOUN only: "Please state your salary..." is a verb -> must fall through
    (r"(?i)(\bstate\b(?!\s+(your|the|a|an|any|why|how|what|wh))(?! .*authoriz)|\bprovince\b)", "_state", "fill"),
    (r"(?i)(zip|postal|postcode)", "_zip", "fill"),
    (r"(?i)(country)", "_country", "fill"),

    # Resume/CV upload. Word-bounded with underscore-aware lookarounds: bare 'cv'
    # lives inside Workable's opaque ids ('elQuzcvxczyuwMep') and matched EVERY such
    # field (22 thirty-second set_input_files timeouts live), while real signals are
    # often underscore-joined ('_systemfield_resume', 'resume_upload') where \b
    # never fires ('_' is a word char).
    (r"(?i)((?<![a-z0-9])resume(?![a-z0-9])|(?<![a-z0-9])cv(?![a-z0-9])|curriculum\s+vitae)", "_resume", "upload"),

    # Cover letter
    (r"(?i)(cover.?letter|letter.?of.?interest)", "_cover_letter", "fill"),

    # LinkedIn
    (r"(?i)(linkedin)", "_linkedin", "fill"),

    # Experience
    (r"(?i)(years?.?of?.?experience|experience.?years|how.?many.?years)", "_experience", "fill"),

    # Salary
    (r"(?i)(salary|compensation|pay.?expect|desired.?pay)", "_salary", "fill"),

    # Start date / availability
    # `start.?date` is negative-lookahead-guarded against a work-history date COMPONENT
    # ('Start date month'/'Start date year' in a Greenhouse Employment block): the
    # availability value ('Immediately') otherwise typed prose into a year field ('Imme').
    (r"(?i)(start.?date(?!\s+(?:month|year))|available.?to.?start|earliest.?start|when.?can.?you.?start|availability)", "_start_date", "fill"),
    (r"(?i)(notice.?period|how (much|long) notice)", "_fact:notice_period", "fill"),

    # Remote / relocation
    (r"(?i)(willing.?to.?relocate|relocation|open.?to.?relocation)", "_no", "select_or_fill"),
    (r"(?i)(remote|work.?from.?home|wfh|work.?location.?pref)", "_yes", "select_or_fill"),

    # How did you hear
    (r"(?i)(how.?did.?you.?(hear|find|learn)|source|referr)", "_how_heard", "fill"),

    # Common BPO/screener yes/no (factual; human still reviews before submit)
    (r"(?i)(at least 18|over 18|18.?years.?(or|of)|are you 18)", "_yes", "select_or_fill"),
    (r"(?i)(convicted|conviction|felony|misdemeanor|criminal.?(history|record))", "_fact:criminal_record", "select_or_fill"),
    (r"(?i)(background.?check|consent.*screen)", "_fact:background_check_ok", "select_or_fill"),
    (r"(?i)(previously.*(work|employ|consult)|former(ly)?.*employ|currently.*employed.*(by|at)|worked (at|for) (us|this))", "_no", "select_or_fill"),
    (r"(?i)(reliable.*(computer|internet)|have.*(computer|laptop|headset|webcam)|high.?speed internet|own equipment|wired.*internet)", "_yes", "select_or_fill"),

    # Website / portfolio
    (r"(?i)(website|portfolio|personal.?url|github)", "_skip", None),

    # Gender/demographics (optional, skip)
    # keep in sync with dropdowns._DEMOGRAPHIC — a narrower list left 'sexual orientation'/
    # 'pronouns'/'hispanic' etc. flowing into the fill+unfilled path (wrongly "1 left for human").
    (r"(?i)(gender|rac(e|ial)|ethnic|veteran|military|armed\s*forces|disabilit|demographic|hispanic|latin[ox]?\b(?!\s*americ)|pronoun|sexual\s*orientation|transgender|lgbtq|neurodiverg|your (?:current )?age\b|age (?:range|group|bracket)|date of birth|\bdob\b)", "_skip", None),
]

# Optional social / profile / URL fields — skipped entirely when NOT required, so a demo
# persona's invented LinkedIn/Twitter/Telegram/etc. profile isn't typed into a field nobody
# asked for. A REQUIRED one (red *) is still filled with the persona's value.
_OPTIONAL_SOCIAL = re.compile(
    r"(?i)\b(linkedin|personal\s*(?:url|site|website|page)|portfolio|"
    r"twitter|github|gitlab|telegram|whatsapp|facebook|instagram|dribbble|behance|"
    r"stack\s*overflow|social\s*(?:media|profile|link)|"
    r"other\s*(?:url|link|website|profile|social)|(?<!company )website)\b")

# Submit button patterns
SUBMIT_PATTERNS = [
    r"(?i)(submit.?application|apply.?now|apply.?for|submit$|apply$|send.?application|submit.?resume)",
    r"(?i)(continue|next|proceed)",
]


# Narrative/open-ended questions: these want a written answer, never a rule value.
# Routed to the LLM draft path instead of being matched to (and filled with) Yes/No,
# a location, etc. — e.g. "Briefly describe your experience working remotely" must NOT
# match the `remote -> Yes` rule. Kept narrow so "how many years"/"how did you hear"
# (legit short fills) still match their rules.
_OPEN_ENDED = re.compile(
    r"(?i)\b(describe|explain|tell us|why (are|do|did|would|should|should)|"
    r"give (an |a )?example|share (a|an|your|some)|walk (us|me) through|"
    r"in your own words|elaborate|what (makes|motivates|interests|excites|drew)|"
    r"how would you (handle|approach|deal)|please (share|provide).{0,30}(example|time|accomplish))\b")


# A work-auth question that names a NON-US country must not be auto-answered "Yes":
# the candidate is US (some CA) authorized only. Claiming authorization in Ireland/UK/etc.
# would be a false statement -> route to the human instead.
_FOREIGN_AUTH = re.compile(
    r"(?i)\b(?:work in|authori[sz]ed in|right to work in|reside in|live in)\s+"
    r"(?:the\s+)?(ireland|uk|united kingdom|england|scotland|australia|new zealand|"
    r"germany|france|spain|netherlands|poland|portugal|romania|india|philippines|singapore|"
    r"malaysia|europe|the eu|eu\b|emea|brazil|mexico)")


# Short-label-only fills: these belong on a compact field label ("City", "Current location"),
# NOT inside a long yes/no question that merely happens to contain the word. e.g. "...are you
# able to work near our Cork office? Cork office address:..." must not fill the location.
_LABEL_ONLY_KEYS = {"_location", "_state", "_zip", "_country", "_salary", "_how_heard", "_start_date"}
_YESNO_START = re.compile(r"(?i)^\s*(are|do|does|did|can|could|will|would|have|has|is|if)\b")

# Generic input placeholders ("Type your response", "Select…") carry NO question — they are
# UI noise. Keeping them out of the human-question text (display_text) matters: Lever puts
# "Type your response" on every custom field, and its word "your" created a false 2-word
# fuzzy hit onto "What is your nationality?", cross-binding a COUNTRY into a date field
# ("earliest date … to start"). match_text still keeps them for rule signals.
_GENERIC_PLACEHOLDER = re.compile(
    r"(?i)^(type (your )?(response|answer|here)|enter (a |your )?(response|answer|value|text)"
    r"|your (response|answer)|please (type|select|choose|specify)|select(\s+an?\s+\w+| ?\.\.\.| one)?"
    r"|choose(\s+an?\s+\w+| ?\.\.\.)?|start typing|—|-)\.?$")

# A text input with a date-mask placeholder is a DATEPICKER widget: typing prose
# into it produces garbage (live Workable turned 'Immediately' into '03/05/4583').
_DATE_MASK = re.compile(r"(?i)\b(?:mm|dd)\s*[/.-]\s*(?:mm|dd)\s*[/.-]\s*y{2,4}\b"
                        r"|\byyyy-mm-dd\b")
# A "Pick date…" / "Select a date" placeholder is ALSO a datepicker even without a
# mm/dd/yyyy mask (Ashby's react-datepicker renders exactly this) — the class/wrapper
# check in extract_form_fields is the primary signal; this catches it label-side too.
_DATE_PLACEHOLDER = re.compile(r"(?i)\b(?:pick|select|choose|enter)\s+(?:a\s+)?date\b"
                               r"|\bdate\s*\.{2,}")


def _looks_like_date(v) -> bool:
    """A value that could be a real date (M/D/Y, ISO, etc.) rather than prose. Used to
    reject an open-text known answer that was mis-routed to a datepicker widget."""
    return bool(re.search(r"\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}", str(v or "")))


def _looks_like_question(text: str) -> bool:
    t = (text or "").strip()
    return (("?" in t) and len(t) > 55) or bool(_YESNO_START.match(t))


# Internal ATS noise that poisons question text: UUIDs (Ashby), cards[...] (Lever),
# QA_/CA_/SQ_ + bare numeric ids (Workable), question_N (Greenhouse), opaque field
# tokens, and input placeholders. Stripped from DISPLAY text only — raw match_text
# keeps name/id signals for rule matching (e.g. name="email").
_NOISE = re.compile(
    r"(?ix)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"  # uuid
    r"|\bcards\[[^\]]*\](?:\[[^\]]*\])?"                                       # lever internal
    r"|\b(?:qa|ca|sq)_\d+\b"                                                   # workable ids
    r"|\bquestion_\d+\b|\b\d{5,}\b"                                            # numeric ids
    r"|\b(?=[a-z]*\d)[a-z0-9]{14,}\b"                                          # opaque tokens (digit required)
    r"|\bselect\s*(?:\.{2,}|…)"                                                # 'Select...' sentinel ONLY
    r"|\b(type\s+here|mm/dd/yyyy|dd/mm/yyyy|required)(?:\.{1,3}|\b)")          # placeholders


_REQ_MARK = str.maketrans({"✱": " ", "＊": " ", "*": " "})


def _clean_text(text: str) -> str:
    """Human question text only: strip internal ids/placeholders, collapse repeats."""
    # Strip the required-marker glyph (✱/＊/*) the SAME way drafts_ui._clean_label does when
    # it keys drafted answers — else a live label 'Language 1✱' never matches the 'Language 1'
    # key and every required field silently drops its drafted answer.
    t = _NOISE.sub(" ", (text or "").translate(_REQ_MARK))
    t = re.sub(r"\s+", " ", t).strip()
    # A+A duplication ('Which shift? Which shift?' from label==nearbyText) -> keep one
    words = t.split(" ")
    half = len(words) // 2
    if half >= 2 and len(words) % 2 == 0 and words[:half] == words[half:]:
        t = " ".join(words[:half])
    # A bare 'Select'/'Select one' is a dropdown placeholder, not a question:
    # route it through the length fallback below. The VERB inside a sentence
    # ('Please select your preferred shift') must never be stripped.
    if t.lower() in ("select", "select one"):
        t = ""
    return t if len(t) >= 4 else (text or "").strip()


def _match_field(text: str) -> tuple[str, str] | None:
    """Match field text against known patterns. Returns (profile_key, action) or None."""
    if _OPEN_ENDED.search(text):
        return None  # narrative question -> open question (LLM-drafted), not a rule fill
    for pattern, key, action in FIELD_PATTERNS:
        if re.search(pattern, text):
            if key == "_work_auth" and _FOREIGN_AUTH.search(text):
                return None  # don't falsely claim authorization in a non-US country
            if key in _LABEL_ONLY_KEYS and _looks_like_question(text):
                return None  # a long/yes-no question, not a short location/salary field
            return key, action
    return None


def _field_selector(el_id: str, el_name: str, aria_label: str,
                    el_type: str = "", el_value: str = "", nth: int = 0,
                    data_qa: str = "") -> str:
    """Build a Playwright selector for a form element. Attribute selector for id
    (not "#id"): Ashby/Workday custom-question ids are bare UUIDs that start with
    a digit, which is an invalid CSS id selector and made those fields unfillable.

    Radio/checkbox: name+value (or an nth index among same-name value-less
    options) ALWAYS beats id —
      - Workable regenerates random ids ('elQuzcvxczyuwMep') on every React
        render and RE-RENDERS the form after the résumé upload is parsed, so
        ids captured at analysis time are stale by fill time (live failure);
      - Ashby gives every option of a group the SAME id (id==name), so an id
        selector always hit the first option.
    name+value are semantic, unique within the group, and survive re-renders.
    Embedded quotes skip the branch (same policy as the id branch).
    """
    if el_type in ("radio", "checkbox") and el_name:
        if el_value and '"' not in el_value:
            return f'[name="{el_name}"][value="{el_value}"]'
        return f'[name="{el_name}"] >> nth={nth}'
    if el_id and '"' not in el_id:
        return f'[id="{el_id}"]'
    if el_name:
        return f'[name="{el_name}"]'
    if aria_label:
        return f'[aria-label="{aria_label}"]'
    if data_qa and '"' not in data_qa:
        # Lever's candidate-location <select data-qa="candidate-location-select"> has no
        # id/name/aria-label — without this it was dropped and the location never filled.
        return f'[data-qa="{data_qa}"]'
    return ""


def _group_entry(ftype: str, members: list[dict]) -> dict:
    """Merge radio/checkbox option fields into ONE logical question field.
    Option `value` holds the SELECTOR of that input — checking it answers the question.
    """
    options = []
    for i, m in enumerate(members):
        text = (m["label"] or m["ariaLabel"] or m.get("optionLabel")
                or m.get("value") or "").strip()
        options.append({"value": m["selector"], "text": text or f"[option {i + 1}]"})
    question = next((m["nearbyText"] for m in members if m["nearbyText"]), "") \
        or members[0]["label"]
    return {**members[0],
            "type": f"{ftype}_group",
            "label": question,
            "nearbyText": question,
            "options": options}


def _merge_by_key(items: list[dict], mergeable, keyfn) -> list[dict]:
    """Merge radio/checkbox fields that share keyfn(f) into one group entry,
    keeping the DOM position of the first member. Non-mergeable items pass through."""
    by_key: dict = {}
    for i, f in enumerate(items):
        if mergeable(f):
            by_key.setdefault(keyfn(f), []).append(i)
    merged: list[dict] = []
    consumed: set[int] = set()
    for i, f in enumerate(items):
        if i in consumed:
            continue
        idxs = by_key.get(keyfn(f), []) if mergeable(f) else []
        if len(idxs) >= 2:
            consumed.update(idxs)
            merged.append(_group_entry(f["type"], [items[j] for j in idxs]))
        else:
            merged.append(f)
    return merged


def _merge_radio_groups(fields: list[dict]) -> list[dict]:
    """Collapse radio (and multi-checkbox) inputs that share a `name` into ONE logical
    question with options, so the choice engine can answer them like a select.
    The merged question keeps the DOM position of its first member.

    Pass 2 (Workable): options can be rendered as inputs with a DISTINCT name each
    inside ONE explicit ARIA group container (fieldset[role=radiogroup] /
    div[role=group]) — that container IS the question, so its members merge,
    checkboxes included ("Open to relocate" Yes/No pairs are checkboxes there).

    Pass 3 (fallback): remaining single radios sharing identical non-empty
    nearbyText (the group question) merge too. Checkboxes stay out of this pass:
    same-nearby checkboxes WITHOUT a declared ARIA group are commonly
    independent consents.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    order: list[dict | tuple[str, str]] = []  # field dicts, or a group key placeholder
    for f in fields:
        if f["type"] in ("radio", "checkbox") and f["name"]:
            gkey = (f["type"], f["name"])
            if gkey not in groups:
                groups[gkey] = []
                order.append(gkey)
            groups[gkey].append(f)
        else:
            order.append(f)

    out: list[dict] = []
    for item in order:
        if isinstance(item, dict):
            out.append(item)
            continue
        members = groups[item]
        if len(members) < 2:
            out.extend(members)
            continue
        out.append(_group_entry(item[0], members))

    # pass 2: distinct-name options inside one explicit ARIA group container
    out = _merge_by_key(
        out,
        lambda f: f["type"] in ("radio", "checkbox") and f.get("groupKey"),
        lambda f: (f["type"], f.get("groupKey", "")))

    # pass 3: distinct-name single radios sharing the same question text
    return _merge_by_key(
        out,
        lambda f: f["type"] == "radio" and f.get("nearbyText"),
        lambda f: f.get("nearbyText", ""))


def _resolve_value(key: str, profile: dict, cover_letter: str, known_answers: dict,
                   facts: dict | None = None) -> str | None:
    """Resolve a matched key to an actual value."""
    if key.startswith("_fact:"):
        v = (facts or {}).get(key[len("_fact:"):])
        if isinstance(v, bool):
            return "Yes" if v else "No"
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        return str(v) if v not in (None, "") else None
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
        return profile.get("location") or "Remote"
    if key == "_state":
        return profile.get("state", "")
    if key == "_zip":
        return profile.get("zip_code", "")
    if key == "_country":
        return profile.get("country") or "United States"
    if key == "_cover_letter":
        return cover_letter or ""
    if key == "_linkedin":
        return profile.get("linkedin_url", "")
    if key == "_experience":
        return str(profile.get("years_experience") or "")
    if key == "_salary":
        return profile.get("desired_salary", "")  # usually skipped
    if key == "_work_auth":
        wa = (profile.get("work_authorization") or "").strip().lower()
        return "No" if (wa.startswith("no") or "not authorized" in wa) else "Yes"
    if key == "_sponsorship":
        ns = str(profile.get("needs_sponsorship") or "No").strip().lower()
        return "Yes" if ns in ("yes", "true", "1") else "No"
    if key == "_start_date":
        # A concrete near-future date answers "when can you start?" everywhere — a free-text
        # box AND a react-datepicker (which can't accept the prose 'Immediately'). Honor an
        # explicit availability the person set (unless it's vague like 'Immediately'/'ASAP').
        v = (profile.get("available_start") or "").strip()
        if v and not re.search(r"(?i)immediate|asap|flexible|negotiable|\bnow\b|any", v):
            return v
        from datetime import date, timedelta
        return (date.today() + timedelta(days=21)).strftime("%m/%d/%Y")
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


# Shared JS: text of a node WITHOUT svg junk (Workable labels embed an <svg> whose
# <desc> says "SVGs not supported by this browser." — that must never leak into a
# label), and aria-labelledby resolution (Workable's only link between an input and
# its human question text: <input aria-labelledby="CA_21645_label"> -> <span
# id="CA_21645_label">Desired salary range per month</span>).
_JS_HELPERS = """
    const txt = (n) => {
        if (!n) return '';
        const c = n.cloneNode(true);
        c.querySelectorAll('svg, style, script').forEach(x => x.remove());
        return (c.textContent || '').replace(/\\s+/g, ' ').trim();
    };
    const fromLabelledby = (node) => {
        const ref = node.getAttribute && node.getAttribute('aria-labelledby');
        if (!ref) return '';
        return ref.trim().split(/\\s+/)
            .map(id => txt(document.getElementById(id)))
            .filter(Boolean).join(' ').trim();
    };
"""

# The element's OWN aria-labelledby -> resolved label text (used when no <label for=>).
_LABELLEDBY_JS = "el => {%s return fromLabelledby(el); }" % _JS_HELPERS

# Nearby-text lookup for non-radio fields without a <label for=...> or labelledby.
# Walks ancestors outward: at each field-ish container, try its aria-labelledby,
# then the first NON-EMPTY label-ish descendant ([id$="_label"] is the Workable
# convention; an empty first candidate — e.g. react-datepicker's aria-live span —
# must not end the search the way single-closest+first-child did).
_NEARBY_JS = """el => {
    %s
    const own = fromLabelledby(el);
    if (own) return own;
    const CONTAINERS = '.field, .form-group, .form-field, [class*="field"], [class*="question"], label, .MuiFormControl-root, .spl-form-element, div[class*="input"]';
    const LABELS = 'label, .label, [class*="label"], [id$="_label"], legend, h3, h4, span';
    let node = el.parentElement;
    for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
        if (!node.matches(CONTAINERS)) continue;
        const g = fromLabelledby(node);
        if (g) return g;
        for (const lbl of node.querySelectorAll(LABELS)) {
            if (lbl === el || lbl.contains(el)) continue;
            const t = txt(lbl);
            if (t) return t;
        }
    }
    return '';
}""" % _JS_HELPERS

# Radio/checkbox GROUP question lookup. Deliberately NO bare `label` in the closest()
# list — that matches the option's OWN wrapping label first and returns the option
# text ("He/him") instead of the group question ("Pronouns"). The group container's
# aria-labelledby wins (Workable: fieldset[role=radiogroup]/div[role=group] carry it).
# Candidate rules:
#  - ':scope' anchors '[class*="question"] label' INSIDE the group — without it any
#    option label whose ANCESTOR (outside the group, e.g. Lever's
#    li.application-question wrapping the whole ul) has a question class qualifies,
#    and the group question became a SIBLING option's text ('She/her', 'No');
#  - a label that wraps a radio/checkbox input is an OPTION, never the question;
#  - nothing found at the nearest container -> walk OUTWARD (Lever: the inner ul
#    matches first but the question div lives one level up).
_GROUP_NEARBY_JS = """el => {
    %s
    const GROUPS = 'fieldset, [role="radiogroup"], [role="group"], .field, .form-group, [class*="question"], [class*="Question"], ul, .application-question';
    const own = el.closest('label');
    const optText = own ? txt(own) : '';
    let group = el.closest(GROUPS);
    for (let depth = 0; group && depth < 4; depth++) {
        const g = fromLabelledby(group);
        if (g && g !== optText) return g;
        const cands = group.querySelectorAll('legend, :scope [class*="question"] label, [class*="label"], [id$="_label"], h3, h4');
        for (const c of cands) {
            if (own && (own.contains(c) || c.contains(own))) continue;
            if (c.querySelector('input[type="radio"], input[type="checkbox"]')) continue;
            const t = txt(c);
            if (!t || t === optText) continue;
            return t;
        }
        group = group.parentElement ? group.parentElement.closest(GROUPS) : null;
    }
    return '';
}""" % _JS_HELPERS

# A radio/checkbox OPTION's own label. Workable hides the real input inside a
# wrapper div[role=radio|checkbox] whose aria-labelledby lists "<group_label_id>
# <option_label_id>" — the LAST resolvable id is the option text ("Yes"/"No").
_OPTION_LABEL_JS = """el => {
    %s
    const wrap = el.closest('[role="radio"], [role="checkbox"]');
    const ref = wrap && wrap.getAttribute('aria-labelledby');
    if (!ref) return '';
    const ids = ref.trim().split(/\\s+/);
    for (let i = ids.length - 1; i >= 0; i--) {
        const t = txt(document.getElementById(ids[i]));
        if (t) return t;
    }
    return '';
}""" % _JS_HELPERS

# Explicit ARIA group container identity for distinct-name option merging
# (_merge_radio_groups pass 2). Only declared groups count — fieldsets without a
# role can wrap unrelated consents.
_GROUP_KEY_JS = """el => {
    const g = el.closest('[role="radiogroup"], [role="group"]');
    if (!g) return '';
    return g.getAttribute('aria-labelledby') || g.id || g.getAttribute('data-ui') || '';
}"""


async def extract_form_fields(page: Page) -> list[dict]:
    """Extract all form fields using Playwright locators (pierces Shadow DOM)."""
    fields = []
    # (type, name) -> count of value-less radios/checkboxes already emitted,
    # for nth-qualified selectors (see _field_selector)
    valueless_seen: dict[tuple[str, str], int] = {}

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

                # Skip DUMMY/TEMPLATE inputs some ATSs render as 1x1 aria-hidden fields
                # (e.g. Workable's CA_/QA_ shadow inputs): Playwright's is_visible() passes a
                # 1x1 visibility:visible box, so they slipped in as REQUIRED-unfilled fields
                # and falsely blocked an otherwise-complete form (verified on Workable/zyte).
                # A real field is neither aria-hidden nor collapsed to ~0px (file inputs are
                # legitimately tiny, so they're exempt).
                if el_type != "file":
                    try:
                        if await el.evaluate(
                            "el => { const r = el.getBoundingClientRect();"
                            " return el.closest('[aria-hidden=\"true\"]') !== null"
                            " || (r.width <= 2 && r.height <= 2); }"):
                            continue
                    except Exception:
                        pass

                # React-Select (Greenhouse) renders a typeahead <input> inside its
                # div widget; Workable renders a fixed-list select as a readonly
                # input[role=combobox]. NEITHER is an open-text field: a text `fill`
                # types prose the widget can't accept — a readonly combobox even
                # type-ahead-jumps to the WRONG (first/worst) option. dropdowns.py
                # owns these (fill_comboboxes_known: option match / candidate level).
                if tag == "input":
                    try:
                        if await el.evaluate(
                            "el => !!el.closest('.select__container')"
                            " || el.getAttribute('role')==='combobox'"
                            " || (el.getAttribute('aria-haspopup')||'').includes('listbox')"):
                            continue
                    except Exception:
                        pass

                el_id = await el.get_attribute("id") or ""
                el_name = await el.get_attribute("name") or ""
                aria_label = await el.get_attribute("aria-label") or ""
                data_qa = await el.get_attribute("data-qa") or ""
                placeholder = await el.get_attribute("placeholder") or ""
                title = await el.get_attribute("title") or ""
                required = await el.get_attribute("required") is not None or await el.get_attribute("aria-required") == "true"

                # Datepicker widget (NOT a free-text field): a date-mask placeholder
                # (mm/dd/yyyy), a "Pick date…" placeholder, or a react-datepicker /
                # *-input-date widget class. Typing prose into any of these leaves the
                # value invalid (a react-datepicker even clears it on blur), so a REQUIRED
                # start-date field ends up silently empty. Mark it `date` so the open-text
                # known answer is rejected (below) and the _start_date rule fills a real date.
                if tag == "input" and el_type in ("", "text"):
                    is_dp = bool(_DATE_MASK.search(placeholder)
                                 or _DATE_PLACEHOLDER.search(placeholder))
                    if not is_dp:
                        try:
                            is_dp = bool(await el.evaluate(
                                "el => !!el.closest('.react-datepicker-wrapper')"
                                " || /(?:^|[\\s_-])(?:datepicker|input-date)/i.test(el.className||'')"))
                        except Exception:
                            is_dp = False
                    if is_dp:
                        el_type = "date"

                # Try to find label text
                label = ""
                try:
                    if el_id:
                        label_el = page.locator(f'label[for="{el_id}"]').first
                        if await label_el.is_visible(timeout=500):
                            label = (await label_el.inner_text()).strip()
                except Exception:
                    pass
                if not label:  # Workable links input -> question via aria-labelledby
                    try:
                        label = await el.evaluate(_LABELLEDBY_JS) or ""
                    except Exception:
                        pass

                # Try nearby text via parent. Radio/checkbox need the GROUP question,
                # not the option's own label text — separate lookup (A2).
                nearby = ""
                option_label = ""
                group_key = ""
                if el_type in ("radio", "checkbox"):
                    try:
                        nearby = await el.evaluate(_GROUP_NEARBY_JS)
                    except Exception:
                        pass
                    try:
                        option_label = await el.evaluate(_OPTION_LABEL_JS)
                    except Exception:
                        pass
                    try:
                        group_key = await el.evaluate(_GROUP_KEY_JS)
                    except Exception:
                        pass
                elif not label:
                    try:
                        nearby = await el.evaluate(_NEARBY_JS)
                    except Exception:
                        pass

                el_value = ""
                if el_type in ("radio", "checkbox"):
                    el_value = await el.get_attribute("value") or ""

                nth = valueless_seen.get((el_type, el_name), 0)
                selector = _field_selector(el_id, el_name, aria_label, el_type, el_value, nth, data_qa)
                if not selector and tag == "input":
                    # Ashby react-datepicker / custom inputs carry NO id/name/aria-label/data-qa
                    # (the <label for> targets the container's data-field-path UUID, not the
                    # input), so they were silently dropped. Target via the container attribute.
                    try:
                        selector = await el.evaluate(
                            "el => { const c = el.closest('[data-field-path]');"
                            " const p = c && c.getAttribute('data-field-path');"
                            " return p ? '[data-field-path=\"'+p+'\"] input' : ''; }") or ""
                    except Exception:
                        selector = ""
                if not selector:
                    continue
                if el_type in ("radio", "checkbox") and el_name and not el_value:
                    valueless_seen[(el_type, el_name)] = nth + 1

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
                    "value": el_value,
                    "optionLabel": option_label,
                    "groupKey": group_key,
                })

            except Exception as e:
                logger.debug("Error extracting field: %s", e)
                continue

    return _merge_radio_groups(fields)


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

    # Workday/iCIMS gate the form behind account creation — stop for the human, don't garble.
    if any(kw in text for kw in ("create account", "create a password", "create your account",
                                 "register to apply", "create an account to apply")):
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
    facts: dict | None = None,
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
        # TWO texts per field: raw match_text keeps name/id/title signals for rule
        # matching (name="email" etc.); display_text is the HUMAN question only —
        # internal ids/placeholders there poison the report and the answer cache.
        match_text = " ".join(filter(None, [
            f["label"], f["ariaLabel"], f["placeholder"],
            f["name"], f["id"], f["title"], f["nearbyText"],
        ]))
        display_parts = []
        _seen_parts: set[str] = set()
        for part in (f["label"], f["ariaLabel"], f["placeholder"], f["nearbyText"]):
            # dedup on a NORMALIZED key so whitespace/case variants of the same text
            # collapse: a leaky aria-label ' Twitter' vs label 'Twitter' otherwise yields
            # display_text 'Twitter Twitter', which defeats the exact known-answer match
            # and leaves the field blank.
            norm = re.sub(r"\s+", " ", (part or "").strip()).lower()
            if part and norm and norm not in _seen_parts:
                # A generic placeholder ("Type your response", "Select…") is not a question;
                # letting it into display_text poisons known-answer matching (see
                # _GENERIC_PLACEHOLDER). Skip it here — it stays in match_text for rules.
                if _GENERIC_PLACEHOLDER.match(norm):
                    continue
                _seen_parts.add(norm)
                display_parts.append(part.strip())
        display_text = _clean_text(" ".join(display_parts))
        if not display_text:  # nothing human-readable -> cleaned name/id beats nothing
            display_text = _clean_text(match_text)

        # Optional social/profile/URL field (LinkedIn, Website, Twitter, Telegram, …) that is
        # NOT required: leave it blank instead of typing the demo persona's invented profile.
        # A required one (red *) falls through and is filled below.
        if (not f.get("required") and f["tag"] == "input"
                and f["type"] in ("text", "url", "search", "input", "")
                and _OPTIONAL_SOCIAL.search(display_text)
                and not _looks_like_question(display_text)):
            continue

        # Check known answers first — EXACT label match preferred over fuzzy overlap (and
        # over dict order), so a work-auth 'Yes' doesn't cross-bind onto the sponsorship
        # radio via shared generic words (see _best_known_answer).
        _best = _best_known_answer(known_answers, display_text)
        # A datepicker can't accept an open-text known answer: the draft for a "When can
        # you start?" field is a PROSE availability sentence, which a react-datepicker
        # rejects (clears on blur), leaving the REQUIRED field empty AND pre-empting the
        # _start_date rule that would supply a real date. Drop a non-date known answer for
        # date widgets so it falls through to _match_field -> _start_date below.
        if _best and f["type"] == "date" and not _looks_like_date(_best[1]):
            _best = None
        for q_text, answer in ([_best] if _best else []):
            if True:
                if f["type"] in ("radio_group", "checkbox_group"):
                    opt = _pick_option(f["options"], answer, "")
                    if opt:
                        fields.append({"selector": opt["value"], "action": "check",
                                       "value": "true",
                                       "matched": f"known_answer:{q_text[:30]}"})
                    else:
                        unknown.append(_unknown_entry(f, display_text))
                elif f["type"] in ("checkbox", "radio"):
                    # Ashby (and friends) render each option of a multi-/single-
                    # select as its OWN <input> with a unique name (name == the
                    # option label) and no ARIA group container, so they never
                    # merge into a *_group and land here individually — each
                    # matching the group question carried in `nearbyText`. `answer`
                    # is the CHOSEN option label(s) (or an affirmative for a lone
                    # boolean checkbox). Emitting 'fill' here calls .fill() on a
                    # checkbox, which throws and checks NOTHING (the silent multi-
                    # select miss). Instead CHECK this box only when it is a chosen
                    # option; the group's other options stay blank (correct for a
                    # multi-select), so no unknown entry either.
                    own = (f.get("label") or f.get("optionLabel")
                           or f.get("name") or "").strip().lower()
                    wants = {w.strip().lower()
                             for w in re.split(r"\s*[,;]\s*", str(answer)) if w.strip()}
                    _ans = str(answer).strip()
                    affirmative = _ans.lower() in ("yes", "true", "1", "on")
                    # A lone affirmative-consent checkbox (e.g. an arbitration/attestation
                    # acknowledgement) carries its OWN sentence as the answer. When that
                    # sentence contains commas, splitting it into `wants` shreds it and the
                    # full `own` label matches no fragment -> a REQUIRED consent box was left
                    # silently unchecked. A whole-string echo of the label, OR an answer that
                    # OPENS with an acknowledgement/consent phrase, IS a check. (A multi-select
                    # option's answer is a bare option label, so it never opens that way.)
                    echoes_label = bool(own) and (own == _ans.lower()
                                                  or (len(own) > 12 and own in _ans.lower()))
                    ack = bool(re.match(
                        r"(?i)\s*(i\s+(?:acknowledge|confirm|agree|understand|consent|certify"
                        r"|accept|have\s+(?:read|opened))|yes\b)", _ans))
                    if affirmative or ack or echoes_label or (own and own in wants):
                        fields.append({"selector": f["selector"], "action": "check",
                                       "value": "true",
                                       "matched": f"known_answer:{q_text[:30]}"})
                else:
                    fields.append({
                        "selector": f["selector"],
                        "action": "select" if f["tag"] == "select" else "fill",
                        "value": answer,
                        "matched": f"known_answer:{q_text[:30]}",
                        "required": f["required"], "label": display_text,
                    })
                break
        else:
            # Try pattern matching
            match = _match_field(match_text)
            if match:
                key, action = match
                value = _resolve_value(key, profile, cover_letter, known_answers, facts)

                if key == "_skip":  # demographics/portfolio: intentionally left blank
                    continue

                if value == "__UPLOAD__":
                    # set_input_files only makes sense on a REAL file input. A
                    # resume/CV mention on anything else — Workable radio groups
                    # inherit the group's first-option radio selector here — hung
                    # 30s per field live. Route those down the normal unknown/
                    # choice path (groups keep their options for the choice engine).
                    if f["type"] != "file":
                        unknown.append(_unknown_entry(f, display_text))
                        continue
                    resume_path = profile.get("resume_path") or ""
                    if resume_path:
                        fields.append({
                            "selector": f["selector"],
                            "action": "upload",
                            "value": resume_path,
                            "matched": "resume",
                        })
                    continue

                # Matched rule but profile/facts hold NO value (linkedin_url='',
                # desired_salary='', missing fact). Silently dropping a required
                # field makes the report claim "nothing left for the human" while
                # the form has blanks -> surface it instead (A4).
                if value in (None, ""):
                    if f["required"] or f["tag"] == "textarea":
                        unknown.append(_unknown_entry(f, display_text))
                    continue

                # Datepickers take dates only: typing prose ('Immediately') produces
                # garbage. A digit-less value -> leave the field for the human.
                if f["type"] == "date" and not re.search(r"\d", str(value)):
                    unknown.append(_unknown_entry(f, display_text))
                    continue

                # Handle radio/checkbox groups
                if f["type"] in ("radio_group", "checkbox_group"):
                    opt = _pick_option(f["options"], value, key)
                    if opt:
                        fields.append({"selector": opt["value"], "action": "check",
                                       "value": "true", "matched": key})
                    else:
                        unknown.append(_unknown_entry(f, display_text))
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
                    elif f["required"]:
                        unknown.append(_unknown_entry(f, display_text))
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
                        elif f["required"]:
                            unknown.append(_unknown_entry(f, display_text))
                    elif f["type"] in ("checkbox", "radio"):
                        # Only auto-check an AFFIRMATIVE answer. A negative answer on a
                        # checkbox/radio is ambiguous (wrong control / Yes-No pair) -> human decides.
                        if str(value).strip().lower() in ("yes", "true", "1"):
                            fields.append({
                                "selector": f["selector"],
                                "action": "check",
                                "value": "true",
                                "matched": key,
                            })
                        else:
                            unknown.append(_unknown_entry(f, display_text))
                    elif f["tag"] == "textarea":
                        # A bare Yes/No never belongs in a free-text box: the rule mis-fired on
                        # a narrative question (e.g. "...describe...remote..."). Leave it for the
                        # human / LLM draft instead of typing "Yes".
                        unknown.append(_unknown_entry(f, display_text))
                    else:
                        fields.append({
                            "selector": f["selector"],
                            "action": "fill",
                            "value": value,
                            "matched": key,
                            "required": f["required"], "label": display_text,
                        })
                    continue

                fields.append({  # value guaranteed non-empty by the A4 check above
                    "selector": f["selector"],
                    "action": action,
                    "value": value,
                    "required": f["required"], "label": display_text,
                    "matched": key,
                })
            else:
                # Unknown field. Optional textareas / long question-looking text inputs
                # must surface too (LLM draft or human) — dropping them silently leaves
                # essay boxes empty on submit (A5).
                if (f["required"] or f["tag"] == "select"
                        or f["type"] in ("radio_group", "checkbox_group")
                        or f["tag"] == "textarea"
                        or (f["tag"] == "input" and f["type"] in ("text", "input", "")
                            and _looks_like_question(display_text))):
                    unknown.append(_unknown_entry(f, display_text))

    # Workable renders the resume input with an opaque name and no 'resume' text
    # anywhere near it — if no upload was genuinely PLANNED (a 'resume' match on a
    # radio group must not suppress this), attach the résumé to the first file
    # input on the page (A7) that is NOT a photo/avatar upload (Workable forms put
    # an optional "Photo" file input BEFORE the resume one).
    if profile.get("resume_path") and not any(
            x["matched"] == "resume" and x["action"] == "upload" for x in fields):
        # Skip photo/avatar inputs AND Ashby's "Autofill from resume" parser input —
        # the résumé attachment belongs on the real resume field, not the parser.
        _photo = re.compile(r"(?i)photo|avatar|picture|headshot|logo|autofill")
        file_field = next(
            (rf for rf in raw_fields if rf["type"] == "file"
             and not _photo.search(" ".join(filter(None, [
                 rf["label"], rf["ariaLabel"], rf["nearbyText"], rf["name"], rf["id"]])))),
            None)
        if file_field:
            fields.append({
                "selector": file_field["selector"],
                "action": "upload",
                "value": profile["resume_path"],
                "matched": "resume",
            })
            unknown = [u for u in unknown if u["selector"] != file_field["selector"]]

    submit = await find_submit_button(page)

    logger.info("Mapped %d fields, %d unknown, submit: %s", len(fields), len(unknown), submit)

    return {
        "fields": fields,
        "unknown_questions": unknown,
        "submit_selector": submit,
        "page_type": page_type,
    }


def _known_answer_matches(q_text: str, match_text: str) -> bool:
    """A cached answer may only fill a field whose text genuinely overlaps the
    cached question — >=2 significant words, not one common word ("work").

    Exception: an EXACT label match (normalized) is unambiguous and fills even
    with a single significant word — Ashby renders short question TITLES
    ("Segment", "Performance") as the field label, and those carry an exact
    drafted-answer key. The >=2-hit heuristic below can never pass for a
    one-word title (max(2, …) demands 2 hits), so without this the exact answer
    is silently dropped and the field is left unfilled.

    _n also strips a trailing '(Optional)'/'(Required)' marker: Workable renders
    the ' (Optional)' as a decorative <span> outside the field label, so the
    drafted key 'Summary (Optional)' never exact-matched the label 'Summary'
    (one shared significant word, so the >=2-hit path failed too) -> the textarea
    stayed blank. Dropping the marker (and excluding it from the sig words) fixes
    every such optionality-suffixed key."""
    if _known_answer_exact(q_text, match_text):
        return True
    mt = match_text.lower()
    sig = {w.lower() for w in q_text.split()
           if len(w) > 3 and w.lower() not in ("optional", "required")}
    hits = sum(1 for w in sig if w in mt)
    return bool(sig) and hits >= max(2, len(sig) // 2)


def _known_answer_exact(q_text: str, match_text: str) -> bool:
    """True when the cached question and the field label are the SAME text (normalized:
    whitespace/asterisk-insensitive, trailing '(Optional)'/'(Required)' dropped). An exact
    match is unambiguous and is preferred over any fuzzy word-overlap hit."""
    # Also collapse a consecutive duplicated word run ('Title Title' -> 'Title',
    # 'Twitter Twitter' -> 'Twitter'): a Greenhouse label whose <label> and a sibling node
    # both carry the text yields a doubled display_text that otherwise never exact-matches
    # the single-word drafted key.
    _n = lambda s: re.sub(
        r"\b(\w[\w-]*)(?:\s+\1\b)+", r"\1",
        re.sub(r"\s*\(?(optional|required)\)?\s*$", "",
               re.sub(r"\s+", " ", re.sub(r"[✱*]", "", s or "").lower()).strip()))
    return bool(_n(q_text)) and _n(q_text) == _n(match_text)


def _best_known_answer(known_answers: dict, display_text: str):
    """Pick the cached (question, answer) for a field: an EXACT normalized-label match wins
    over any fuzzy word-overlap hit and over dict order. Two Yes/No screeners sharing only
    generic words (authorized-to-work vs require-sponsorship) otherwise cross-bind on the
    first fuzzy hit, inverting a work-auth 'Yes' onto the sponsorship radio. Returns
    (q_text, answer) or None."""
    for q_text, answer in known_answers.items():
        if _known_answer_exact(q_text, display_text):
            return (q_text, answer)
    for q_text, answer in known_answers.items():
        if _known_answer_matches(q_text, display_text):
            return (q_text, answer)
    return None


def _unknown_entry(f: dict, display_text: str) -> dict:
    return {
        "question_text": _clean_text(display_text)[:200],
        "selector": f["selector"],
        "type": f["type"],
        "options": [o["text"] for o in f.get("options", [])],
        "option_selectors": [o.get("value", "") for o in f.get("options", [])]
        if f["type"] in ("radio_group", "checkbox_group") else [],
    }


def _pick_option(options: list[dict], desired: str, key: str) -> dict | None:
    """Find the best matching option dict for a desired value."""
    desired_lower = (desired or "").lower()

    if key in ("_work_auth", "_yes") or (key.startswith("_fact:") and desired_lower == "yes"):
        for o in options:
            if o["text"].lower() in ("yes", "yes, i am", "authorized", "yes - authorized"):
                return o
        for o in options:
            if "yes" in o["text"].lower():
                return o

    if key in ("_no", "_sponsorship") or (key.startswith("_fact:") and desired_lower == "no"):
        for o in options:
            if o["text"].lower().strip() == "no":
                return o
        for o in options:
            if re.match(r"no\b", o["text"].lower().strip()):
                return o

    if key == "_country":
        for o in options:
            t = o["text"].lower()
            if "united states" in t or t in ("us", "usa"):
                return o

    for o in options:
        if o["text"].lower().strip() == desired_lower:
            return o
    for o in options:
        if desired_lower and desired_lower in o["text"].lower():
            return o
    return None


def _match_select_option(options: list[dict], desired: str, key: str) -> str | None:
    opt = _pick_option(options, desired, key)
    return opt["text"] if opt else None
