"""Talemetry apply strategy — the apply.talemetry.com account wall (used for Progressive).

Progressive's careers site (careers.progressive.com, Cloudflare-fronted Talemetry SSR) hands
the per-job "Apply" off to Talemetry's applicant portal `apply.talemetry.com/application/<guid>`
(recon 2026-09-23), which gates the application behind an applicant ACCOUNT (email + password)
+ an emailed verification code, with a reCAPTCHA on register/submit:

    careers.progressive.com/jobs/<id>-<slug>/  -- Apply -->  apply.talemetry.com/application/<guid>
      create account (email + password)  — reCAPTCHA on register
      -> emailed OTP  (verify_code.read_code from the persona @takhet.com Maildir)
      -> authenticated application form (contact / résumé / screeners / consent / Submit)

Reuses every solved primitive (Cloudflare on the careers hand-off is cleared by the headful
real-Chrome stealth context the recon driver launches; reCAPTCHA → captcha_solver.solve_on_page;
email OTP → verify_code.read_code; the authenticated form → the shared base.prefill pipeline).
It shares the account-bootstrap + wizard machinery with the Robert Half lane (same
account-then-apply shape), overriding only the Talemetry/Progressive hosts, wall detection,
apply hand-off, and the PROGRESSIVE_ADVANCE gate.

LIVE actions are GATED behind env PROGRESSIVE_ADVANCE — OFF by default (a dry-run only lands
on the apply.talemetry.com wall, reported `needs_account`; nothing created, no PII sent).
"""
import os
import re

from playwright.async_api import Page

from backend.applier.strategies.roberthalf import RobertHalfStrategy

_WALL_TEXT_RE = re.compile(
    r"create (?:an )?account|create your account|sign in|register|log ?in|"
    r"verification code|verify your email|new applicant|returning applicant", re.I)


def _env_advance() -> bool:
    """True only when PROGRESSIVE_ADVANCE is explicitly set (the live account-create/submit
    switch for the Progressive/Talemetry lane)."""
    return os.getenv("PROGRESSIVE_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


class TalemetryStrategy(RobertHalfStrategy):
    name = "talemetry"
    advance_wizard = _env_advance()

    @classmethod
    def matches(cls, url: str) -> bool:
        u = (url or "").lower()
        # the Talemetry applicant portal + the Progressive careers hand-off page.
        return "talemetry.com" in u or "careers.progressive.com" in u

    async def _click_apply(self, page: Page) -> None:
        """The Progressive job page's 'Apply' opens the apply.talemetry.com application portal."""
        for sel in ('a:has-text("Apply Now")', 'button:has-text("Apply Now")',
                    'a:has-text("Apply for this job")', 'button:has-text("Apply")',
                    'a:has-text("Apply")', 'a[href*="talemetry.com" i]',
                    'a[href*="/application/" i]'):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=1200):
                    await loc.click(timeout=4000)
                    await page.wait_for_timeout(2800)
                    return
            except Exception:
                continue

    async def _on_account_wall(self, page: Page) -> bool:
        """True when on the apply.talemetry.com account wall, not the filled application form."""
        try:
            u = (page.url or "").lower()
        except Exception:
            u = ""
        if "talemetry.com" in u:
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
        # still on the Cloudflare-fronted careers page (Apply not yet followed) — not the form.
        return "careers.progressive.com" in u
