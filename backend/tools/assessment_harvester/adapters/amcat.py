"""AMCAT / Aspiring Minds adapter (Teleperformance downstream).

Entry: the `amcatglobal.aspiringminds.com/?autoLoginVersion=3&token=<JWT>` autologin link from the
"TP Assessment - Test Login Details" email. The battery is numerical / verbal / logical / domain
(cognitive MCQ), AMPI personality, and sometimes SVAR speaking + typing.

LIVE STATUS (verified 2026-09-05): every currently-available token (all 272, incl. the freshest at
~21h old) loads to "This assessment has either been completed or submitted. Message code TC100"
BEFORE any interaction — the test instance is already terminal on arrival (auto-submit/expire, or the
token is consumed at generation). So the harvester reaches no live AMCAT items right now; a token must
be opened inside its live window (right after the invite lands). The adapter is complete and will
harvest the moment it meets a live token.
"""
from __future__ import annotations

import re

from backend.tools.assessment_harvester.adapters.base import Adapter

_TERMINAL_RE = re.compile(
    r"completed or submitted|no further action|message code tc|has expired|no longer available|"
    r"link is invalid|session (has )?expired", re.I)


class AmcatAdapter(Adapter):
    platform = "amcat"

    async def enter(self, page, url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)   # autologin redirect + SPA boot
        await self.dismiss_noise(page)
        # section-start / instructions -> Start Test / Begin / I'm ready
        for _ in range(3):
            if await self.advance(page):
                await page.wait_for_timeout(2500)
            else:
                break

    async def is_terminal(self, page) -> bool:
        try:
            body = (await page.inner_text("body", timeout=3000)).lower()
        except Exception:
            body = ""
        return bool(_TERMINAL_RE.search(body))

    async def is_done(self, page) -> bool:
        return await super().is_done(page)

    async def wall(self, page) -> str | None:
        """Every current AMCAT token loads to TC100 'completed or submitted' before any interaction —
        the test instance is terminal on arrival. That is the genuine wall (a dead token)."""
        if await self.is_terminal(page):
            return "terminal_token"
        return None
