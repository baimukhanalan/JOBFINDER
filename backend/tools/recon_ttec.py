"""TTEC (Oracle Taleo Enterprise) apply recon + first-cut driver plan.

READ-ONLY probe helpers + the build plan for auto-applying to every TTEC vacancy end-to-end,
mirroring the Teleperformance/iCIMS breakthrough (`backend/tools/icims_recon.py`): a headful
Chromium on DISPLAY=:98, a FRESH synthetic persona per job, a per-job isolated profile dir, the
persona's @takhet.com mailbox for the emailed verification code, and the tailored resume PDF.

DO NOT confuse TTEC with Teleperformance:
  * Teleperformance  -> iCIMS  (hCaptcha on every submit; needs the paid NopeCHA key)
  * TTEC             -> Oracle TALEO ENTERPRISE  (careersection JSF app; NO captcha observed)

Flow, established by probe (2026-09-01, datacenter IP, real-browser UA):
  1. The mass_hiring_jobs.apply_url is a Radancy TalentBrew LISTING page
     (https://www.ttecjobs.com/en/job/<city>/<slug>/44028/<jobid>). It is NOT the apply form.
     The page embeds exactly ONE per-job Taleo apply link:
         https://ttec.taleo.net/careersection/jobapply.ftl?job=<REQCODE>   (e.g. 04DW3, 04DBX, 04CK5)
     -> resolve_taleo_url() below does GET + regex to get it.
  2. The Taleo URL 302 -> /careersection/<N>/jobapply.ftl?job=<REQCODE>  (N is assigned dynamically;
     just follow redirects). HTTP 200, sets JSESSIONID. NO Akamai/WAF, NO 403, NO captcha script
     (no google.com/recaptcha, gstatic, hcaptcha, sitekey anywhere in the rendered pages).
  3. First interactive gate = a "Privacy Agreement" modal (statementBeforeAuthentification.jsf) with
     an "I Accept" button (name=...StatementBeforeAuthentificationContent-ContinueButton). Click it.
  4. Sign In / New User. Taleo apply is ACCOUNT-GATED: you must register (email + username + password)
     to submit. Classic Taleo Enterprise creates the account inline; some tenants email a verification
     code/link. EITHER WAY it is handled: register with the persona's @takhet.com address and, if a
     code is required, read it with backend.tools.verify_code.read_code(email, since_ts).
  5. Multi-step JSF wizard -> "Thank you for applying":
        Personal Info (name/email/phone/address+STATE) -> Resume upload (attach the tailored PDF; Taleo
        can also parse it to autofill) -> Work Experience / Education -> Prescreening Questions
        (Yes/No + selects: work authorization, 18+, background-check consent, shift/availability, the
        bespoke "are you located in the state of <X>?" residence screener on state-specific reqs,
        language fluency on the bilingual reqs) -> EEO / self-ID (decline: "I choose not to
        disclose") -> eSignature (type full name to certify) -> Review -> Submit.
     None of these is captcha- or assessment-gated; Taleo submits from the datacenter IP.
  6. Ground truth = the Taleo "Thank you for applying / We have received your application" email in the
     persona's @takhet.com Maildir (helper: reuse icims_recon._app_confirmed). TTEC gates HIRE behind a
     later post-application assessment (an emailed link, arrives AFTER submit) -> that is the HUMAN step,
     exactly like TP's AMCAT and Maximus's SHL. Auto-apply reaches "Thank you for applying"; the
     assessment is out of scope (aptitude/cognitive -> project hard boundary = human-only).

ELIGIBILITY (a synthetic persona must FIT the job, truthful-by-design):
  * 27 active TTEC rows. 4 are LICENSED insurance-agent roles (ids 506/511/513/529: "Licensed
    Healthcare Insurance Agent", "Licensed Property & Casualty Insurance Agent"). They gate on holding
    an ACTIVE state insurance license (a real credential a synthetic persona cannot truthfully hold) ->
    SKIP by design (same policy as never attaching a fabricated medical report / diploma). is_licensed().
  * 18 state-specific rows. 15 are "... Remote in California" bilingual reqs, plus VA/NC/LA-or-MN.
    The state is in the TITLE (the connector hardcodes location_raw="Remote, United States" and drops
    it). Put the persona IN that state so the "are you located in the state of <X>?" screener answers
    Yes truthfully (residence is a synthetic-persona DESIGN attribute) -> ttec_state() below.
    NOTE: icims_recon.ALLOWED_STATES is TP's business list and EXCLUDES California; TTEC genuinely
    hires WAH in CA (it is actively posting CA-only reqs), so TTEC needs its own state table
    (TTEC_STATE_CITY below, CA included) -- do NOT reuse the TP allow-list verbatim.
  * Bilingual reqs (Spanish/Vietnamese/Tagalog/Russian/Mandarin/Korean/Farsi/Cantonese/Cambodian/
    Armenian/Arabic/Hmong/Laotian) gate on speaking that language -> design the persona bilingual in
    that language (same as synth_persona / the Avature _job_bilingual pattern). ttec_language() below.

WHAT IS MISSING (the build, needs live browser iteration -- cannot be driven from this recon shell):
  * No taleo.py strategy exists. Build backend/applier/strategies/taleo.py (matches "taleo.net") OR,
    faster to ship, a taleo_recon.py harness cloned from icims_recon.py: same patchright headful setup
    on :98, per-job ICIMS_PROFILE_DIR-style isolated profile, _build_persona, the generic _CAP_JS
    field-capture (ATS-agnostic -- already works on any DOM incl. Taleo's JSF), and a wizard-walk loop
    tuned to the Taleo step names above. NopeCHA is NOT needed (no captcha), so it can run key-free.
  * Taleo's JSF partial-postback DOM (ViewState, IFrame-heavy) must be observed live to write robust
    selectors for each step's fields/buttons -- that is the live-iteration work.
  * A run command (mirror of the TP lane):
        DISPLAY=:98 sg mail -c 'cd /home/projects/jobfinder && \
            python3 -m backend.tools.taleo_recon --job <mass_hiring_id> --fresh'
    then a sequential cron lane like mass_hiring_apply_tp_cron.py over
        WHERE source='ttec' AND active AND NOT is_licensed(title).

Usage (read-only, safe):  python3 -m backend.tools.recon_ttec [--job <id>]
"""
from __future__ import annotations

import argparse
import re
import sys

import httpx

from backend.tools import mail_db

_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
       # Taleo mislabels content-encoding on redirect hops -> httpx DecodingError; force uncompressed.
       "Accept-Encoding": "identity"}

# TTEC-specific work-state table (Columbus-style city + a plausible ZIP). CA is included (TP's list
# omits it, but TTEC hires WAH in California). Extend as new state-specific reqs appear.
TTEC_STATE_CITY = {
    "CA": ("California", "Los Angeles", "90012"),
    "VA": ("Virginia", "Richmond", "23219"),
    "NC": ("North Carolina", "Charlotte", "28202"),
    "LA": ("Louisiana", "New Orleans", "70112"),
    "MN": ("Minnesota", "Minneapolis", "55401"),
    "TX": ("Texas", "Austin", "78701"),
    "FL": ("Florida", "Orlando", "32801"),
    "GA": ("Georgia", "Atlanta", "30303"),
    "OH": ("Ohio", "Columbus", "43215"),
}
_DEFAULT_STATE = ("OH", "Ohio", "Columbus", "43215")

_LICENSE_RE = re.compile(r"licens|licence|insurance agent|property\s*&?\s*casualty|\bP&C\b", re.I)
_LANG_RE = re.compile(
    r"\b(Spanish|Vietnamese|Tagalog|Russian|Mandarin|Korean|Farsi|Cantonese|Cambodian|"
    r"Armenian|Arabic|Hmong|Laotian|Portuguese|French)\b", re.I)
_TALEO_RE = re.compile(r"(ttec\.taleo\.net/careersection/[^\s\"'<>]*jobapply\.ftl\?job=[A-Za-z0-9]+)", re.I)


def is_licensed(title: str) -> bool:
    """A licensed insurance-agent role -> skip (a synthetic persona can't hold a real state license)."""
    return bool(_LICENSE_RE.search(title or ""))


def ttec_state(title: str) -> tuple[str, str, str, str]:
    """(code, full, city, zip) for a TTEC-allowed state named in the TITLE; else default Ohio.
    Matches a full state name ('Remote in California') or a trailing 2-letter code."""
    t = title or ""
    low = t.lower()
    for code, (full, city, zc) in TTEC_STATE_CITY.items():
        if full.lower() in low:
            return code, full, city, zc
    m = re.search(r"[-–]\s*([A-Z]{2})\b", t)
    if m and m.group(1) in TTEC_STATE_CITY:
        code = m.group(1); full, city, zc = TTEC_STATE_CITY[code]
        return code, full, city, zc
    return _DEFAULT_STATE


def ttec_language(title: str) -> str | None:
    """The second language a bilingual req gates on -> design the persona bilingual in it. Else None."""
    m = _LANG_RE.search(title or "")
    return m.group(1).title() if m else None


def resolve_taleo_url(listing_url: str) -> str | None:
    """GET the ttecjobs.com listing page and return its embedded per-job Taleo apply URL, or None."""
    try:
        r = httpx.get(listing_url, headers=_UA, timeout=30, follow_redirects=True)
    except Exception as e:  # noqa: BLE001
        print(f"  resolve error: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    m = _TALEO_RE.search(r.text)
    return ("https://" + m.group(1)) if m else None


def probe(job_id: int) -> None:
    """Read-only: resolve one job to its Taleo URL and report the gate. NEVER submits."""
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id,title,apply_url FROM mass_hiring_jobs WHERE id=%s", (job_id,))
        row = cur.fetchone()
    if not row:
        print(f"job {job_id} not found"); return
    _id, title, listing = row
    print(f"job {_id}: {title}")
    print(f"  licensed(skip)={is_licensed(title)}  state={ttec_state(title)}  language={ttec_language(title)}")
    taleo = resolve_taleo_url(listing)
    print(f"  taleo apply URL: {taleo}")
    if not taleo:
        return
    r = httpx.get(taleo, headers=_UA, timeout=30, follow_redirects=True)
    body = r.text
    gate = ("privacy-agreement -> register/login (account-gated), NO captcha"
            if re.search(r"Privacy Agreement|I Accept", body, re.I) else "unexpected entry page")
    has_captcha = bool(re.search(r"recaptcha|hcaptcha|sitekey|grecaptcha", body, re.I))
    print(f"  HTTP {r.status_code}  final={r.url}  size={len(body)}")
    print(f"  gate: {gate}   captcha_script_present={has_captcha}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", type=int, default=504, help="a mass_hiring_jobs id (source='ttec') to probe")
    ap.add_argument("--all", action="store_true", help="list eligibility for every active TTEC row")
    args = ap.parse_args()
    if args.all:
        with mail_db.conn() as c:
            cur = c.cursor()
            cur.execute("SELECT id,title FROM mass_hiring_jobs WHERE source='ttec' AND active ORDER BY id")
            rows = cur.fetchall()
        skip = sum(is_licensed(t) for _, t in rows)
        print(f"{len(rows)} active TTEC rows; {skip} licensed(skip); {len(rows)-skip} auto-applyable")
        for _id, t in rows:
            tag = "SKIP-licensed" if is_licensed(t) else f"state={ttec_state(t)[0]} lang={ttec_language(t)}"
            print(f"  {_id}: {tag:28} {t}")
    else:
        probe(args.job)


if __name__ == "__main__":
    main()
