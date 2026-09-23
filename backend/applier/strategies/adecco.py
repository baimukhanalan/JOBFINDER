"""Adecco apply strategy — the candidate.adecco.com "easyApply" React SPA account wall.

Adecco's per-job "Apply now" (adecco.com/en-us/job-search/…) hands off to its own candidate
SPA `candidate.adecco.com/easyApply?queryState=…` (recon 2026-09-23), which gates the
application behind a candidate ACCOUNT (email + password, the `signUp_portal` user-flow) +
an emailed verification code, with a reCAPTCHA on the register/submit step:

    adecco.com/en-us/job-search/<slug>   -- Apply now -->  candidate.adecco.com/easyApply
      create account (email + password)  — reCAPTCHA on register
      -> emailed OTP  (verify_code.read_code from the persona @takhet.com Maildir)
      -> authenticated easyApply form (contact / résumé / screeners / consent / Submit)

Reuses every solved primitive (reCAPTCHA → captcha_solver.solve_on_page; email OTP →
verify_code.read_code; the authenticated form → the shared base.prefill pipeline). It shares
the account-bootstrap + wizard machinery with the Robert Half lane (same Salesforce-class
account-then-apply shape), overriding only the Adecco host, wall detection, apply hand-off,
and the ADECCO_ADVANCE gate.

LIVE actions (create the account, transmit PII, click Submit) are GATED behind env
ADECCO_ADVANCE — OFF by default (a dry-run only lands on the candidate.adecco.com wall,
reported `needs_account`; nothing created, no PII transmitted).
"""
import os
import re

from playwright.async_api import Page

from backend.applier.strategies.roberthalf import RobertHalfStrategy

_WALL_TEXT_RE = re.compile(
    r"create (?:an )?account|create your account|sign in|sign up|register|log ?in|"
    r"verification code|verify your email|easyapply|easy apply", re.I)


def _env_advance() -> bool:
    """True only when ADECCO_ADVANCE is explicitly set (the live account-create/submit switch)."""
    return os.getenv("ADECCO_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


class AdeccoStrategy(RobertHalfStrategy):
    name = "adecco"
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        u = (url or "").lower()
        return "adecco.com" in u

    async def _click_apply(self, page: Page) -> None:
        """The job page's 'Apply now' opens the candidate.adecco.com easyApply SPA."""
        for sel in ('a:has-text("Apply now")', 'button:has-text("Apply now")',
                    'a:has-text("APPLY NOW")', 'button:has-text("Apply")',
                    'a:has-text("Apply")', 'a[href*="candidate.adecco.com" i]',
                    'a[href*="easyapply" i]'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=1200):
                    await loc.click(timeout=4000)
                    await page.wait_for_timeout(2800)
                    return
            except Exception:
                continue

    async def _on_account_wall(self, page: Page) -> bool:
        """True when on the candidate.adecco.com account/easyApply wall, not the filled form."""
        try:
            u = (page.url or "").lower()
        except Exception:
            u = ""
        if "candidate.adecco.com" in u:
            # the easyApply SPA is the account wall until an email+password account exists.
            has_pw = False
            try:
                has_pw = bool(await page.locator('input[type="password"]').count())
            except Exception:
                pass
            if has_pw:
                return True
            try:
                txt = ((await page.inner_text("body"))[:4000] or "")
            except Exception:
                txt = ""
            return bool(_WALL_TEXT_RE.search(txt))
        return False
