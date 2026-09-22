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
        # Validation failures (a filled-but-rejected field, e.g. the reserved-fiction 555-01xx phone
        # Oracle's libphonenumber flags 'Enter a valid number') are NOT empty, so _rescan_required
        # misses them — surface them so the co-pilot's submit gate refuses honestly.
        try:
            inv = await self._invalid_fields(page)
            if inv:
                report["invalid_fields"] = inv
                cur = list(report.get("unfilled") or [])
                for lbl in inv:
                    tag = f"{lbl} (invalid)"
                    if tag not in cur:
                        cur.append(tag)
                report["unfilled"] = cur
        except Exception as exc:
            logger.debug("oracle_orc: invalid-scan raised: %s", exc)
        return report

    # ---- ORC-specific gap fill (label/role driven so it generalizes across CX tenants) ----
    async def _fill_orc_gaps(self, page: Page, profile_form: dict, facts=None) -> None:
        await self._dismiss_cookie_banner(page)
        # Guest-auth 'I agree with the terms and conditions' — must be ticked for the auth step's
        # Next to reveal the full application form (harmless on later steps: no-op when absent).
        await self._tick_terms(page)
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
                                    match, first_ok: bool = False, prefer: str = "",
                                    shorten: bool = False) -> bool:
        for b in boxes:
            lab = b.get("label") or ""
            if not match(lab):
                continue
            if (b.get("val") or "").strip() and not first_ok:
                return True
            return await self._pick_combobox(page, f"[data-jfcb='{b['i']}']", val,
                                             first_ok=first_ok, prefer=prefer, shorten=shorten)
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
        st_full = (profile_form.get("state") or "").strip()
        st_code = (profile_form.get("state_code") or "").strip()
        zc = profile_form.get("zip") or profile_form.get("postal_code") or ""
        st_prefer = st_code or st_full
        # CITY FIRST, preferring the persona's STATE — selecting a city on Oracle CX auto-cascades its
        # State + County, and a bare "Columbus" collides ("Columbus City, IA" vs Columbus, OH), so
        # pick the option in the right state. Then State (re-assert only if the cascade left it empty),
        # then Postal (now scoped to the correct state so the ZIP typeahead resolves), then County.
        addr = [
            ("city", profile_form.get("city") or "", lambda l: "city" in l, False, st_prefer, False),
            ("state", st_full, lambda l: "state" in l or "province" in l, False, st_code, False),
            # Postal: first_ok + shorten so a persona ZIP that doesn't fit the auto-cascaded county
            # still resolves to a valid consistent ZIP (retype shorter prefixes until options appear).
            ("postal", zc, lambda l: "postal" in l or "zip" in l, True, "", True),
            ("county", "", lambda l: "county" in l, True, "", False),
        ]
        for _key, val, match, first_ok, prefer, shorten in addr:
            try:
                # re-map before each so a just-cascaded field (State/County auto-set by City) is seen
                # as already-filled and skipped, and the Postal field (late-rendering) is picked up.
                boxes = await self._map_comboboxes(page)
                await self._fill_orc_combobox_by(page, boxes, _key, val, match,
                                                 first_ok=first_ok, prefer=prefer, shorten=shorten)
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

    # Popup option shapes across CX tenants: classic JET renders <li role=option> /
    # .oj-listbox-result; Redwood/CX renders a role=grid popup (aria-haspopup="grid") whose
    # options are role=row / role=gridcell / .cx-select-* list items. Cover all of them.
    _OPT_SEL = ("[role=option], [role=row], [role=gridcell], .oj-listbox-result, "
                "li[role=option], .oj-collection-item, [class*='listbox'] li, "
                "[class*='dropdown'] li, [class*='cx-select'] li, [class*='select'] [role=row]")

    async def _options_locator(self, page: Page, el):
        """Prefer the combobox's OWN popup (its aria-controls listbox) so we never match a stray
        grid row elsewhere; fall back to the page-wide option selectors."""
        try:
            ctrl = await el.get_attribute("aria-controls")
        except Exception:
            ctrl = None
        if ctrl:
            cid = ctrl.split()[0]
            scoped = page.locator(
                f"#{cid} [role=option], #{cid} [role=row], #{cid} li, #{cid} [role=gridcell]")
            try:
                if await scoped.count():
                    return scoped
            except Exception:
                pass
        return page.locator(self._OPT_SEL)

    async def _decline_combobox(self, page: Page, sel: str) -> bool:
        """Open a JET EEO combobox and click its non-disclosure option (decline / prefer-not /
        'I do not want to answer' / 'not a protected veteran'). Never types a protected characteristic."""
        dec_re = re.compile(
            r"do not (want|wish)|don't want|decline|prefer not|not to answer|choose not|"
            r"not a protected veteran|i am not a|not applicable|not identif", re.I)
        try:
            el = page.locator(sel).first
            if not await el.count():
                return False
            await el.scroll_into_view_if_needed(timeout=2000)
            await el.click(timeout=2500)
            await page.wait_for_timeout(600)
            opts = await self._options_locator(page, el)
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

    async def _pick_combobox(self, page: Page, sel: str, val: str, first_ok: bool = False,
                             prefer: str = "", shorten: bool = False) -> bool:
        try:
            el = page.locator(sel).first
            if not await el.count():
                return False
            await el.scroll_into_view_if_needed(timeout=2000)
            await el.click(timeout=2500)
            await page.wait_for_timeout(400)
            if val and shorten:
                # POSTAL/ZIP CX typeahead. Two problems the old .fill()+prefix loop never beat: (1) the
                # widget FILTERS ON REAL KEYSTROKES — an el.fill() sets .value WITHOUT firing the keyup
                # the autocomplete listens to, so NO options ever render (that's why the prefix retry
                # "didn't surface options"); (2) the persona's exact ZIP (43215 = Franklin) doesn't fit
                # the auto-cascaded City+County (Columbus→Delaware) → "No results". A synthetic persona
                # only needs ANY valid local ZIP, so: TYPE the ZIP with REAL keys, then BACKSPACE toward
                # a 1-digit prefix until the listbox offers a real option, and take the FIRST one. The
                # ZIP's own leading digit is the state's region digit (4 = OH), so even a 1-char prefix
                # lists ZIPs for this locale — a valid, consistent postal code for the cascaded city.
                digits = re.sub(r"\D", "", val) or val.strip()

                def _real(loc):
                    return loc.filter(
                        has_not_text=re.compile("no matches|no results|searching|select", re.I))
                try:
                    await el.fill("", timeout=1500)          # clear any pre-seeded / cascaded value
                except Exception:
                    pass
                await page.keyboard.type(digits, delay=60)   # REAL keystrokes fire the autocomplete
                typed = digits
                await page.wait_for_timeout(900)
                for _ in range(len(digits) + 1):
                    try:
                        real = _real(await self._options_locator(page, el)).first
                        if await real.count():
                            await real.click(timeout=2500)
                            await page.wait_for_timeout(300)
                            return True
                    except Exception:
                        pass
                    if len(typed) <= 1:
                        break
                    await page.keyboard.press("Backspace")   # shorten the prefix, keep the session live
                    typed = typed[:-1]
                    await page.wait_for_timeout(750)
                # nothing offered even at a 1-digit prefix — commit whatever was typed as free text
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(200)
                await page.keyboard.press("Tab")
                return bool(typed)
            if val:
                try:
                    await el.fill(val, timeout=2000)
                except Exception:
                    await page.keyboard.type(val, delay=45)
                await page.wait_for_timeout(900)
            opts = await self._options_locator(page, el)
            target = None
            # 1) EXACT value in the preferred state: the option whose text STARTS with the typed city
            #    immediately followed by a separator — so "Columbus" resolves to "Columbus, OH", never
            #    "Columbus Grove, OH" (a different town+county whose ZIP then won't fit → empty Postal).
            if val and prefer:
                exact = opts.filter(
                    has_text=re.compile(r"^\s*" + re.escape(val) + r"\s*[,(\-–/]", re.I)).filter(
                    has_text=re.compile(r"\b" + re.escape(prefer) + r"\b", re.I)).first
                if await exact.count():
                    target = exact
            # 2) with a `prefer` token (the persona's state), pick the option that matches BOTH the
            #    typed value AND the state — so a bare "Columbus" resolves to Columbus, OH not IA.
            if (target is None or not await target.count()) and val and prefer:
                cand = opts.filter(has_text=re.compile(re.escape(val.split()[0]), re.I)).filter(
                    has_text=re.compile(r"\b" + re.escape(prefer) + r"\b", re.I)).first
                if await cand.count():
                    target = cand
            if (target is None or not await target.count()) and prefer:
                cand = opts.filter(has_text=re.compile(r"\b" + re.escape(prefer) + r"\b", re.I)).first
                if await cand.count():
                    target = cand
            if (target is None or not await target.count()) and val:
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
        # "Do you HAVE a High School Diploma, GED or equivalent?" (a Yes/No screener a synthetic
        # persona with an education fact answers Yes). Checked AFTER the education-tier question
        # above (which owns "highest level of education"), so this only catches the Yes/No form.
        if re.search(r"high school diploma|diploma.{0,8}ged|\bged\b|diploma or equivalent|"
                     r"documentation|provide.*if needed|verify.*education|able to provide", t):
            return ["Yes"]
        # Relatives / other members currently employed with the company → No (a fresh synthetic
        # persona has no relatives at the employer — truthful).
        if re.search(r"(relative|family member|immediate family|other member|anyone).{0,50}"
                     r"(employ|work)|member.{0,20}currently employed|know (anyone|someone).{0,30}work", t):
            return ["No"]
        # "Have you ever worked for / provided services for <company>?" → No (fresh persona).
        if re.search(r"ever (worked|work).{0,20}for|previously (employed|worked)|former (employee|"
                     r"associate)|worked for or provided|provided services (for|to)", t):
            return ["No"]
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

    async def _invalid_fields(self, page: Page) -> list:
        """Labels of visible, NON-empty fields the page marks aria-invalid=true or that carry a
        visible field error (e.g. the phone 'Enter a valid number' from Oracle's libphonenumber on
        the reserved-fiction 555-01xx number). These aren't 'empty' so _rescan_required misses them,
        but they still block Submit — surface them so the co-pilot gate is honest."""
        try:
            return await page.evaluate(
                """()=>{const out=[];const seen=new Set();
                  for(const el of document.querySelectorAll('input,textarea,[role=combobox]')){
                    const r=el.getBoundingClientRect(); if(r.width===0&&r.height===0) continue;
                    const val=(el.value||'').trim(); if(!val) continue;   // empty is _rescan_required's job
                    let bad=el.getAttribute('aria-invalid')==='true';
                    const desc=el.getAttribute('aria-describedby');
                    if(!bad&&desc){for(const id of desc.split(/\\s+/)){const n=document.getElementById(id);
                      if(n&&(n.innerText||'').trim()&&/valid|invalid|required|enter a/i.test(n.innerText)){bad=true;break;}}}
                    if(!bad) continue;
                    let lab='';const id=el.id;
                    if(id){const l=document.querySelector('label[for="'+
                      (window.CSS&&CSS.escape?CSS.escape(id):id)+'"]');if(l)lab=l.innerText.trim();}
                    if(!lab)lab=el.getAttribute('aria-label')||el.getAttribute('placeholder')||el.name||'field';
                    lab=(lab||'').replace(/\\s*\\*\\s*$/,'').replace(/\\s+/g,' ').trim().slice(0,60);
                    if(!seen.has(lab)){seen.add(lab);out.push(lab);}
                  } return out;}""")
        except Exception:
            return []

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
        # Oracle CX's own cookie-consent MODAL (a role=dialog with Accept / Decline / Manage
        # Preferences) overlays the guest-auth step + blocks the Next/Submit click. SCOPE the
        # click to a cookie container so a stray 'Accept' elsewhere on the form is never hit.
        for csel in (".cookie-consent-modal button:has-text('Accept')",
                     ".cookie-consent button:has-text('Accept')",
                     "[class*='cookie'] button:has-text('Accept')",
                     ".cookie-consent-modal button:has-text('Decline')",
                     "[class*='cookie'] button:has-text('Decline')"):
            try:
                b = page.locator(csel).first
                if await b.count() and await b.is_visible(timeout=500):
                    await b.click(timeout=1500)
                    await page.wait_for_timeout(250)
                    return
            except Exception:
                continue
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

    async def _tick_terms(self, page: Page) -> None:
        """Accept the guest-auth Terms & Conditions so the auth 'Next' enables + reveals the full
        application form. Oracle CX gates acceptance behind an AGREEMENT DIALOG (a role=dialog with
        per-country info links — all target=_blank — and an 'Agree' button) bound to the Knockout
        observable legalDisclaimer.isAccepted. Acceptance = clicking 'Agree' (NOT a country link,
        which just opens the legal text in a new tab). We open the dialog via #legal-disclaimer-link
        if it isn't already up, click 'Agree', and force-check the hidden <input> as a fallback.
        No-op when the disclaimer checkbox is absent (later steps) or already accepted."""
        try:
            state = await page.evaluate(
                "()=>{const c=document.getElementById('legal-disclaimer-checkbox');"
                "return c?(c.checked?'checked':'present'):'absent';}")
        except Exception:
            state = "absent"
        if state != "present":
            return

        async def _click_agree() -> bool:
            for sel in ("div[class*='dialog'] button:has-text('Agree')",
                        ".app-dialog button:has-text('Agree')",
                        "button:has-text('Agree')"):
                try:
                    b = page.locator(sel).first
                    if await b.count() and await b.is_visible(timeout=600):
                        await b.click(timeout=1500)
                        await page.wait_for_timeout(400)
                        return True
                except Exception:
                    continue
            return False

        # 1) If the agreement dialog is already open, just Agree; else open it via the disclaimer
        #    link (NEVER a country link — those spawn target=_blank tabs), then Agree.
        if not await _click_agree():
            for sel in ("#legal-disclaimer-link", "a#legal-disclaimer-link",
                        "label[for='legal-disclaimer-checkbox'] a:has-text('terms')"):
                try:
                    b = page.locator(sel).first
                    if await b.count() and await b.is_visible(timeout=600):
                        await b.click(timeout=1500)
                        await page.wait_for_timeout(600)
                        break
                except Exception:
                    continue
            await _click_agree()
        # 2) Fallback: force-check the hidden native input + dispatch so the Knockout checked
        #    binding (legalDisclaimer.isAccepted) fires even if the Agree button wasn't found.
        try:
            await page.evaluate(
                "()=>{const c=document.getElementById('legal-disclaimer-checkbox');"
                "if(c&&!c.checked){c.checked=true;"
                "c.dispatchEvent(new Event('click',{bubbles:true}));"
                "c.dispatchEvent(new Event('input',{bubbles:true}));"
                "c.dispatchEvent(new Event('change',{bubbles:true}));}}")
        except Exception:
            pass

    async def _wait_for_form_render(self, page: Page, timeout_ms: int = 16000) -> bool:
        """After the guest-auth Next, the full single-page application form renders asynchronously.
        Poll until real form widgets (JET/CX role=radio / role=combobox groups) appear, so the gap
        fill runs against the actual form and not the still-loading auth step."""
        import time as _t
        deadline = _t.time() + timeout_ms / 1000.0
        while _t.time() < deadline:
            await self._dismiss_cookie_banner(page)
            try:
                n = await page.evaluate(
                    "()=>document.querySelectorAll('[role=radio],[role=combobox],"
                    "input[type=file]').length")
            except Exception:
                n = 0
            if n and n > 0:
                await page.wait_for_timeout(800)
                return True
            await page.wait_for_timeout(600)
        return False

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
        """Fill a newly-revealed wizard step. On the full single-page application form this is the
        WHOLE gap fill (identity/address comboboxes, Title + Yes/No screener radios, EEO decline,
        WOTC) plus the generic analyzer pass; on an EEO/voluntary/review step it degrades to the
        decline + consent + screener helpers those steps need."""
        await self._dismiss_cookie_banner(page)
        # Run the ordinary analyzer pass FIRST (fills the plain name/email/address text inputs the
        # newly-rendered form exposes), then the ORC-specific JET/CX gap fill (comboboxes, radios,
        # EEO decline, WOTC) which owns the widgets analyze_page can't.
        try:
            analysis = await analyze_page(page, profile_form, cover_letter, {}, facts or {})
            await fill_form(page, analysis)
        except Exception as exc:
            logger.debug("oracle_orc: step analyze/fill raised: %s", exc)
        try:
            await self._fill_orc_gaps(page, profile_form, facts)
        except Exception as exc:
            logger.debug("oracle_orc: step gap fill raised: %s", exc)
        try:
            await self._handle_wotc(page, profile_form)
        except Exception as exc:
            logger.debug("oracle_orc: step wotc raised: %s", exc)

    # ---- WOTC (Work Opportunity Tax Credit) 'Take Tax Credit Assessment' ----
    _WOTC_DONE_RE = re.compile(
        r"thank you|assessment (?:is )?complete|completed|you have completed|"
        r"return to (?:your )?application|no (?:further )?questions|survey complete", re.I)

    async def _handle_wotc(self, page: Page, profile_form: dict) -> bool:
        """Oracle CX gates Submit on a 'Tax Credit Assessment' (WOTC). 'Take Tax Credit Assessment'
        navigates the tab SAME-WINDOW to the ADP jobcredits.com partner survey, where `_wotc_answer_no`
        opts out (no SSN fabricated — WOTC is voluntary, "will NOT negatively impact consideration").
        Returns True if handled. **OPT-IN via ORC_WOTC_OPTOUT=1** — the partner opt-out works but its
        ASP.NET postback redirect back to the Oracle SPA is slow/flaky and can stall a fill, so by
        default we DON'T navigate: the WOTC stays a pending step in `unfilled` (harmless, since the
        reserved-fiction phone already blocks Submit as an owner-policy wall). Enable it once ORC_PHONE
        is set and the lane is being driven to a real ack. Runs AT MOST once per fill (it navigates
        the tab; a 2nd pass — the form step + the review step both call it — would re-navigate)."""
        if os.getenv("ORC_WOTC_OPTOUT", "").strip().lower() not in ("1", "true", "yes", "on"):
            return False
        if getattr(self, "_wotc_attempted", False):
            return False
        try:
            body = (await page.locator("body").inner_text(timeout=4000)).lower()
        except Exception:
            body = ""
        if "tax credit" not in body:
            return False
        self._wotc_attempted = True
        ctx = page.context
        before = list(ctx.pages)
        clicked = False
        for sel in ('a:has-text("Take Tax Credit Assessment")',
                    'button:has-text("Take Tax Credit Assessment")',
                    'a:has-text("Tax Credit Assessment")', 'button:has-text("Tax Credit Assessment")',
                    'a:has-text("Tax Credit")', 'button:has-text("Tax Credit")',
                    'oj-button:has-text("Tax Credit") button'):
            try:
                b = page.locator(sel).first
                if await b.count() and await b.is_visible(timeout=1000):
                    await b.click(timeout=3000)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            return False
        await page.wait_for_timeout(3500)
        # The partner survey usually opens in a NEW TAB; operate there, else on this page.
        survey = page
        try:
            if len(ctx.pages) > len(before):
                survey = ctx.pages[-1]
        except Exception:
            pass
        try:
            await survey.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        # Walk the survey — OPT OUT is the primary path (a synthetic persona has no SSN, and WOTC is
        # voluntary: "your answers will NOT negatively impact consideration of your application"). The
        # jobcredits.com partner (ADP) opts out via a link -> confirm "Opt Out" -> a J-1-visa confirm
        # modal (answer No: a US persona is not a J-1 exchange visitor). Only if NO opt-out affordance
        # exists do we answer everything No/decline + advance (which still can't pass an SSN gate).
        import time as _t
        wotc_deadline = _t.time() + 90                    # hard cap so a stuck survey can't hang the fill
        opted_out = False
        for _ in range(8):
            if _t.time() > wotc_deadline:
                break
            await page.wait_for_timeout(1000)
            try:
                surl = (survey.url or "").lower()
            except Exception:
                surl = ""
            if "oraclecloud.com" in surl:
                break                                     # back on the application
            # 1) trigger the opt-out (link OR <input type=submit value='Opt Out'>).
            if not opted_out:
                for sel in ("#OptOutVisibleLink", "a[data-open*='optout' i]",
                            "a[aria-controls*='optout' i]", "input[value='Opt Out' i]",
                            "a:has-text('Opt Out')", "button:has-text('Opt Out')",
                            "a:has-text('Decline')", "button:has-text('Decline')"):
                    try:
                        loc = survey.locator(sel).first
                        if await loc.count() and await loc.is_visible(timeout=500):
                            await loc.click(timeout=2500)
                            opted_out = True
                            await survey.wait_for_timeout(700)
                            break
                    except Exception:
                        continue
            # 2) confirm the opt-out modal, then answer the follow-up J-1-visa confirm (No).
            for sel in ("#OptOutConfirmYesButton", "input[name='OptOutConfirmYesButton']",
                        ".reveal input[value='Opt Out' i]", "input[value='Opt Out' i]",
                        "button:has-text('Confirm')", "input[value='Yes' i]"):
                try:
                    loc = survey.locator(sel).first
                    if await loc.count() and await loc.is_visible(timeout=500):
                        await loc.click(timeout=2500)
                        await survey.wait_for_timeout(700)
                        break
                except Exception:
                    continue
            for sel in ("#j1VisaOptOutConfirmNoButton", "input[name='j1VisaOptOutConfirmNoButton']"):
                try:
                    loc = survey.locator(sel).first
                    if await loc.count() and await loc.is_visible(timeout=500):
                        await loc.click(timeout=2500)
                        await survey.wait_for_timeout(700)
                        break
                except Exception:
                    continue
            # 3) if no opt-out was available at all, fall back to answer-No + advance.
            if not opted_out:
                try:
                    stext = (await survey.locator("body").inner_text(timeout=3000))
                except Exception:
                    stext = ""
                if self._WOTC_DONE_RE.search(stext or ""):
                    break
                await self._wotc_answer_no(survey, profile_form)
                if not await self._wotc_advance(survey):
                    break
            try:
                await survey.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
        # Return focus to the application tab.
        if survey is not page:
            try:
                await survey.close()
            except Exception:
                pass
            try:
                await page.bring_to_front()
            except Exception:
                pass
        await page.wait_for_timeout(1500)
        # SAFETY: the same-tab opt-out should redirect back to the Oracle SPA, but if the partner
        # postback stalled and left us on jobcredits.com, go_back so the surrounding fill/submit runs
        # against the application — never leave the tab stranded on the partner (that stalls the fill).
        try:
            if "oraclecloud.com" not in (page.url or "").lower():
                for _ in range(3):
                    await page.go_back(timeout=8000)
                    await page.wait_for_timeout(1500)
                    if "oraclecloud.com" in (page.url or "").lower():
                        break
        except Exception:
            pass
        logger.debug("oracle_orc: WOTC handled (opted_out=%s)", opted_out)
        return True

    async def _wotc_answer_no(self, q: Page, profile_form: dict) -> None:
        """Answer every WOTC eligibility question No/decline; fill required Name/DOB/ZIP text
        (NEVER an SSN — a synthetic persona has none, and a fake SSN must not be transmitted to a
        government-adjacent partner). Mirrors taleo._wotc_fill's polarity."""
        pf = profile_form or {}
        full = (pf.get("full_name") or pf.get("name") or "").strip()
        parts = full.split()
        first = pf.get("first_name") or (parts[0] if parts else "")
        last = pf.get("last_name") or (parts[-1] if len(parts) > 1 else "")
        dob = pf.get("dob") or pf.get("date_of_birth") or "01/01/1995"
        zc = pf.get("zip") or pf.get("postal_code") or ""
        try:
            await q.evaluate(
                """([first,last,dob,zc])=>{
                  const n=s=>(s||'').toLowerCase();
                  const labOf=el=>{let t='';if(el.id){const l=document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(el.id):el.id)+'"]');if(l)t=l.innerText;}
                    if(!t)t=el.getAttribute('aria-label')||el.getAttribute('placeholder')||'';
                    if(!t){const w=el.closest('div,td,li,tr,p');if(w&&(w.innerText||'').length<120)t=w.innerText;} return n(t);};
                  const setv=(el,v)=>{if(!v)return;el.value=v;el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));};
                  for(const el of document.querySelectorAll('input[type=text],input:not([type]),input[type=tel],input[type=date],input[type=number]')){
                    const ty=(el.type||'').toLowerCase(); if(['hidden','submit','button','checkbox','radio','file'].includes(ty))continue;
                    if((el.value||'').trim())continue; const lab=labOf(el);
                    if(/social security|\\bssn\\b/.test(lab))continue;            // never fill an SSN
                    if(/first name|given name/.test(lab))setv(el,first); else if(/last name|surname|family name/.test(lab))setv(el,last);
                    else if(/date of birth|birth date|\\bdob\\b/.test(lab))setv(el,dob);
                    else if(/zip|postal/.test(lab))setv(el,zc);}
                  const rg={}; for(const r of document.querySelectorAll('input[type=radio]')){if(r.name)(rg[r.name]=rg[r.name]||[]).push(r);}
                  const rlab=r=>{const l=r.id?document.querySelector('label[for="'+(window.CSS&&CSS.escape?CSS.escape(r.id):r.id)+'"]'):null;return n(((l&&l.innerText)||(r.closest('label')?r.closest('label').innerText:'')||''));};
                  for(const nm in rg){const rs=rg[nm]; if(rs.some(r=>r.checked))continue;
                    let pick=rs.find(r=>/^\\s*no\\b|none of these|does not|do not|decline|not a member|not applicable|n\\/a/.test(rlab(r)))||rs.find(r=>/^\\s*no\\s*$/.test(n(r.value||'')));
                    if(pick){pick.checked=true;pick.dispatchEvent(new Event('click',{bubbles:true}));pick.dispatchEvent(new Event('change',{bubbles:true}));}}
                  for(const sel of document.querySelectorAll('select')){if(sel.multiple)continue;const cur=sel.options[sel.selectedIndex];
                    if(sel.value&&cur&&!/select|choose|^--|no selection/.test(n(cur.text)))continue;
                    const o=[...sel.options].find(o=>o.value&&/^\\s*no\\s*$|none|decline|not a\\b|n\\/a/.test(n(o.text)))||[...sel.options].find(o=>o.value&&!/select|choose|^--|no selection/.test(n(o.text)));
                    if(o){sel.value=o.value;sel.dispatchEvent(new Event('change',{bubbles:true}));}}
                  for(const c of document.querySelectorAll('input[type=checkbox]')){if(c.checked)continue;const lab=labOf(c);
                    if(/agree|consent|acknowledge|certify|understand|authorize|i have read|confirm|signature/.test(lab)&&!/marketing|newsletter|opt.?in to receive/.test(lab)){
                      c.checked=true;c.dispatchEvent(new Event('click',{bubbles:true}));c.dispatchEvent(new Event('change',{bubbles:true}));}}
                }""", [first, last, dob, zc])
        except Exception as exc:
            logger.debug("oracle_orc: wotc answer raised: %s", exc)

    async def _wotc_advance(self, q: Page) -> bool:
        for sel in ('input[value="Submit" i]', 'button:has-text("Submit")', 'input[value="Continue" i]',
                    'button:has-text("Continue")', 'button:has-text("Next")', 'input[value="Next" i]',
                    'button:has-text("Finish")', 'button:has-text("Done")', 'a:has-text("Submit")',
                    'button[type="submit"]', 'input[type="submit"]'):
            try:
                loc = q.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=600):
                    await loc.click(timeout=4000)
                    return True
            except Exception:
                continue
        return False

    async def _advance_wizard(self, page, report, profile_form, cover_letter, facts) -> None:
        """Walk the multi-step wizard: click Continue while it advances (filling each new step),
        and STOP at the final Submit — recording its selector in the report WITHOUT clicking it.
        If a Continue click does NOT advance (validation blocked it because a required field is
        still empty), stop and leave the gaps in `unfilled` for the human / next iteration."""
        for _ in range(6):
            await self._dismiss_cookie_banner(page)
            # Tick the guest-auth Terms checkbox (if this is that step) BEFORE reading the primary
            # button — an unaccepted disclaimer makes the auth Next silently validation-fail.
            await self._tick_terms(page)
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
            n_fields_before = await self._field_count(page)
            try:
                await btn.click()
                await page.wait_for_timeout(1500)
            except Exception:
                break
            # The full single-page form renders asynchronously after the guest-auth Next; wait for
            # its widgets to appear (a signature check alone misses the async render + false-fires
            # "blocked"). Advanced == the step signature changed OR new form widgets appeared.
            rendered = await self._wait_for_form_render(page)
            advanced = rendered or (await self._step_signature(page) != sig) \
                or (await self._field_count(page) > n_fields_before)
            if not advanced:
                # Did not advance -> a required field on this step is still empty. Stop; the
                # human / next iteration finishes it (the dry-run screenshot shows what's left).
                report["wizard_blocked_step"] = sig
                report["unfilled"] = await self._rescan_required(page)
                return
            await self._fill_current_step(page, profile_form, cover_letter, facts)

    async def _field_count(self, page: Page) -> int:
        try:
            return await page.evaluate(
                "()=>document.querySelectorAll('[role=radio],[role=combobox],"
                "input[type=file]').length")
        except Exception:
            return 0
