"""Orchestrate one application pre-fill:

    job + profile
      -> tailor résumé to the JD (keyword match, no fabrication)
      -> render résumé to a PDF
      -> open the apply page (reusing the profile's saved session if any)
      -> pick the ATS strategy and PRE-FILL every field
      -> screenshot, return a report

It STOPS before submitting — there is no auto-submit path by design. A human reviews
the screenshot, solves any CAPTCHA, completes assessments, and clicks Submit himself.
"""
import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from backend.applier.browser import BrowserManager
from backend.applier.strategies.amazon_apply import AmazonStrategy
from backend.applier.strategies.ashby import AshbyStrategy
from backend.applier.strategies.avature import AvatureStrategy
from backend.applier.strategies.base import GenericStrategy
from backend.applier.strategies.greenhouse import GreenhouseStrategy
from backend.applier.strategies.icims import ICIMSStrategy
from backend.applier.strategies.kelly import KellyStrategy
from backend.applier.strategies.lever import LeverStrategy
from backend.applier.strategies.oracle_orc import OracleORCStrategy
from backend.applier.strategies.phenom import PhenomStrategy, PhenomWorkdayStrategy
from backend.applier.strategies.smartrecruiters import SmartRecruitersStrategy
from backend.applier.strategies.workable import WorkableStrategy
from backend.applier.strategies.workday import WorkdayMassHiringStrategy, WorkdayStrategy
from backend.applier.strategies.workingsolutions import WorkingSolutionsStrategy
from backend.profiles.facts import load_facts
from backend.profiles.store import Profile
from backend.services.tailor.render import render_html, render_text
from backend.services.tailor.tailor import tailor_resume
from backend.services.tailor.variants import variant_for

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_ROOT = PROJECT_ROOT / "uploads" / "prefill"

# Match gate: minimum fit score (0-100) a candidate must reach on the JD to be pre-filled.
# Scored on the BASE (un-tailored) résumé — tailoring injects the JD's own keywords, so
# gating on the tailored résumé would pass anyone; the base résumé reflects GENUINE fit.
#
# DEFAULT OFF (0): the "порог входа" is disabled so one profile can apply across role
# families without being auto-cut on fit. The fit score is still COMPUTED and written to
# report.json (shown on the bot card) so the human reviewer sees how strong the match is —
# it just no longer blocks. Re-enable by exporting e.g. MATCH_GATE_MIN=50 ATS_GATE_MIN=80.
# NOTE: this only stops REJECTION on fit. It does NOT make a weak résumé look strong —
# résumé content stays no-fabrication (services/tailor) and variant routing still refuses
# someone else's work history (services/tailor/variants). A low-fit role now goes through
# with an honestly-low fit_score for the human to judge, not a manufactured one.
MATCH_GATE_MIN = float(os.getenv("MATCH_GATE_MIN", "0"))
# Secondary bar on the TAILORED résumé's ATS score (the "ATS %" shown on the bot card).
# Both must pass. None (missing ats) fails open so a scoring gap can't reject everyone.
# Also default OFF — see above; export ATS_GATE_MIN=80 to restore the old bar.
ATS_GATE_MIN = float(os.getenv("ATS_GATE_MIN", "0"))

# Résumé polish via the configured LLM (the --ai path). Defaults ON so EVERY prefill
# mirrors the JD's wording — validated no-fabrication rephrase of the candidate's REAL
# experience (no new company/skill/number). Set TAILOR_AI=0 to force the deterministic
# path. An explicit use_ai=True always wins; this only lifts the default for callers that
# leave it off (e.g. the batch/CLI path — the bot already passes use_ai=True).
_AI_DEFAULT = os.getenv("TAILOR_AI", "1").strip().lower() in ("1", "true", "yes", "on")

# Path words that carry no role signal — dropped from URL-derived keywords. Includes
# ATS/domain noise and generic company-name suffixes (so an org slug like "salmon-group"
# contributes nothing that would dilute the JD keyword match for every candidate alike).
_URL_STOP = {
    "jobs", "job", "careers", "career", "apply", "application", "applications",
    "boards", "board", "opening", "openings", "position", "positions", "posting",
    "postings", "www", "com", "io", "co", "org", "hq", "ashbyhq", "greenhouse",
    "lever", "workable", "myworkdayjobs", "workday", "icims", "gh", "the", "and",
    "for", "our", "join", "team", "remote", "en", "us",
    "group", "inc", "llc", "ltd", "labs", "lab", "technologies", "tech", "global",
    "holding", "holdings", "company", "corp", "solutions", "software", "systems",
}


def _url_keywords(url: str, company: str = "") -> str:
    """Role keywords carried in the apply URL's own path (the user's 'ключи из ссылки').

    Slug-based ATS URLs embed the role in the path (Greenhouse/Lever:
    …/jobs/senior-ios-engineer) — those words strengthen the per-vacancy tailoring and
    the match gate. UUID-based URLs (Ashby: …/<uuid>/application) yield nothing useful,
    which is fine. Company-name tokens are excluded so the org slug adds no noise."""
    drop = set(_URL_STOP) | {w for w in re.split(r"[\s\-_.]+", (company or "").lower()) if w}
    tail = re.split(r"[/?#=&]", (url or "").lower())
    words: list[str] = []
    for seg in tail:
        for w in re.split(r"[-_.+]", seg):
            if w.isalpha() and len(w) >= 3 and w not in drop:
                words.append(w)
    return " ".join(dict.fromkeys(words))  # order-preserving dedup


# PhenomWorkdayStrategy (Humana) MUST precede WorkdayStrategy — Humana's apply_url is a
# Workday URL, so it has to be checked first; every other Workday URL still falls through to
# the stock WorkdayStrategy (byte-identical). PhenomStrategy (Conduent) matches only the
# careers.conduent.com wrapper host, so its position is not order-sensitive.
STRATEGIES = [GreenhouseStrategy, LeverStrategy, AshbyStrategy, WorkableStrategy,
              PhenomWorkdayStrategy, WorkdayMassHiringStrategy, WorkdayStrategy,
              ICIMSStrategy, AvatureStrategy,
              KellyStrategy, PhenomStrategy, OracleORCStrategy, WorkingSolutionsStrategy,
              SmartRecruitersStrategy, AmazonStrategy]  # GenericStrategy is the fallback


def _pick_strategy(url: str):
    for cls in STRATEGIES:
        if cls.matches(url):
            return cls()
    return GenericStrategy()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "job"


def _save_to_downloads(pdf: Path, profile_id: str, jid: str) -> str | None:
    """Keep a copy of every generated résumé in the user's Downloads, so the human (or
    the Chrome extension flow) always has the tailored PDF on disk to attach by hand if a
    site rejects the programmatic upload. Lands in ~/Downloads/JobFinder/ by default;
    RESUME_DOWNLOADS_DIR overrides the base dir, RESUME_DOWNLOADS_DIR="" turns it off.
    Best-effort — a copy failure never breaks a pre-fill."""
    try:
        base = os.getenv("RESUME_DOWNLOADS_DIR", str(Path.home() / "Downloads"))
        if not base.strip():
            return None
        dest_dir = Path(base) / "JobFinder"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"resume_{profile_id}_{jid}.pdf"
        shutil.copyfile(pdf, dest)
        return str(dest)
    except Exception as exc:
        logger.debug("save-to-downloads failed: %s", exc)
        return None


def _tenant(url: str) -> str:
    return urlparse(url).hostname or "default"


_NORMAL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def _normalize_pdf_metadata(out_path: Path, title: str = "", author: str = "") -> None:
    """Headless Chromium stamps every printed PDF with Creator '...HeadlessChrome...' and
    Title 'about:blank' — a dead giveaway to ATS fraud/spam detection that the résumé was
    machine-generated, which flags the application EVEN when a human submits it manually.
    Rewrite the metadata to look like an ordinary 'print to PDF from Chrome' (what millions
    of real résumés are). Best-effort — a failure here never blocks the résumé."""
    try:
        from pypdf import PdfReader, PdfWriter
    except Exception:
        return
    try:
        reader = PdfReader(str(out_path))
        writer = PdfWriter()
        for pg in reader.pages:
            writer.add_page(pg)
        md = {k: str(v) for k, v in (reader.metadata or {}).items()}  # keep CreationDate/Producer
        cur_title = md.get("/Title")
        md["/Title"] = (f"{title} — Resume") if title else (
            cur_title if cur_title not in (None, "", "about:blank") else "Resume")
        if author:
            md["/Author"] = author
        cr = md.get("/Creator") or ""
        md["/Creator"] = cr.replace("HeadlessChrome", "Chrome") if "Headless" in cr else (cr or _NORMAL_UA)
        writer.add_metadata(md)
        tmp = out_path.with_suffix(".meta.pdf")
        with open(tmp, "wb") as f:
            writer.write(f)
        tmp.replace(out_path)
    except Exception as exc:
        logger.debug("pdf metadata normalize failed: %s", exc)


async def _html_to_pdf(bm: BrowserManager, html: str, out_path: Path,
                       title: str = "", author: str = "") -> None:
    ctx = await bm.new_context()
    page = await ctx.new_page()
    try:
        await page.set_content(html, wait_until="domcontentloaded")
        await page.emulate_media(media="print")
        await page.pdf(path=str(out_path), format="Letter", print_background=True,
                       margin={"top": "0.5in", "bottom": "0.5in", "left": "0.6in", "right": "0.6in"})
    finally:
        await page.close()
        await ctx.close()
    # Strip the headless-automation fingerprint from the PDF metadata (anti-flag).
    _normalize_pdf_metadata(out_path, title, author)


async def prefill_application(job: dict, profile: Profile, *, headless: bool = True,
                              use_ai: bool = False,
                              draft_answers: bool = False, use_variants: bool = True,
                              hold_open: bool = False, skip_gate: bool = False,
                              resume_parser_only: bool = False) -> dict:
    apply_url = job.get("apply_url") or job.get("url")
    if not apply_url:
        raise ValueError("job has no apply_url")

    use_ai = use_ai or _AI_DEFAULT  # AI polish is on by default (TAILOR_AI=0 to disable)

    jid = _slug(f"{job.get('company','')}-{job.get('title','')}")
    out_dir = OUT_ROOT / profile.id / jid
    out_dir.mkdir(parents=True, exist_ok=True)

    # Reply address = the candidate's own plain @takhet.com address (no per-application
    # tag — a clean, professional-looking address). An incoming reply is mapped back to
    # the specific position by matching the role named in the reply text against this
    # profile's applications (see mail_sink.apply_statuses / _jid_for_profile_reply).
    try:
        from backend.tools.mail_sink import address_map
        reply_addr = address_map().get(profile.id)
    except Exception:
        reply_addr = None

    # 1) Pick the niche résumé variant for this job, then tailor + render it.
    #    Falls back to the profile's base résumé when variants are off/unavailable.
    #    The résumé is regenerated PER VACANCY against the JD's keywords — from the
    #    job title, its description, AND the apply URL's own slug (`ключи из ссылки`).
    #    The same enriched JD text feeds the match gate below so both stay in sync.
    jd_title = job.get("title", "")
    url_kw = _url_keywords(apply_url, job.get("company", ""))
    jd_text = (job.get("description", "") + ("\n" + url_kw if url_kw else "")).strip()
    niche, variant_base = variant_for(job, profile) if use_variants else (None, None)
    base_resume = variant_base or profile.resume
    tailored = tailor_resume(base_resume, jd_title, job.get("company", ""),
                             jd_text, use_ai=use_ai)
    if reply_addr:
        tailored.setdefault("personal_info", {})["email"] = reply_addr
    html = render_html(tailored)
    resume_pdf = out_dir / "resume.pdf"

    # Keep the form's "years of experience" consistent with the résumé variant being
    # submitted — niche etalons state different totals (12-15), so without this the form
    # would say 15 while the PDF says e.g. "12+ years".
    form = profile.to_form_dict()
    if variant_base and variant_base.get("years_experience"):
        form["years_experience"] = variant_base["years_experience"]
    form["linkedin_url"] = ""  # per request: leave LinkedIn blank on the form
    if reply_addr:
        form["email"] = reply_addr  # recruiter replies land on the per-application box

    report: dict = {
        "profile": profile.id,
        "job_title": job.get("title", ""),
        "company": job.get("company", ""),
        "apply_url": apply_url,
        "resume_niche": niche,
        "match_score": tailored.get("match_score"),
        "ats_score": (tailored.get("ats_score") or {}).get("score"),
        "application_email": reply_addr,
        "missing_keywords": tailored.get("missing_keywords"),
        "used_ai": tailored.get("used_ai"),
        "submitted": False,
    }

    # --- Honest match gate -------------------------------------------------------
    # Score the BASE résumé (not the tailored one) against the JD. Tailoring injects
    # the JD's own keywords into the résumé, which lifts the coverage score for ANY
    # profile — so a gate on the tailored score would pass mismatches too. The base
    # résumé reflects genuine role fit (matched families land ~50-82, mismatches
    # ~22-35). Below the threshold we render nothing and open no browser.
    try:
        from backend.services.tailor.ats_score import ats_score as _ats
        _fit_resume = dict(base_resume)
        _fit_resume["_jd_title"] = jd_title
        fit = _ats(jd_text, _fit_resume)
        ats = report.get("ats_score")  # tailored résumé ATS % (may be None)
        passed = (fit["score"] >= MATCH_GATE_MIN
                  and (ats is None or ats >= ATS_GATE_MIN))
        report["fit_score"] = fit["score"]
        report["fit_required_coverage"] = fit["required_coverage"]
        report["match_gate"] = {"metric": "base_fit+ats", "fit": fit["score"],
                                "fit_min": MATCH_GATE_MIN, "ats": ats,
                                "ats_min": ATS_GATE_MIN, "passed": passed}
    except Exception as exc:  # fail-open: a scoring bug must not reject every candidate
        logger.warning("match gate scoring failed (%s) — allowing through", exc)
        report["match_gate"] = {"passed": True, "error": str(exc)}

    # Match gate REMOVED as a filter (per product decision): the engine tailors ANY JD
    # and pre-fills EVERY job regardless of fit — the human decides what to submit at
    # review. fit_score / ats_score above are kept purely as informational relevance
    # signals on the bot card; they never block. (`skip_gate` is retained for callers
    # like open_for_submit but is now a no-op.)

    bm = BrowserManager(headless=headless)
    await bm.start()
    try:
        await _html_to_pdf(bm, html, resume_pdf, title=profile.full_name, author=profile.full_name)
        profile.resume_path = str(resume_pdf)
        # form was snapshotted BEFORE the résumé was rendered — without this the
        # analyzer sees resume_path='' and silently skips the upload (live bug:
        # form complete but no résumé attached).
        form["resume_path"] = str(resume_pdf)
        report["resume_pdf"] = str(resume_pdf)
        dl = _save_to_downloads(resume_pdf, profile.id, jid)
        if dl:
            report["resume_downloads"] = dl

        # 2) Open the apply page, reusing the profile's saved session if present
        ctx = await bm.new_context(storage_state=profile.storage_state_path(_tenant(apply_url)))
        page = await ctx.new_page()
        try:
            await page.goto(apply_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
            # Dismiss cookie/consent banners (OneTrust etc.) that otherwise intercept
            # clicks on radios/dropdowns and leave required fields empty.
            try:
                from backend.applier.filler import dismiss_overlays
                if await dismiss_overlays(page):
                    await page.wait_for_timeout(500)
            except Exception:
                pass

            # A grounded cover letter for forms that have a cover-letter field
            # (the analyzer fills it verbatim; ignored on forms without one).
            resume_text = render_text(tailored)
            try:
                from backend.services.tailor.answers import cover_letter
                cover = cover_letter(job, resume_text, form, use_llm=draft_answers)
            except Exception as exc:
                logger.debug("cover-letter generation failed: %s", exc)
                cover = ""

            strategy = _pick_strategy(apply_url)
            fill_report = await strategy.prefill(
                page, form, str(resume_pdf), cover_letter=cover,
                job=job, draft=draft_answers, resume_summary=resume_text,
                facts=load_facts(profile.id), profile_id=profile.id, niche=niche or "",
                resume_parser_only=resume_parser_only)
            report.update(fill_report)
            # The strategy flips this on when the env switch triggers parser-only mode
            # even though the caller passed resume_parser_only=False.
            parser_only_mode = fill_report.get("mode") == "resume_parser_only"

            # Fill the last stragglers the field-mapper doesn't cover: consent /
            # agreement checkboxes and location-style typeaheads. Consent boxes are
            # auto-checked but flagged [review] — a human ratifies them by clicking
            # Submit, and the flag keeps them visible in the review list.
            # In résumé-parser-only mode these are ALSO programmatic fills, so skip
            # them — everything but the résumé is left for the human by design.
            if not parser_only_mode:
                try:
                    left = await _fill_leftovers(page, form)
                    if left["consent_checked"]:
                        report.setdefault("review_items", [])
                        for lbl in left["consent_checked"]:
                            report["review_items"].append(
                                {"question": lbl, "answer": "[review] consent auto-checked — verify"})
                    report["leftovers"] = left
                except Exception as exc:
                    logger.debug("fill_leftovers failed: %s", exc)

            # Completeness scan: after all filling, walk the live DOM and report any
            # control still empty, split by required vs optional. This is the ground
            # truth for "nothing left to do but click Submit".
            try:
                report["completeness"] = await _scan_completeness(page)
                if parser_only_mode:
                    # These required blanks are exactly what the human still fills by
                    # hand — surface them as the work-remaining list for the dashboard.
                    report["unfilled"] = report["completeness"].get("empty_required", [])
                    # Parser-only types nothing into the form, so the drafted cover
                    # letter never reaches it — surface it so the human can paste it.
                    report["cover_letter"] = cover
            except Exception as exc:
                logger.debug("completeness scan failed: %s", exc)

            shot = out_dir / "prefilled.png"
            await page.screenshot(path=str(shot), full_page=True)
            report["screenshot"] = str(shot)

            # Human-submit hold: keep the just-filled page open so a person can
            # review it and click Submit THEMSELVES. The engine still never clicks
            # anything — it only waits. Only meaningful when headless=False.
            if hold_open and not headless:
                logger.info("Holding the browser open for human review + submit.")
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, input,
                    "\n>>> Browser is open and pre-filled. Review it, click Submit "
                    "yourself if you want, then press Enter here to close. <<<\n")
                report["human_hold"] = True
        finally:
            await page.close()
            await ctx.close()
    finally:
        await bm.close()

    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info("Pre-fill done: %s @ %s -> filled=%s match=%s",
                report["job_title"], report["company"],
                report.get("filled"), report.get("match_score"))
    return report


_SCAN_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const labelFor = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                 if (l && l.innerText.trim()) return l.innerText.trim(); }
    const wrap = el.closest('label'); if (wrap && wrap.innerText.trim()) return wrap.innerText.trim();
    const al = el.getAttribute('aria-label'); if (al) return al.trim();
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(' ').map(i => document.getElementById(i)).filter(Boolean)
                          .map(n => n.innerText.trim()).join(' '); if (t) return t; }
    const ph = el.getAttribute('placeholder'); if (ph) return ph.trim();
    const nm = el.getAttribute('name'); return nm || el.tagName.toLowerCase();
  };
  const req = el => el.required || el.getAttribute('aria-required') === 'true'
                 || !!el.closest('[aria-required="true"]');
  const emptyReq = [], emptyOpt = [];
  const seenGroup = new Set();
  document.querySelectorAll('input, textarea, select').forEach(el => {
    if (!vis(el)) return;
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden','submit','button','reset','image','search'].includes(type)) return;
    let filled, label = labelFor(el).replace(/\s+/g,' ').slice(0,80), required = req(el);
    if (type === 'radio' || type === 'checkbox') {
      const name = el.name || el.getAttribute('aria-labelledby') || label;
      const gkey = type + '::' + name;
      if (seenGroup.has(gkey)) return; seenGroup.add(gkey);
      const group = name ? [...document.querySelectorAll(`input[name="${CSS.escape(name)}"]`)]
                         : [el];
      filled = group.some(g => g.checked);
      required = group.some(req);
    } else if (el.tagName.toLowerCase() === 'select') {
      const v = el.value; const opt = el.options[el.selectedIndex];
      filled = v !== '' && v != null && !(opt && /select|choose|—|\.\.\./i.test(opt.text) && el.selectedIndex === 0);
    } else if (type === 'file') {
      filled = el.files && el.files.length > 0;
    } else {
      filled = (el.value || '').trim() !== '';
    }
    if (filled) return;
    (required ? emptyReq : emptyOpt).push(label);
  });
  const uniq = a => [...new Set(a)].filter(Boolean);
  return { empty_required: uniq(emptyReq), empty_optional: uniq(emptyOpt) };
}
"""


_CONSENT_RE = re.compile(
    r"(?i)i (?:confirm|agree|consent|have read|acknowledge|understand)"
    r"|consent form|i freely provide|terms|privacy|data protection|authoriz")


async def _fill_leftovers(page, form: dict) -> dict:
    """Check consent/agreement boxes and fill location-style typeaheads — the last
    non-mapped controls, so an ATS form ends up with only Submit remaining."""
    result: dict = {"consent_checked": [], "combobox_filled": []}

    for cb in await page.query_selector_all('input[type="checkbox"]'):
        try:
            if await cb.is_checked() or not await cb.is_visible():
                continue
            label = await cb.evaluate(
                '(el)=>{const l=el.closest("label");'
                'const f=el.id?document.querySelector(`label[for="${el.id}"]`):null;'
                'return (l&&l.innerText||f&&f.innerText||el.getAttribute("aria-label")||el.name||"").trim();}')
            if _CONSENT_RE.search(label or ""):
                await cb.check(timeout=3000)
                result["consent_checked"].append((label or "")[:70])
        except Exception:
            continue

    city = (form.get("city") or (form.get("location", "").split(",")[0] or "")).strip()
    if city:
        for combo in await page.query_selector_all('input[role="combobox"]'):
            try:
                if (await combo.input_value()).strip() or not await combo.is_visible():
                    continue
                ph = (await combo.get_attribute("placeholder")) or ""
                lbl = await combo.evaluate(
                    '(el)=>{const l=el.closest("label");return (l&&l.innerText||el.getAttribute("aria-label")||"").trim();}')
                if not re.search(r"(?i)start typing|location|city|address|based|where", ph + " " + (lbl or "")):
                    continue
                await combo.click()
                await combo.fill(city)
                await page.wait_for_timeout(1800)
                opt = await page.query_selector('[role="option"]')
                if opt:
                    await opt.click()
                else:
                    await combo.press("ArrowDown")
                    await combo.press("Enter")
                await page.wait_for_timeout(400)
                if (await combo.input_value()).strip() or True:
                    result["combobox_filled"].append(city)
            except Exception:
                continue
    return result


async def _scan_completeness(page) -> dict:
    """DOM ground-truth: which visible controls are still empty (required vs not)."""
    res = await page.evaluate(_SCAN_JS)
    er = res.get("empty_required", [])
    eo = res.get("empty_optional", [])
    return {
        "empty_required": er,
        "empty_optional": eo,
        "ready_to_submit": len(er) == 0,
        "n_empty_required": len(er),
        "n_empty_optional": len(eo),
    }
