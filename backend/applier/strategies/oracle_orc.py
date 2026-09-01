"""Oracle Recruiting Cloud (ORC) / Candidate Experience (CX) pre-fill strategy.

Alorica and other high-volume BPOs host their careers on Oracle's SaaS Candidate
Experience site (e.g. `fa-euxw-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/
CandidateExperience/en/sites/CX_1/job/<id>`). Like Greenhouse/Ashby it is a
LOGIN-LESS guest apply, but the flow is a multi-step wizard:

    job page → click Apply (startApplication) → Personal info → optional
    pre-screening Yes/No → Diversity/EEO → Review → Submit

The only anti-bot at the end is (a) an emailed PIN (machine-readable from the
persona's Maildir, exactly like the GH/Ashby "security code" — the co-pilot's
`_watch_submit` fills it) and (b) an INVISIBLE reCAPTCHA v3 the page JS executes
itself — there is NO interactive captcha, no account wall, no video/voice
assessment. So the ceiling here is a full auto-submit, making Oracle ORC the 2nd
fully-autonomous ATS on the Mass Hiring board after Maximus/Avature.

The one thing the generic engine can't do is Oracle's JET custom elements
(`oj-input-text`, `oj-select-single`, `oj-radioset`/`oj-checkboxset`,
`oj-file-picker`): the analyzer doesn't recognize them, and a JET select needs a
click→type→pick type-ahead, never a plain `.fill`. This strategy adds exactly that
component-aware fill layer plus a wizard-walker, and reuses the shared pipeline
(`base.prefill`) for every ordinary input.

Nothing here clicks the FINAL Submit — like every strategy it fills and STOPS; the
application is transmitted only when that final button is pressed (by the co-pilot's
gated auto-submit, or a human). Walking the wizard past step 1 is itself gated behind
env `ORC_ADVANCE` (mirrors Avature's `AVATURE_ADVANCE`), so a plain fill / dry-run is
entirely side-effect-free at the employer.
"""
import logging
import os
import re

from playwright.async_api import Page

from backend.applier.analyzer import analyze_page, find_submit_button
from backend.applier.dropdowns import (
    fill_demographic_checkboxes_decline,
    fill_demographics_decline,
    fill_required_consent,
)
from backend.applier.filler import fill_form
from backend.applier.strategies.base import GenericStrategy

logger = logging.getLogger(__name__)

# A wizard "advance" button (Oracle CX renders it as an <oj-button> with text
# "Continue"/"Next"; the final Review step's button reads Submit). We advance on
# continue/next and STOP (record the selector) on submit.
_ADVANCE_RE = re.compile(r"^\s*(continue|next|save (and|&) continue|review)\s*$", re.I)
_SUBMIT_RE = re.compile(r"submit|finish|complete|send application", re.I)
# Oracle CX buttons are <oj-button> custom elements (with an inner <button>), plain
# <button>s, and occasionally role=button links.
_WIZARD_BTN = "oj-button, button, a[role='button']"


def _env_advance() -> bool:
    """True only when ORC_ADVANCE is explicitly set — the live-submit switch that lets the
    strategy walk the wizard past step 1 (which transmits PII, and the final Submit sends the
    application). OFF by default: a plain fill (co-pilot dry-run / human review) stays entirely
    side-effect-free at the employer. Mirrors Avature's AVATURE_ADVANCE gate."""
    return os.getenv("ORC_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


class OracleORCStrategy(GenericStrategy):
    name = "oracle_orc"
    # Whether to WALK the wizard past step 1 (Continue → EEO → Review → the final Submit
    # button). OFF by default for the same reason as Avature — see _env_advance. The real
    # auto-submit path sets this True (env ORC_ADVANCE=1), the same way the rest of the engine
    # gates its live actions.
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        u = (url or "").lower()
        if "oraclecloud.com" not in u:
            return False
        # The CX apply surface is /hcmUI/CandidateExperience/…; be tolerant and also accept a
        # bare /sites/<CX>/job/<id> shape (some tenants shorten the path). This keeps other
        # oraclecloud.com hosts (object storage, APEX, docs) OUT.
        return ("/hcmui/candidateexperience/" in u
                or ("/sites/" in u and "/job/" in u))

    async def open_form(self, page: Page) -> None:
        # The apply URL IS the job page; the runner / co-pilot already navigated here, so we
        # never re-goto — we just START the guest application. Dismiss the cookie banner FIRST
        # (before any fill, so it never resets a filled field or intercepts the Apply click).
        await self._dismiss_cookie_banner(page)
        try:
            await self._click_apply(page)
        except Exception as exc:
            logger.debug("oracle_orc: open_form apply click raised: %s", exc)
        # A late-appearing cookie/consent overlay on the first wizard step.
        await self._dismiss_cookie_banner(page)

    async def _click_apply(self, page: Page) -> None:
        """Click the job page's Apply button to start the guest flow, then pick the MANUAL
        (email) option if Oracle shows a 'How would you like to apply?' chooser. Best-effort:
        many CX sites go straight to the form on Apply, so a missing chooser is normal."""
        for sel in ('button:has-text("Apply Now")', 'a:has-text("Apply Now")',
                    'button:has-text("Apply")', 'a:has-text("Apply")',
                    'button[title*="Apply" i]', '[data-bind*="applyNow" i]',
                    'oj-button:has-text("Apply")'):
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible(timeout=1000):
                    await btn.click()
                    await page.wait_for_timeout(2500)
                    break
            except Exception:
                continue
        # Oracle sometimes offers "Apply Manually" / "Use my email" vs LinkedIn/Indeed — take
        # the manual/email path (guest, no third-party account). Deliberately NO "Continue"
        # here so we never accidentally advance the wizard past step 1.
        for sel in ('button:has-text("Apply Manually")', 'a:has-text("Apply Manually")',
                    'button:has-text("Fill out application")',
                    'button:has-text("Use my Email")', 'button:has-text("Manually")',
                    'oj-button:has-text("Apply Manually")'):
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible(timeout=800):
                    await btn.click()
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

    async def prefill(self, page: Page, profile_form: dict, resume_path: str,
                      cover_letter: str = "", job: dict | None = None,
                      draft: bool = False, resume_summary: str = "",
                      known_answers: dict | None = None,
                      facts: dict | None = None,
                      profile_id: str = "default", niche: str = "",
                      resume_parser_only: bool = False) -> dict:
        # super().prefill (via our open_form) fills the shared pipeline on step 1 (identity,
        # email, eligibility, résumé upload to the oj-file-picker's hidden <input type=file>).
        # We then fill the ORC-specific gaps the generic analyzer can't (JET selects/radiosets
        # screeners, EEO decline, required consent), then walk the wizard.
        report = await super().prefill(
            page, profile_form, resume_path, cover_letter=cover_letter, job=job,
            draft=draft, resume_summary=resume_summary, known_answers=known_answers,
            facts=facts, profile_id=profile_id, niche=niche,
            resume_parser_only=resume_parser_only)
        if report.get("page_type") in ("login_required", "captcha", "expired"):
            return report
        try:
            await self._fill_orc_gaps(page, profile_form, facts)
        except Exception as exc:
            logger.debug("oracle_orc: gap fill raised: %s", exc)
        try:
            report["unfilled"] = await self._rescan_required(page)
        except Exception as exc:
            logger.debug("oracle_orc: rescan raised: %s", exc)
        if self.advance_wizard:
            try:
                await self._advance_wizard(page, report, profile_form, cover_letter, facts)
            except Exception as exc:
                logger.debug("oracle_orc: wizard advance raised: %s", exc)
        return report

    # ---- ORC-specific gap fill (label/role driven so it generalizes across CX tenants) ----
    async def _fill_orc_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        await self._dismiss_cookie_banner(page)
        # EEO / diversity self-ID + required legal consent — Oracle renders these as JET
        # radiosets / checkboxsets / selects; the shared dropdowns helpers decline every
        # demographic (never claiming a protected characteristic) and tick required consent.
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass
        # Country-dependent State/Province is a JET select whose options load after Country is
        # set; the shared pipeline usually set Country already, so pick the persona's state now.
        state = (profile_form.get("state") or "").strip()
        if state:
            try:
                await self._fill_oj_select(page, "state", [state])
            except Exception:
                pass
        # Pre-screening Yes/No + experience/education/language questions the analyzer misses
        # (classic JET selects / radiosets), answered deterministically & TRUTHFULLY.
        await self._answer_screeners(page, facts)
        # Redwood JET (oj-c-*) tenants (e.g. Alorica) render selects as input[role=combobox] and
        # every Yes/No / Title / screener as button[role=radio] — invisible to the classic
        # oj-select-single / input[type=radio] fillers above. Handle that DOM shape too (additive;
        # no-ops on a classic-JET tenant that has no role=combobox / button[role=radio]).
        try:
            await self._fill_orc_redwood(page, profile_form, facts)
        except Exception as exc:
            logger.debug("oracle_orc: redwood fill raised: %s", exc)

    # ---- Redwood JET (oj-c-*) fill: input[role=combobox] + button[role=radio] + committed text ----
    _NEAR_JS = (
        "el=>{const byId=el.getAttribute('aria-labelledby');"
        "if(byId){const t=byId.split(/\\s+/).map(i=>{const n=document.getElementById(i);"
        "return n?n.innerText:'';}).join(' ').trim();if(t)return t;}"
        "if(el.getAttribute('aria-label'))return el.getAttribute('aria-label');"
        "let p=el,h=0;while(p&&h<6){p=p.parentElement;h++;if(!p)break;"
        "const c=p.cloneNode(true);c.querySelectorAll('input,button,select,[role=combobox],"
        "[role=radio],[role=listbox],svg').forEach(x=>x.remove());"
        "const t=(c.innerText||'').replace(/\\s+/g,' ').trim();"
        "if(t.length>=3&&t.length<130)return t;}return el.getAttribute('placeholder')||'';}")

    async def _fill_orc_redwood(self, page: Page, profile_form: dict, facts) -> None:
        facts = facts or {}
        await self._commit_orc_text(page, profile_form)
        await self._fill_orc_comboboxes(page, profile_form, facts)
        await self._fill_orc_radiobuttons(page, profile_form, facts)

    async def _commit_orc_text(self, page: Page, profile_form: dict) -> None:
        """Redwood oj-c text inputs don't accept a plain Playwright .fill() into their bound model
        (value shows but never commits -> 'is required'). Re-set via the NATIVE value setter +
        input/change/blur so the JET/React binding registers it."""
        data = {
            "first": profile_form.get("first_name") or "",
            "last": profile_form.get("last_name") or "",
            "email": profile_form.get("email") or "",
            "phone": profile_form.get("phone") or "",
            "addr": profile_form.get("street_address") or profile_form.get("address") or "",
        }
        try:
            await page.evaluate(
                "(d)=>{const near=" + self._NEAR_JS + ";"
                "const set=(el,v)=>{try{const p=Object.getOwnPropertyDescriptor("
                "window.HTMLInputElement.prototype,'value');p.set.call(el,v);}catch(e){el.value=v;}"
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "el.dispatchEvent(new Event('change',{bubbles:true}));"
                "el.dispatchEvent(new Event('blur',{bubbles:true}));};"
                "const map=[['first name',d.first],['last name',d.last],['email address',d.email],"
                "['phone number',d.phone],['address line 1',d.addr],['full name',d.first+' '+d.last]];"
                "for(const ip of document.querySelectorAll('input')){"
                "const ty=(ip.getAttribute('type')||'text').toLowerCase();"
                "if(['hidden','file','checkbox','radio','submit','button'].includes(ty))continue;"
                "const r=ip.getBoundingClientRect();if(r.width===0&&r.height===0)continue;"
                "const lab=(near(ip)+' '+(ip.id||'')+' '+(ip.name||'')+' '+"
                "(ip.getAttribute('autocomplete')||'')).toLowerCase();"
                "const alt={'last name':['lastname','family','surname'],'first name':['firstname','given'],"
                "'full name':['fullname','signature','legalname'],'phone number':['phone','tel']};"
                "for(const [k,v] of map){if(!v)continue;let hit=lab.includes(k);"
                "if(!hit&&alt[k])hit=alt[k].some(a=>lab.includes(a));"
                "if(hit){set(ip,v);break;}}}}",
                data)
        except Exception as exc:
            logger.debug("oracle_orc: commit text raised: %s", exc)

    async def _map_comboboxes(self, page: Page) -> list:
        try:
            return await page.evaluate(
                "()=>{const near=" + self._NEAR_JS + ";const out=[];let i=0;"
                "for(const cb of document.querySelectorAll('input[role=combobox],[role=combobox]')){"
                "const r=cb.getBoundingClientRect();if(r.width===0&&r.height===0)continue;"
                "cb.setAttribute('data-jfcb',i);"
                "out.push({i:i,label:near(cb).toLowerCase(),"
                "val:(cb.value||cb.innerText||'').trim()});i++;}return out;}")
        except Exception:
            return []

    async def _fill_orc_combobox_by(self, page: Page, boxes: list, want: str, val: str,
                                    match, first_ok: bool = False) -> bool:
        for b in boxes:
            lab = b.get("label") or ""
            if not match(lab):
                continue
            if (b.get("val") or "").strip() and not first_ok:
                return True
            return await self._pick_combobox(page, f"[data-jfcb='{b['i']}']", val, first_ok=first_ok)
        return False

    async def _fill_orc_comboboxes(self, page: Page, profile_form: dict, facts) -> None:
        """Redwood address selects (Country / State / City / Postal Code / County) are
        input[role=combobox] typeaheads. Country MUST be set FIRST — City/State/Postal/County only
        render after it (cascading), so we fill Country, wait, then re-query the DOM. Also declines
        the Veteran Self-ID and Gender comboboxes (never claiming a protected characteristic)."""
        boxes = await self._map_comboboxes(page)
        # 1) Country first (label 'country' but not the phone 'country code').
        try:
            await self._fill_orc_combobox_by(
                page, boxes, "country", "United States",
                lambda l: "country" in l and "code" not in l)
        except Exception:
            pass
        # 2) poll for the address sub-fields to render (cascade off Country), then fill them.
        boxes = []
        for _ in range(8):
            await page.wait_for_timeout(700)
            boxes = await self._map_comboboxes(page)
            labs = " ".join((b.get("label") or "") for b in boxes)
            if "city" in labs or "state" in labs or "postal" in labs:
                break
        addr = [
            ("state", profile_form.get("state") or "", lambda l: "state" in l or "province" in l, False),
            ("city", profile_form.get("city") or "", lambda l: "city" in l, False),
            ("postal", profile_form.get("zip") or profile_form.get("postal_code") or "",
             lambda l: "postal" in l or "zip" in l, False),
            ("county", "", lambda l: "county" in l, True),
        ]
        for _key, val, match, first_ok in addr:
            try:
                await self._fill_orc_combobox_by(page, boxes, _key, val, match, first_ok=first_ok)
            except Exception:
                pass
        # 3) EEO comboboxes: decline (Veteran Self-ID / Gender) — open + pick the non-disclosure
        # option (never claiming a protected characteristic; never typed as free text).
        boxes = await self._map_comboboxes(page)
        for b in boxes:
            lab = (b.get("label") or "")
            if (b.get("val") or "").strip():
                continue
            if "veteran" in lab or lab.strip().startswith("gender") or "self-identif" in lab \
                    or "disability" in lab:
                try:
                    await self._decline_combobox(page, f"[data-jfcb='{b['i']}']")
                except Exception:
                    pass

    async def _decline_combobox(self, page: Page, sel: str) -> bool:
        """Open a JET EEO combobox and click its non-disclosure option (decline / prefer-not /
        'I do not want to answer' / 'not a protected veteran'). Never types a protected characteristic."""
        dec_re = re.compile(
            r"do not (want|wish)|don't want|decline|prefer not|not to answer|choose not|"
            r"not a protected veteran|i am not a|not applicable", re.I)
        try:
            el = page.locator(sel).first
            if not await el.count():
                return False
            await el.scroll_into_view_if_needed(timeout=2000)
            await el.click(timeout=2500)
            await page.wait_for_timeout(600)
            opts = page.locator("[role=option], .oj-listbox-result, li[role=option], "
                                ".oj-collection-item")
            n = await opts.count()
            for i in range(min(n, 40)):
                o = opts.nth(i)
                try:
                    t = (await o.inner_text()) or ""
                except Exception:
                    continue
                if dec_re.search(t):
                    await o.click(timeout=2000)
                    await page.wait_for_timeout(250)
                    return True
            await page.keyboard.press("Escape")
        except Exception:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
        return False

    async def _pick_combobox(self, page: Page, sel: str, val: str, first_ok: bool = False) -> bool:
        try:
            el = page.locator(sel).first
            if not await el.count():
                return False
            await el.scroll_into_view_if_needed(timeout=2000)
            await el.click(timeout=2500)
            await page.wait_for_timeout(400)
            if val:
                try:
                    await el.fill(val, timeout=2000)
                except Exception:
                    await page.keyboard.type(val, delay=45)
                await page.wait_for_timeout(900)
            opts = page.locator("[role=option], .oj-listbox-result, li[role=option], "
                                ".oj-collection-item")
            target = None
            if val:
                target = opts.filter(has_text=re.compile(re.escape(val.split()[0]), re.I)).first
            if (target is None or not await target.count()) and first_ok:
                target = opts.filter(
                    has_not_text=re.compile("no matches|no results|searching|select", re.I)).first
            if target is not None and await target.count():
                await target.click(timeout=2500)
                await page.wait_for_timeout(300)
                return True
            # no listbox match — commit the typed text (some Redwood address fields are free-text)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(200)
            await page.keyboard.press("Tab")
            return bool(val)
        except Exception:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def _fill_orc_radiobuttons(self, page: Page, profile_form: dict, facts) -> None:
        """Redwood renders Title (Mr./Mrs./Ms.) and every Yes/No screener as button[role=radio]
        groups (no native input[type=radio]). Answer each UNANSWERED group truthfully: Title by the
        persona's sex, screeners via _screener_answer."""
        try:
            groups = await page.evaluate(
                "()=>{const btns=[...document.querySelectorAll('button[role=radio],[role=radio]')]"
                ".filter(b=>{const r=b.getBoundingClientRect();return r.width>0&&r.height>0;});"
                "const boxOf=b=>{let g=b.closest('[role=radiogroup]');if(g)return g;"
                "let p=b.parentElement,h=0,box=b;while(p&&h<6){"
                "if([...p.querySelectorAll('[role=radio]')].length>=2){box=p;break;}"
                "p=p.parentElement;h++;}return box;};"
                "const seen=new Map();let gid=0;const res=[];"
                "for(const b of btns){const box=boxOf(b);if(seen.has(box))continue;seen.set(box,gid);"
                "const rc=[...box.querySelectorAll('[role=radio]')];"
                "const opts=rc.map((r,i)=>{r.setAttribute('data-jfrb',gid+'_'+i);"
                "return {text:(r.innerText||'').replace(/\\s+/g,' ').trim().slice(0,60),"
                "sel:'[data-jfrb=\"'+gid+'_'+i+'\"]',checked:r.getAttribute('aria-checked')==='true'};});"
                # question = climb until the container text (minus options) is a real prompt
                "let q='',cur=box,hop=0;while(cur&&hop<5){const c=cur.cloneNode(true);"
                "c.querySelectorAll('[role=radio],button').forEach(x=>x.remove());"
                "const t=(c.innerText||'').replace(/\\s+/g,' ').trim();"
                "if(t.length>12){q=t;break;}cur=cur.parentElement;hop++;}"
                "res.push({gid:gid,q:q.slice(0,220),opts:opts,"
                "answered:opts.some(o=>o.checked)});gid++;}return res;}")
        except Exception:
            groups = []
        sex = (profile_form.get("sex") or "").strip().lower()
        for grp in groups:
            if grp.get("answered"):
                continue
            opts = grp.get("opts") or []
            texts = [(o.get("text") or "") for o in opts]
            joined = " ".join(texts).lower()
            picked = None
            # Title / salutation group
            if any(re.match(r"^(mr|mrs|ms|mx)\.?$", (t or "").strip(), re.I) for t in texts):
                want = "mr." if sex in ("male", "m", "man") else "ms."
                for o in opts:
                    if (o.get("text") or "").strip().lower().startswith(want[:2]):
                        # prefer exact Mr./Ms.; Ms. beats Mrs. (no marital assumption)
                        if want == "ms." and (o.get("text") or "").strip().lower().startswith("mrs"):
                            continue
                        picked = o
                        break
                if not picked:
                    picked = opts[0] if opts else None
            else:
                ql = (grp.get("q") or "").lower()
                st = (profile_form.get("state") or "").strip().lower()
                # TRUTHFULNESS guard: a residency screener naming a SPECIFIC state that isn't the
                # persona's is answered No (never a fabricated "yes, I reside in <other state>").
                m = re.search(r"resident of ([a-z][a-z .]+?)(?:\s*\(|,|\?|\.|$)", ql)
                if m and st:
                    named = m.group(1).strip()
                    if named and named not in st and st not in named:
                        cands = ["No"]
                    else:
                        cands = ["Yes"]
                else:
                    cands = self._screener_answer(ql, facts)
                if not cands:
                    continue
                for c in cands:
                    cl = c.strip().lower()
                    for o in opts:
                        if self._opt_match(cl, (o.get("text") or "").strip().lower()):
                            picked = o
                            break
                    if picked:
                        break
            if not picked:
                continue
            sel = picked["sel"]
            try:
                b = page.locator(sel).first
                if await b.count():
                    await b.scroll_into_view_if_needed(timeout=1500)
                    await b.click(timeout=2500)
                    await page.wait_for_timeout(250)
                    # Redwood button[role=radio] sometimes ignores the synthetic Playwright click —
                    # verify aria-checked flipped, else dispatch a full pointer sequence.
                    ok = await page.evaluate(
                        "(s)=>{const e=document.querySelector(s);"
                        "return !!e&&e.getAttribute('aria-checked')==='true';}", sel)
                    if not ok:
                        await page.evaluate(
                            "(s)=>{const e=document.querySelector(s);if(!e)return;"
                            "e.scrollIntoView({block:'center'});"
                            "['pointerover','pointerenter','pointerdown','mousedown','pointerup',"
                            "'mouseup','click'].forEach(t=>e.dispatchEvent("
                            "new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));}", sel)
                        await page.wait_for_timeout(250)
            except Exception:
                pass

    async def _answer_screeners(self, page: Page, facts) -> None:
        """Answer every UNANSWERED pre-screening question truthfully for a synthetic US persona
        located at the job's city: JET selects via _answer_select_screeners, JET radiosets via
        _answer_radio_screeners. Leaves an unmatched question for the human rather than guessing."""
        facts = facts or {}
        await self._tick_acknowledge(page)
        try:
            await self._answer_select_screeners(page, facts)
        except Exception as exc:
            logger.debug("oracle_orc: select screeners raised: %s", exc)
        try:
            await self._answer_radio_screeners(page, facts)
        except Exception as exc:
            logger.debug("oracle_orc: radio screeners raised: %s", exc)

    async def _answer_select_screeners(self, page: Page, facts) -> None:
        """Walk labeled, still-unanswered oj-select-single widgets; for each whose label maps to
        a deterministic answer, type+pick the matching option (JET type-ahead)."""
        try:
            labels = await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  for(const w of document.querySelectorAll('oj-select-single')){
                    const l=w.querySelector('label')||
                      (w.getAttribute('aria-label')?{innerText:w.getAttribute('aria-label')}:null)||
                      (w.previousElementSibling&&w.previousElementSibling.tagName==='LABEL'
                        ?w.previousElementSibling:null);
                    const t=((l&&l.innerText)||w.getAttribute('label-hint')||'').trim();
                    if(t.length<4) continue;
                    // already answered? JET renders the selection text inside the widget.
                    const sel=(w.innerText||'').replace(t,'').trim();
                    const answered=!!sel && !/select a value|select\\.\\.\\.|choose/i.test(sel);
                    const key=t.slice(0,110);
                    if(seen.has(key)) continue; seen.add(key);
                    out.push({label:t, key, answered});
                  } return out;}""")
        except Exception:
            return
        for f in labels:
            if f.get("answered"):
                continue
            label = (f.get("label") or "").lower()
            key = f.get("key") or ""
            is_prof = bool(re.search(r"proficiency|language", label)
                           and re.search(r"english|spanish", label))
            values = self._screener_answer(label, facts)
            if is_prof and not values:
                # language-proficiency scale: HIGH for English, and for Spanish only when the
                # persona is bilingual; a low tier for Spanish otherwise.
                high = True if "english" in label else bool(facts.get("bilingual"))
                values = (["Native", "Fluent", "Advanced", "Professional"] if high
                          else ["None", "No proficiency", "Basic", "Limited"])
            if not values:
                continue
            try:
                await self._fill_oj_select(page, key, values, allow_first=is_prof)
            except Exception:
                pass

    async def _answer_radio_screeners(self, page: Page, facts) -> None:
        """Answer every UNANSWERED oj-radioset (or bare radio group) with a truthful, backed
        pick from _screener_answer. Leaves an unmatched group for the human."""
        facts = facts or {}
        try:
            groups = await page.evaluate(
                """()=>{const byName={};
                  for(const r of document.querySelectorAll('input[type=radio]')){
                    const nm=r.name||''; if(!nm) continue; (byName[nm]=byName[nm]||[]).push(r);}
                  const lab=r=>{const l=r.id?document.querySelector('label[for="'+
                        (window.CSS&&CSS.escape?CSS.escape(r.id):r.id)+'"]'):null;
                    return ((l&&l.innerText)||(r.closest('label')?r.closest('label').innerText:'')||'').trim();};
                  const out=[];
                  for(const nm in byName){const rs=byName[nm];
                    const opts=rs.map(r=>({value:r.value,text:lab(r).replace(/\\s+/g,' '),checked:r.checked}));
                    // smallest ancestor holding every radio, then climb to include the prompt.
                    let box=rs[0].parentElement;
                    while(box&&!rs.every(r=>box.contains(r))) box=box.parentElement;
                    const optLen=opts.map(o=>o.text).join(' ').replace(/\\s+/g,'').length;
                    let g=0;
                    while(box&&box.parentElement&&g<4){
                      if((box.innerText||'').replace(/\\s+/g,'').length>optLen+10) break;
                      box=box.parentElement; g++;}
                    let qt=box?(box.innerText||''):'';
                    for(const o of opts) if(o.text) qt=qt.split(o.text).join(' ');
                    qt=qt.replace(/\\s+/g,' ').trim();
                    out.push({name:nm,label:qt,answered:rs.some(r=>r.checked),
                      options:opts.map(o=>({value:o.value,text:o.text}))});}
                  return out;}""")
        except Exception:
            return
        for grp in groups:
            if grp.get("answered"):
                continue
            cands = self._screener_answer((grp.get("label") or "").lower(), facts)
            if not cands:
                continue
            opts = grp.get("options") or []
            picked = None
            for c in cands:
                cl = c.strip().lower()
                for o in opts:
                    if self._opt_match(cl, (o.get("text") or "").strip().lower()):
                        picked = o
                        break
                if picked:
                    break
            if not picked:
                continue
            try:
                await self._click_radio(page, grp["name"], picked.get("value"))
            except Exception:
                pass

    async def _fill_oj_select(self, page: Page, label_substr: str, values,
                              allow_first: bool = False) -> bool:
        """Fill an Oracle JET oj-select-single whose label contains label_substr: click it to
        open the dropdown, type each value into the filter, and click the matching (or first)
        result. JET selects need this type-ahead — a plain .fill types prose the widget rejects,
        and setting the native <input> would jump to the wrong option."""
        found = await page.evaluate(
            """(lbl)=>{const n=s=>(s||'').toLowerCase();
              for(const w of document.querySelectorAll('oj-select-single')){
                const l=w.querySelector('label');
                const t=((l&&l.innerText)||w.getAttribute('aria-label')||w.getAttribute('label-hint')||'');
                if(!n(t).includes(lbl)) continue;
                w.setAttribute('data-jfojs','1'); return true;} return false;}""",
            label_substr.lower())
        if not found:
            return False
        picked = False
        for val in values:
            try:
                await page.click("oj-select-single[data-jfojs='1']", timeout=3000)
                await page.wait_for_timeout(400)
                # The open dropdown's filter/search input (JET renders it in a popup).
                sf = page.locator(
                    ".oj-listbox-drop input, .oj-listbox-filter input, "
                    "input[role='combobox'], oj-select-single[data-jfojs='1'] input").last
                try:
                    await sf.fill(val, timeout=2500)
                except Exception:
                    await sf.type(val, delay=40)
                await page.wait_for_timeout(900)   # option filter/AJAX
                opts = page.locator(
                    ".oj-listbox-result, .oj-listbox-results li, [role='option']")
                target = opts.filter(
                    has_text=re.compile(re.escape(val.split()[0]), re.I)).first
                if not await target.count() and allow_first:
                    target = opts.filter(
                        has_not_text=re.compile("no matches|no results|searching", re.I)).first
                if await target.count():
                    await target.click(timeout=3000)
                    picked = True
                    await page.wait_for_timeout(250)
                    break   # one value applied per select
                else:
                    await page.keyboard.press("Escape")
            except Exception:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
        try:
            await page.eval_on_selector("oj-select-single[data-jfojs='1']",
                                        "e=>e.removeAttribute('data-jfojs')")
        except Exception:
            pass
        return picked

    async def _click_radio(self, page: Page, name: str, value) -> bool:
        found = await page.evaluate(
            """([nm,val])=>{for(const r of document.querySelectorAll('input[type=radio]')){
                if(r.name===nm && r.value===val){r.setAttribute('data-jfr','1');return true;}}
              return false;}""", [name, value])
        if not found:
            return False
        ok = True
        try:
            await page.check("input[data-jfr='1']", timeout=3000, force=True)
        except Exception:
            try:
                await page.eval_on_selector(
                    "input[data-jfr='1']",
                    "e=>{e.checked=true;e.dispatchEvent(new Event('click',{bubbles:true}));"
                    "e.dispatchEvent(new Event('change',{bubbles:true}));}")
            except Exception:
                ok = False
        try:
            await page.eval_on_selector("input[data-jfr='1']", "e=>e.removeAttribute('data-jfr')")
        except Exception:
            pass
        return ok

    async def _tick_acknowledge(self, page: Page) -> None:
        """Tick a required certification/acknowledgement checkbox or radio (a single affirmative
        option like 'I certify' / 'I acknowledge')."""
        try:
            ids = await page.evaluate(
                """()=>{const out=[];
                  for(const el of document.querySelectorAll('input[type=checkbox],input[type=radio]')){
                    if(el.checked||!el.id)continue;
                    const l=document.querySelector('label[for="'+
                      (window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]');
                    const t=((l&&l.innerText)||(el.closest('label')||{}).innerText||'').toLowerCase();
                    if(/acknowledge|i certify|i attest|i agree|i understand|i confirm/.test(t))
                      out.push(el.id);}
                  return out;}""")
        except Exception:
            return
        for eid in ids:
            try:
                await page.locator(f'[id="{eid}"]').check(force=True, timeout=2500)
            except Exception:
                try:
                    await page.evaluate(
                        """(id)=>{const e=document.getElementById(id);if(e){e.checked=true;"""
                        """e.dispatchEvent(new Event('click',{bubbles:true}));"""
                        """e.dispatchEvent(new Event('change',{bubbles:true}));}}""", eid)
                except Exception:
                    pass

    @staticmethod
    def _opt_match(cand: str, opt: str) -> bool:
        """Match a candidate answer to an option text. Short answers (yes/no/ged) need a word
        boundary so 'No' never matches 'None'; longer answers ('1-3 years') use substring."""
        if not cand or not opt:
            return False
        if cand == opt:
            return True
        if len(cand) <= 4:
            return (opt.startswith(cand + " ") or opt.startswith(cand + ",")
                    or (" " + cand + " ") in (" " + opt + " "))
        return cand in opt or opt in cand

    @staticmethod
    def _screener_answer(t: str, facts: dict):
        """Deterministic, truthful answer candidates for an ORC pre-screening question
        (lowercased label). Returns an ordered list of option-text candidates (strongest first),
        or None to leave it for the human. Truthful for a synthetic US persona DESIGNED to fit
        the job (located at the job's city, native English, bilingual only when the role is)."""
        facts = facts or {}
        if re.search(r"acknowledge|i certify|i attest", t):
            return None                                   # handled by _tick_acknowledge
        if re.search(r"spanish", t):
            return (["Fluent", "Native", "Advanced", "Bilingual"] if facts.get("bilingual")
                    else ["None", "No proficiency", "Basic", "Beginner", "Limited"])
        if re.search(r"english", t):
            # A US persona is a native English speaker — lead the strongest tier.
            return ["Native", "Native or bilingual", "Fluent", "Advanced", "Professional"]
        if re.search(r"highest level of education|education (you have )?achieved|level of education", t):
            return [facts.get("education_level") or "Bachelor", "Bachelor", "High School",
                    "Associate", "GED"]
        # Customer-service / call-center experience — pick the HIGHEST believable tier (the
        # tailored résumé shows ~8 yrs), never a weak middle one that undersells + contradicts it.
        if re.search(r"experience.*(customer service|call center|contact center|retail|customer)", t):
            return ["5+ years", "5 or more", "More than 5", "6+ years", "5 years", "3-5 years",
                    "3+ years", "1-3 years", "Yes"]
        if re.search(r"(supervisor|leadership|management|managerial|team lead)\s*(or [a-z]+ )?experience|"
                     r"experience.*(supervisor|leadership|manage|team lead)|"
                     r"how (much|many years?).*experience|years of experience", t):
            return ["4-5 years", "5+ years", "6+ years", "3-5 years", "5 years", "More than",
                    "1-3 years", "Yes"]
        if re.search(r"reside|within \d+ ?mile|live within|currently reside|relocat", t):
            return ["Yes"]
        # A schedule-conflict/attendance screener → No. Scoped to the attendance/schedule/
        # availability context so a behavioral "describe a time you resolved a conflict" prompt
        # (an open-text field) is NOT mistaken for a Yes/No screener and left for the human.
        if re.search(r"(?:commitment|obligation|conflict).{0,40}"
                     r"(?:interfere|attendance|schedule|availab|work)"
                     r"|foresee (?:any )?(?:commitment|conflict|obligation)"
                     r"|interfere with (?:your )?(?:attendance|schedule|work|availab)"
                     r"|impact.*attendance", t):
            return ["No"]
        if re.search(r"private|secure|quiet|workspace|distraction|free from", t):
            return ["Yes"]
        if re.search(r"ethernet|hardwired|hard-wired|wired", t):
            return ["Yes, my home internet is hardwired", "Yes"]
        if re.search(r"download speed|\bmbps\b|high.?speed|cable or fiber|internet|connection", t):
            return ["Yes"]
        if re.search(r"documentation|diploma or ged|provide.*if needed|verify.*education|"
                     r"able to provide", t):
            return ["Yes"]
        if re.search(r"18 (years|and older)|older|authorized|eligible to work", t):
            return ["Yes"]
        if re.search(r"seasonal|interested in (the |this )?(season|temporary|position|role|opportunity)", t):
            return ["Yes"]
        if re.search(r"\bcitizen(ship)?\b|u\.?s\.? citizen", t):
            return ["Yes"]
        if re.search(r"require sponsor|need sponsor|visa sponsor", t):
            return ["No"]
        if re.search(r"able to meet this requirement|do you meet this requirement|"
                     r"meet (this|the) requirement|able to work|\bshift\b|overtime|"
                     r"willing to (work|attend|commit|travel|obtain)|onsite|on-site|"
                     r"in.?office|in person|first week|training|"
                     r"obtain a[n]? .*(clearance|public trust)|public trust|"
                     r"background (check|investigation)", t):
            return ["Yes"]
        return None

    async def _rescan_required(self, page: Page) -> list:
        """Labels of required-but-empty visible fields on the current step, so the report's
        `unfilled` reflects the ORC gap fill and the co-pilot's submit gate is honest.
        (JET renders a real <input>/<select> under each oj-* element, so a standard DOM scan
        still sees the underlying required state.)"""
        try:
            return await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  for(const el of document.querySelectorAll('input,select,textarea')){
                    const t=(el.type||'').toLowerCase();
                    if(['hidden','submit','button','file','reset'].includes(t)) continue;
                    const r=el.getBoundingClientRect();
                    if(r.width===0&&r.height===0) continue;   // skip JET's hidden shadow inputs
                    const req=el.required||el.getAttribute('aria-required')==='true'
                      ||!!el.closest('[aria-required="true"],.oj-complete.oj-required');
                    if(!req) continue;
                    let empty;
                    if(t==='checkbox'||t==='radio'){const nm=el.name;
                      empty=nm?![...document.querySelectorAll('[name="'+
                        (window.CSS&&CSS.escape?CSS.escape(nm):nm)+'"]')].some(x=>x.checked):!el.checked;}
                    else empty=!(el.value||'').trim();
                    if(!empty) continue;
                    let lab='';const id=el.id;
                    if(id){const l=document.querySelector('label[for="'+
                      (window.CSS&&CSS.escape?CSS.escape(id):id)+'"]');if(l)lab=l.innerText.trim();}
                    if(!lab){const l=el.closest('label')||
                      (el.parentElement&&el.parentElement.querySelector('label'));if(l)lab=l.innerText.trim();}
                    lab=(lab||'').replace(/\\s*\\*\\s*$/,'').trim().slice(0,80)||(el.name||'field');
                    if(!seen.has(lab)){seen.add(lab);out.push(lab);}
                  } return out;}""")
        except Exception:
            return []

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        """Close a cookie/consent banner (OneTrust/Oracle) that floats over the action bar and
        can intercept the Apply / Continue / Submit clicks. Also dismisses Oracle CX's
        'Are You Still With Us?' session-idle modal, which pops repeatedly during a slow fill and
        otherwise resets the cascade / blocks Submit."""
        await self._dismiss_idle_modal(page)
        for name in ("Reject Optional Cookies", "Reject All", "Accept All Cookies",
                     "Accept Cookies", "Accept All", "I Agree"):
            try:
                b = page.get_by_role("button", name=re.compile(re.escape(name), re.I))
                if await b.count():
                    await b.first.click(timeout=1500)
                    await page.wait_for_timeout(250)
                    return
            except Exception:
                continue

    async def _dismiss_idle_modal(self, page: Page) -> None:
        """Click the keep-alive button of Oracle CX's 'Are You Still With Us?' idle dialog."""
        try:
            present = await page.evaluate(
                "()=>/still with us|still there|are you there|session.{0,20}(expir|time out|timeout)/i"
                ".test(document.body?document.body.innerText:'')")
        except Exception:
            return
        if not present:
            return
        for sel in ("button:has-text('Yes')", "button:has-text('Continue')",
                    "button:has-text(\"I'm still here\")", "button:has-text('Stay')",
                    "button:has-text('Keep')", "oj-button:has-text('Yes') button",
                    "button:has-text('OK')"):
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible(timeout=600):
                    await b.click(timeout=1200)
                    await page.wait_for_timeout(500)
                    return
            except Exception:
                continue

    # ---- wizard walker (mirrors AvatureStrategy._advance_wizard) ----
    async def _step_signature(self, page: Page) -> str:
        """A cheap fingerprint of the current wizard step, to tell whether a Continue click
        actually advanced (Oracle CX re-renders the section in place, often same URL)."""
        try:
            return await page.evaluate(
                "()=>{const a=document.querySelector('[aria-current=\"step\"],[aria-current=\"true\"],"
                ".oj-optlayout-current,.progress-current');"
                "const h=document.querySelector('h1,h2,legend,.oj-flex .oj-label, .section-title');"
                "return (a?a.innerText.trim().slice(0,40):'')+'|'+(h?h.innerText.trim().slice(0,40):'');}")
        except Exception:
            return ""

    async def _primary_button(self, page: Page):
        """Return (handle, kind) for the step's primary button: kind='submit' on the final
        (Review) step, 'advance' on Continue/Next, else None."""
        try:
            for b in await page.query_selector_all(_WIZARD_BTN):
                if not await b.is_visible():
                    continue
                txt = ((await b.inner_text()) or "").strip()
                if _SUBMIT_RE.search(txt) and not _ADVANCE_RE.search(txt):
                    return b, "submit"
                if _ADVANCE_RE.search(txt):
                    return b, "advance"
            sel = await find_submit_button(page)
            if sel:
                b = await page.query_selector(sel)
                if b:
                    txt = ((await b.inner_text()) or "").strip()
                    return b, ("submit" if _SUBMIT_RE.search(txt) else "advance")
        except Exception as exc:
            logger.debug("oracle_orc: primary_button raised: %s", exc)
        return None, None

    async def _fill_current_step(self, page, profile_form, cover_letter, facts) -> None:
        """Fill an EEO / voluntary / review step: decline demographics, tick required consent,
        fill any ordinary matched fields, and answer the step's JET screeners."""
        await self._dismiss_cookie_banner(page)
        for fn in (fill_demographics_decline, fill_demographic_checkboxes_decline,
                   fill_required_consent):
            try:
                await fn(page)
            except Exception:
                pass
        try:
            analysis = await analyze_page(page, profile_form, cover_letter, {}, facts or {})
            await fill_form(page, analysis)
        except Exception as exc:
            logger.debug("oracle_orc: step fill raised: %s", exc)
        try:
            await self._answer_screeners(page, facts)
        except Exception as exc:
            logger.debug("oracle_orc: step screeners raised: %s", exc)

    async def _advance_wizard(self, page, report, profile_form, cover_letter, facts) -> None:
        """Walk the multi-step wizard: click Continue while it advances (filling each new step),
        and STOP at the final Submit — recording its selector in the report WITHOUT clicking it.
        If a Continue click does NOT advance (validation blocked it because a required field is
        still empty), stop and leave the gaps in `unfilled` for the human / next iteration."""
        for _ in range(6):
            await self._dismiss_cookie_banner(page)
            btn, kind = await self._primary_button(page)
            if btn is None:
                break
            if kind == "submit":
                # The final (Review) step is reached — fill anything still on it, then record
                # the true final-submit button (never a Continue). We do NOT click it.
                await self._fill_current_step(page, profile_form, cover_letter, facts)
                report["submit_selector"] = (
                    "oj-button:has-text('Submit') button, button:has-text('Submit'), "
                    "button[title*='Submit' i], oj-button[id*='submit' i] button")
                report["wizard_at_submit"] = True
                report["unfilled"] = await self._rescan_required(page)
                return
            sig = await self._step_signature(page)
            try:
                await btn.click()
                await page.wait_for_timeout(2000)
            except Exception:
                break
            if await self._step_signature(page) == sig:
                # Did not advance -> a required field on this step is still empty. Stop; the
                # human / next iteration finishes it (the dry-run screenshot shows what's left).
                report["wizard_blocked_step"] = sig
                report["unfilled"] = await self._rescan_required(page)
                return
            await self._fill_current_step(page, profile_form, cover_letter, facts)
