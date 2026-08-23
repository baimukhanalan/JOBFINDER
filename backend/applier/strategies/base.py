"""Apply-strategy interface. A strategy knows how to PRE-FILL a given ATS's form.

A strategy fills every field, screenshots, and STOPS for a human to submit. There is
deliberately no submit path: nothing here ever clicks the final Submit button — the
person reviews the pre-filled form and submits it himself.
"""
import asyncio
import functools
import logging
import os
import re
from abc import ABC, abstractmethod

from playwright.async_api import Page

from backend.applier.analyzer import analyze_page, detect_page_type
from backend.applier.dropdowns import (
    apply_button_choice,
    apply_react_select_choice,
    fill_comboboxes_known,
    fill_demographics_decline,
    fill_react_selects,
    fill_react_selects_known,
    harvest_button_groups,
    harvest_react_selects,
    list_unanswered_react_selects,
)
from backend.applier.filler import check_input, dismiss_overlays, fill_form

logger = logging.getLogger(__name__)


def _env_true(name: str) -> bool:
    """A truthy env switch (1/true/yes/on)."""
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


# Snapshot every visible value control so we can measure how many fields the ATS's
# OWN résumé parser populated (résumé-parser-only mode). Files/passwords excluded.
_VALUES_JS = r"""
() => {
  const out = {}; let i = 0;
  document.querySelectorAll('input, textarea, select').forEach(el => {
    const t = (el.type || el.tagName || '').toLowerCase();
    if (['hidden','submit','button','reset','image','file','password'].includes(t)) return;
    const key = el.id || el.name || ('__i' + (i++));
    out[key] = (t === 'checkbox' || t === 'radio') ? (el.checked ? '1' : '')
                                                    : ((el.value || '').trim());
  });
  return out;
}
"""


async def _snapshot_values(page: Page) -> dict:
    try:
        return await page.evaluate(_VALUES_JS)
    except Exception:
        return {}


def _count_newly_filled(before: dict, after: dict) -> int:
    """How many controls went from empty -> non-empty between two snapshots."""
    return sum(1 for k, v in after.items() if v and not (before.get(k) or ""))


# Question types that must never be routed to open-text drafting: discrete-option
# controls, files, and datepickers (typing prose into a date mask makes garbage).
_CLOSED = frozenset({
    "select", "select-one", "radio_group", "checkbox_group",
    "radio", "checkbox", "file", "date",
})

# "[review] " is the LLM/cache WIRE format for "a human must eyeball this before
# submit" (it travels through the answer cache and drafted_answers so the flag
# survives across runs). It must NEVER reach a live form field or a human-facing
# surface — always convert it to metadata with the helpers below first.
_REVIEW_PREFIX = "[review]"


def strip_review(ans):
    """Split the '[review]' wire prefix off a drafted answer.

    Returns (text, flagged). Non-strings and unprefixed strings pass through
    unchanged with flagged=False.
    """
    if isinstance(ans, str) and ans.startswith(_REVIEW_PREFIX):
        return ans.removeprefix("[review] ").removeprefix("[review]").lstrip(), True
    return ans, False


def merge_custom_pick(q_text: str, option_text: str | None, backed: bool,
                      applied: bool, sources: dict, review_items: list,
                      choice_picks: dict, drafted: dict, unfilled: list) -> int:
    """Bookkeeping for ONE custom-widget question (react-select / button group)
    after the choice engine ran. Mirrors the native closed-choice loop EXACTLY:
    backed -> sources['choice'] + replayable drafted entry; unbacked ->
    sources['choice_review'] + review_items. No pick / failed apply -> unfilled
    (custom questions are not in unknown_questions, so the standard answered_idx
    bookkeeping never sees them). Returns 1 when applied (add to filled count)."""
    if option_text is None or not applied:
        if q_text not in unfilled:
            unfilled.append(q_text)
        return 0
    choice_picks[q_text] = {"option": option_text, "backed": backed}
    if backed:
        sources["choice"] += 1
        drafted[q_text] = option_text
    else:
        sources["choice_review"] += 1
        review_items.append({"question": q_text, "answer": option_text,
                             "kind": "choice"})
    return 1


def review_from_known(known_answers: dict | None) -> list[dict]:
    """Re-flag '[review]'-prefixed answers that replay via known_answers.

    On a co-pilot reload, drafted_answers (wire format, prefix included) come
    back as known_answers; without this rescan the review gate silently
    vanishes on reload. Returns review_items entries with STRIPPED text.
    """
    items = []
    for q_text, ans in (known_answers or {}).items():
        text, flagged = strip_review(ans)
        if flagged:
            items.append({"question": q_text, "answer": text, "kind": "draft"})
    return items


class ApplyStrategy(ABC):
    name: str = "generic"

    @classmethod
    @abstractmethod
    def matches(cls, url: str) -> bool:
        ...

    async def open_form(self, page: Page) -> None:
        """Optional: click an 'Apply' button to reveal the form. Default: no-op."""
        return None

    async def autofill_from_resume(self, page: Page, resume_path: str) -> bool:
        """Optional: upload the résumé to an ATS 'autofill from resume' parser so the
        site pre-populates its own fields. Default: no-op."""
        return False

    async def attach_resume(self, page: Page, resume_path: str) -> bool:
        """Upload the résumé to the form's real résumé-attachment file input — NOT a
        photo/avatar input, and NOT Ashby's separate 'Autofill from resume' parser
        input. On Greenhouse / Lever / Workable / Workday the same attachment upload
        also triggers the ATS's own résumé parser (it pre-populates name / email /
        experience from the file). Returns True on a successful upload.

        Mirrors the analyzer's résumé-field heuristic (analyzer.py 'A7' fallback):
        first non-image file input whose id/name/surrounding text is not a
        photo/avatar/logo/autofill control.
        """
        if not resume_path:
            return False
        try:
            for inp in await page.query_selector_all('input[type="file"]'):
                info = await inp.evaluate(
                    '(el)=>{const c=el.closest("div,section,fieldset,form");'
                    'return {acc:(el.accept||"").toLowerCase(),'
                    ' blob:((el.id||"")+" "+(el.name||"")+" "+(c?c.innerText:""))'
                    '.toLowerCase()};}')
                if "image/" in (info.get("acc") or ""):
                    continue
                if re.search(r"photo|avatar|picture|headshot|logo|autofill",
                             info.get("blob") or ""):
                    continue
                await inp.set_input_files(resume_path)
                # Give an attachment-triggered server-side parser time to populate.
                await page.wait_for_timeout(3500)
                return True
        except Exception as exc:
            logger.debug("attach_resume raised: %s", exc)
            return False
        return False

    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "",
                      resume_parser_only: bool = False) -> dict:
        # known_answers = answers already drafted by a prior batch — fills them directly
        # (no LLM call), so the co-pilot's "Fill" is instant instead of re-drafting (~30s).
        # resume_parser_only = anti-spam mode: feed the résumé to the ATS's OWN parser
        # and attach it, but type nothing else (env RESUME_PARSER_ONLY flips it globally).
        if not resume_parser_only and _env_true("RESUME_PARSER_ONLY"):
            resume_parser_only = True
        await self.open_form(page)
        await page.wait_for_timeout(1500)
        # Dismiss cookie/consent modals (Workable/OneTrust) whose backdrop intercepts
        # pointer events — without this a required dropdown (e.g. Workable 'English Level')
        # can never be clicked and silently stays blank. Mirrors runner.prefill; the helper
        # is a no-op when no banner is present.
        try:
            if await dismiss_overlays(page):
                await page.wait_for_timeout(500)
        except Exception as exc:
            logger.debug("dismiss_overlays raised: %s", exc)
        if resume_parser_only:
            return await self._prefill_resume_parser_only(page, resume_path)
        # Let the ATS parse the résumé and pre-populate its own fields first (Ashby's
        # "Autofill from resume"); the analyzer then fills only what's still empty.
        try:
            await self.autofill_from_resume(page, resume_path)
        except Exception as exc:
            logger.debug("autofill_from_resume raised: %s", exc)
        # Attach the résumé to the form's real file input. autofill_from_resume only feeds
        # Ashby's separate PARSER input; without this the required résumé upload on
        # Workable/Lever/etc. was never populated (form silently unsubmittable). Direct
        # set_input_files, so no filechooser race; attach_resume skips photo/avatar inputs.
        try:
            await self.attach_resume(page, resume_path)
        except Exception as exc:
            logger.debug("attach_resume raised: %s", exc)
        # known_answers may carry the '[review]' wire prefix (replayed
        # drafted_answers) — the analyzer types values verbatim into fields, so
        # strip here; the flags are reinstated from the originals further down.
        known_clean = {q: strip_review(a)[0]
                       for q, a in (known_answers or {}).items()}
        analysis = await analyze_page(page, profile_form, cover_letter,
                                      known_clean, facts or {})
        page_type = analysis.get("page_type", "unknown")
        if page_type in ("login_required", "captcha", "expired"):
            return {
                "strategy": self.name, "page_type": page_type,
                "filled": 0, "failed": 0,
                "unfilled": [q.get("question_text", "") for q in analysis.get("unknown_questions", [])],
                "review_items": [], "answer_sources": {},
                "submit_selector": analysis.get("submit_selector"),
                "submitted": False,
                "note": f"stopped: page_type={page_type}",
            }
        success, fail, failed_required = await fill_form(page, analysis)

        # Custom React-Select dropdowns (Greenhouse) the analyzer can't fill — deterministic
        # eligibility ones only (work-auth/sponsorship/country/18+/background/equipment).
        try:
            ds = await fill_react_selects(page)
        except Exception as exc:
            logger.debug("fill_react_selects raised unexpectedly: %s", exc)
            ds = {"filled": 0, "handled": []}
        success += ds["filled"]

        # Typeahead react-selects (e.g. School) we hold an explicit answer for: type +
        # pick. These list no options until typed, so the harvest above skips them.
        try:
            dk = await fill_react_selects_known(page, known_clean)
            success += dk["filled"]
        except Exception as exc:
            logger.debug("fill_react_selects_known raised unexpectedly: %s", exc)

        # Ashby input[role=combobox] dropdowns/typeaheads (What brought you, Location)
        # from known answers — the only path that fills these custom widgets.
        try:
            dcb = await fill_comboboxes_known(page, known_clean)
            success += dcb["filled"]
        except Exception as exc:
            logger.debug("fill_comboboxes_known raised unexpectedly: %s", exc)

        # EEO/diversity self-ID fields: answer the explicit 'Prefer not to answer' option
        # (never a real protected characteristic), so a REQUIRED demographic completes.
        try:
            dd = await fill_demographics_decline(page)
            success += dd["filled"]
        except Exception as exc:
            logger.debug("fill_demographics_decline raised unexpectedly: %s", exc)

        unknown = analysis.get("unknown_questions", [])
        # `success` here = rule-based fills that actually took + react-select eligibility
        sources = {"rule": success, "fact": 0,
                   "choice": 0, "choice_review": 0, "draft": 0, "draft_review": 0,
                   "human": 0}
        # how many of the rule fills came from the person's fact sheet (planned count)
        fact_planned = sum(1 for f in analysis.get("fields", [])
                           if str(f.get("matched", "")).startswith("_fact:"))
        sources["fact"] = fact_planned
        sources["rule"] = max(0, success - fact_planned)
        review_items: list[dict] = []
        drafted: dict = {}
        choice_picks: dict = {}
        answered_idx: set[int] = set()
        unfilled_custom: list[str] = []  # custom-widget questions left unanswered

        # Reinstate review flags carried in via known_answers (co-pilot reload).
        # Runs regardless of `draft` — otherwise a reload erases the review gate.
        for item in review_from_known(known_answers):
            review_items.append(item)
            sources["draft_review"] += 1

        # --- Replay-first: exact known answers for native closed questions ------
        # Any known answer (the reviewed ideal packet) for a native select / radio /
        # checkbox group is applied EXACTLY here, before the LLM gets a say — so the
        # reviewed packet is what lands, and short-label selects the analyzer's fuzzy
        # match misses ("Country", "Seniority") still fill. answered_idx marks them so
        # the deterministic/draft loops below skip them (no double-fill, no clobber).
        for i, q in enumerate(unknown):
            if i in answered_idx:
                continue
            if q.get("type") not in ("select", "select-one", "radio_group", "checkbox_group"):
                continue
            opts = q.get("options") or []
            known = known_clean.get(q.get("question_text", ""))
            if not known or not opts:
                continue
            idx = opts.index(known) if known in opts else next(
                (k for k, o in enumerate(opts)
                 if (o or "").strip().lower() == str(known).strip().lower()), None)
            if idx is None or not await self._fill_choice(page, q, idx):
                continue
            answered_idx.add(i)
            success += 1
            qt = q["question_text"]
            choice_picks[qt] = {"option": opts[idx], "backed": True}
            drafted[qt] = opts[idx]
            sources["choice"] += 1

        # --- Deterministic answering (LLM-FREE) --------------------------------
        # Fill the standard screener + identity fields (Telegram, hear-about,
        # relocation, working-hours suitability, English level) with NO model call,
        # so the form reaches "only Submit remains" even when drafting is off or the
        # LLM is down. Runs regardless of `draft`; the draft path below only augments
        # whatever is still open (it skips indices answered here).
        if job:
            det_closed = [(i, q) for i, q in enumerate(unknown)
                          if i not in answered_idx and q.get("options")
                          and q.get("type") in
                          ("select", "select-one", "radio_group", "checkbox_group")]
            if det_closed:
                from backend.services.tailor.choices import deterministic_choices
                picks = deterministic_choices(
                    [{"question_text": q["question_text"], "options": q["options"]}
                     for _, q in det_closed], facts or {})
                for (i, q), pick in zip(det_closed, picks):
                    if pick["index"] is None:
                        continue
                    if not await self._fill_choice(page, q, pick["index"]):
                        continue
                    answered_idx.add(i)
                    success += 1
                    q_text = q["question_text"]
                    option_text = q["options"][pick["index"]]
                    choice_picks[q_text] = {"option": option_text,
                                            "backed": bool(pick["backed"])}
                    if pick["backed"]:
                        sources["choice"] += 1
                        drafted[q_text] = option_text
                    else:
                        sources["choice_review"] += 1
                        review_items.append({"question": q_text,
                                             "answer": option_text, "kind": "choice"})

            det_open = [(i, q) for i, q in enumerate(unknown)
                        if i not in answered_idx and q.get("type") not in _CLOSED
                        and len(q.get("question_text", "")) > 12]
            if det_open:
                from backend.services.tailor.answers import deterministic_answers
                det = deterministic_answers(
                    [q["question_text"] for _, q in det_open], profile_form, job, facts)
                for i, q in det_open:
                    ans = det.get(q.get("question_text", ""))
                    sel = q.get("selector")
                    if not (ans and sel):
                        continue
                    fill_text, flagged = strip_review(ans)
                    try:
                        await page.locator(sel).first.fill(fill_text, timeout=4000)
                    except Exception:
                        continue
                    answered_idx.add(i)
                    success += 1
                    q_text = q["question_text"]
                    if flagged:
                        if not any(r["question"] == q_text for r in review_items):
                            sources["draft_review"] += 1
                            review_items.append({"question": q_text,
                                                 "answer": fill_text, "kind": "draft"})
                    else:
                        sources["rule"] += 1

        if draft and job:
            company = (job or {}).get("company", "")

            # 1) Closed questions with options -> constrained LLM choice (validated index).
            #    Skip those the deterministic phase already answered.
            closed = [(i, q) for i, q in enumerate(unknown)
                      if i not in answered_idx and q.get("options") and q.get("type") in
                      ("select", "select-one", "radio_group", "checkbox_group")]
            if closed:
                from backend.services.tailor.choices import choose_options
                loop = asyncio.get_running_loop()
                # sync HTTP LLM call + retry sleeps — keep them off the event loop so
                # Playwright's CDP connection doesn't stall during long drafts
                picks = await loop.run_in_executor(None, functools.partial(
                    choose_options,
                    [{"question_text": q["question_text"], "options": q["options"]}
                     for _, q in closed],
                    facts or {}, job, niche))
                for (i, q), pick in zip(closed, picks):
                    if pick["index"] is None:
                        continue
                    if not await self._fill_choice(page, q, pick["index"]):
                        continue
                    answered_idx.add(i)
                    success += 1
                    q_text = q["question_text"]
                    option_text = q["options"][pick["index"]]
                    choice_picks[q_text] = {"option": option_text,
                                            "backed": bool(pick["backed"])}
                    if pick["backed"]:
                        sources["choice"] += 1
                        # Bare option text so the reload replay works: the analyzer's
                        # known-answers branch matches it against radio/checkbox option
                        # labels (_pick_option) and select labels — a prefix would break
                        # that text matching.
                        drafted[q_text] = option_text
                    else:
                        # Unbacked guess: kept OUT of drafted so a reload re-evaluates
                        # (or re-flags) it instead of silently replaying a guess.
                        sources["choice_review"] += 1
                        review_items.append({"question": q_text,
                                             "answer": option_text,
                                             "kind": "choice"})

            # 2) Open text questions -> per-person cache, then LLM draft.
            # Route everything that's NOT a closed/binary/file type — this catches
            # "input", "email", "number", "tel", "url", "search", "", etc.
            open_qs = [(i, q) for i, q in enumerate(unknown)
                       if i not in answered_idx and q.get("type") not in _CLOSED
                       and len(q.get("question_text", "")) > 15]
            if open_qs:
                from backend import answer_cache
                from backend.services.tailor.answers import draft_answers
                q_texts = [q["question_text"] for _, q in open_qs]
                # update, not assign — `drafted` already holds backed choice picks
                drafted.update(answer_cache.get_many(q_texts, company,
                                                     profile=profile_id, niche=niche))
                missing = [t for t in q_texts if t not in drafted]
                if missing:
                    loop = asyncio.get_running_loop()
                    fresh = await loop.run_in_executor(None, functools.partial(
                        draft_answers, missing, profile_form, job, resume_summary,
                        facts=facts, niche_label=niche))
                    if fresh:
                        answer_cache.put_many(fresh, company,
                                              profile=profile_id, niche=niche)
                        drafted.update(fresh)
                for i, q in open_qs:
                    ans = drafted.get(q.get("question_text", ""))
                    sel = q.get("selector")
                    if not (ans and sel):
                        continue
                    # `ans` (wire format, prefix kept) stays in drafted_answers/cache;
                    # only the stripped text may touch the live field.
                    fill_text, flagged = strip_review(ans)
                    try:
                        await page.locator(sel).first.fill(fill_text, timeout=4000)
                    except Exception:
                        continue
                    answered_idx.add(i)
                    success += 1
                    if flagged:
                        q_text = q["question_text"]
                        # don't double-count questions already re-flagged by the
                        # known-answers rescan above
                        if not any(r["question"] == q_text for r in review_items):
                            sources["draft_review"] += 1
                            review_items.append({"question": q_text,
                                                 "answer": fill_text,
                                                 "kind": "draft"})
                    else:
                        sources["draft"] += 1

            # 3) Custom widgets (react-selects, button toggles) — closed questions
            # the DOM analyzer can't see; same constrained-choice engine. Runs
            # AFTER fill_react_selects: the deterministic eligibility ones are
            # answered already (single-value set), so harvest skips them.
            customs: list[tuple[str, dict]] = []
            try:
                customs += [("rs", c) for c in await harvest_react_selects(page)]
            except Exception:
                pass
            try:
                customs += [("btn", b) for b in await harvest_button_groups(page)]
            except Exception:
                pass
            if customs:
                # Replay first (co-pilot reload): a backed pick stored in
                # drafted_answers comes back via known_answers — apply it directly,
                # no LLM round-trip. Only exact question + exact option text replay.
                pending: list[tuple[str, dict]] = []
                for kind, c in customs:
                    qt = c["question_text"]
                    known = known_clean.get(qt)
                    if known is None:
                        # The drafted key and the live harvested label are truncated at
                        # DIFFERENT caps (scraper [:300] vs button-group harvest [:200]), so a
                        # long (>200-char) Ashby Yes/No label never string-equals its drafted
                        # key -> the already-drafted answer is discarded and a REQUIRED field
                        # is left blank. Recover via a prefix match (one key starts the other).
                        known = next((v for k, v in known_clean.items()
                                      if k and (k.startswith(qt) or qt.startswith(k))), None)
                    if known in c["options"]:
                        idx = c["options"].index(known)
                        if kind == "rs":
                            ok = await apply_react_select_choice(
                                page, c["container_index"], known)
                        else:
                            ok = await apply_button_choice(page, c["selectors"][idx])
                        success += merge_custom_pick(
                            c["question_text"], known, True, ok, sources,
                            review_items, choice_picks, drafted, unfilled_custom)
                    else:
                        pending.append((kind, c))
                if pending:
                    from backend.services.tailor.choices import choose_options
                    loop = asyncio.get_running_loop()
                    picks = await loop.run_in_executor(None, functools.partial(
                        choose_options,
                        [{"question_text": c["question_text"], "options": c["options"]}
                         for _, c in pending],
                        facts or {}, job, niche))
                    for (kind, c), pick in zip(pending, picks):
                        option_text = (c["options"][pick["index"]]
                                       if pick["index"] is not None else None)
                        applied = False
                        if option_text is not None:
                            if kind == "rs":
                                applied = await apply_react_select_choice(
                                    page, c["container_index"], option_text)
                            else:
                                applied = await apply_button_choice(
                                    page, c["selectors"][pick["index"]])
                        success += merge_custom_pick(
                            c["question_text"], option_text, bool(pick["backed"]),
                            applied, sources, review_items, choice_picks, drafted,
                            unfilled_custom)
        else:
            # No drafting: custom button groups would otherwise be a SILENT miss
            # (not filled, not reported) — surface them as unfilled for the
            # report/submit gate. (React-selects are covered by the scan below.)
            try:
                unfilled_custom += [g["question_text"]
                                    for g in await harvest_button_groups(page)]
            except Exception:
                pass

        # A field the analyzer logged "unknown" may still have been filled by a
        # downstream widget handler — fill_comboboxes_known picks a Workable combobox's
        # option, which populates a SEPARATE backing input the analyzer saw as an empty
        # text field. Re-read live values so a genuinely-populated field is not falsely
        # reported unfilled (false human task + false submit-gate block).
        unfilled = []
        for i, q in enumerate(unknown):
            if i in answered_idx:
                continue
            sel = q.get("selector")
            if sel and q.get("type") not in ("radio_group", "checkbox_group"):
                try:
                    if await page.locator(sel).first.evaluate(
                        "el => { const t=(el.tagName||'').toLowerCase();"
                        " if(t==='select') return el.selectedIndex>0 && !!el.value;"
                        " return !!(el.value && String(el.value).trim()); }",
                            timeout=1500):
                        continue
                except Exception:
                    pass
            unfilled.append(q.get("question_text", ""))
        unfilled += [t for t in unfilled_custom if t not in unfilled]
        # A REQUIRED standard field whose fill returned False (e.g. Lever 'Current location'
        # the dead-geocode couldn't set) is not an unknown_question, so it would otherwise
        # never surface — a phantom 'form complete' over a blank required field.
        unfilled += [t for t in failed_required if t not in unfilled]
        # Safety net for BOTH modes: any react-select still on its placeholder is
        # invisible to the analyzer (its typeahead input is skipped) — without this
        # scan the report/submit gate would treat the form as complete.
        try:
            for t in await list_unanswered_react_selects(page):
                if t not in unfilled:
                    unfilled.append(t)
        except Exception:
            pass
        sources["human"] = len(unfilled)

        # Wait for the résumé upload to finish before returning. Ashby (and others) upload the
        # attached résumé to their storage in the BACKGROUND; the fill (replaying a cached
        # draft) completes in a few seconds while that upload takes ~10-15s. If the human
        # clicks Submit before it lands, Ashby blocks with "We're updating your forms (e.g.
        # uploading files), please try again when they're finished" — which reads like a
        # not-filled error even though every field is set. Settle the network (bounded) so
        # the fill isn't 'done' until the upload is, and a same-second Submit goes through.
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass

        return {
            "strategy": self.name,
            "page_type": page_type,
            "filled": success,
            "failed": fail,
            "unfilled": unfilled,
            "drafted_answers": drafted,   # human reviews/edits these before submit
            "review_items": review_items,  # filled, but need a human eye before submit
            "choice_picks": choice_picks,  # {q: {option, backed}} — all LLM choice picks
            "answer_sources": sources,
            "dropdowns": ds["handled"],   # react-select eligibility dropdowns auto-answered
            "submit_selector": analysis.get("submit_selector"),
            "submitted": False,  # no auto-submit path — a human always clicks Submit
        }

    async def _prefill_resume_parser_only(self, page: Page, resume_path: str) -> dict:
        """Résumé-parser-only pre-fill (anti-spam mode).

        Upload the résumé so the ATS's OWN parser populates as many fields as it can,
        attach the résumé, then STOP. Deliberately types NOTHING into text fields,
        dropdowns, or screeners — that machine-gun programmatic fill is the automation
        tell that trips ATS spam detection. The human fills the remaining required
        fields (surfaced in report['completeness']) and clicks Submit.
        """
        page_type = await detect_page_type(page)
        base = {
            "strategy": self.name, "page_type": page_type,
            "mode": "resume_parser_only", "failed": 0,
            "unfilled": [], "drafted_answers": {}, "review_items": [],
            "choice_picks": {}, "dropdowns": [], "submit_selector": None,
            "submitted": False,
        }
        if page_type in ("login_required", "captcha", "expired"):
            return {**base, "filled": 0, "resume_autofill": False,
                    "resume_attached": False, "answer_sources": {},
                    "note": f"stopped: page_type={page_type}"}

        before = await _snapshot_values(page)
        autofilled = False
        try:  # Ashby's dedicated "Autofill from resume" parser input (no-op elsewhere)
            autofilled = await self.autofill_from_resume(page, resume_path)
        except Exception as exc:
            logger.debug("autofill_from_resume raised: %s", exc)
        attached = False
        try:  # the real résumé attachment (also triggers the parser on GH/Lever/etc.)
            attached = await self.attach_resume(page, resume_path)
        except Exception as exc:
            logger.debug("attach_resume raised: %s", exc)
        if autofilled or attached:
            await page.wait_for_timeout(2500)  # catch late server-side parser stragglers
        after = await _snapshot_values(page)
        filled = _count_newly_filled(before, after)
        logger.info("resume-parser-only: autofill=%s attach=%s -> parser filled ~%d fields",
                    autofilled, attached, filled)
        return {**base, "filled": filled, "resume_autofill": autofilled,
                "resume_attached": attached, "answer_sources": {"parser": filled}}

    async def _fill_choice(self, page: Page, q: dict, index: int) -> bool:
        """Apply a chosen option: select by label, or check the radio/checkbox input."""
        try:
            if q.get("type") in ("radio_group", "checkbox_group"):
                sels = q.get("option_selectors") or []
                if index >= len(sels) or not sels[index]:
                    return False
                # check_input, not bare check(): Workable hides the real input
                # behind a styled wrapper — the wrapper gets clicked instead.
                return await check_input(page, sels[index])
            else:
                from backend.applier.filler import select_dropdown
                if not await select_dropdown(page, q["selector"], q["options"][index]):
                    return False
            return True
        except Exception as e:
            logger.debug("choice fill failed for %r: %s",
                         q.get("question_text", "")[:40], e)
            return False

class GenericStrategy(ApplyStrategy):
    name = "generic"

    @classmethod
    def matches(cls, url: str) -> bool:
        return True  # fallback for any URL

    async def open_form(self, page: Page) -> None:
        for sel in [
            'button:has-text("Apply for this job")', 'a:has-text("Apply for this job")',
            'button:has-text("Apply Now")', 'a:has-text("Apply Now")',
            'button:has-text("Apply")', 'a:has-text("Apply")',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=800):
                    await btn.click()
                    await page.wait_for_timeout(1500)
                    return
            except Exception:
                continue
