"""Fail-closed final action for the isolated company application workflow.

This module is intentionally unaware of the legacy catalog, copilot, dashboard
and bulk state.  It receives an already-filled live Playwright page, performs
one final action, and reports success only when the destination page exposes a
positive confirmation signal.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_CONFIRM_TEXT = re.compile(
    r"(?i)thank you for (?:your )?application|thanks for applying|"
    r"application (?:has been )?(?:submitted|received)|"
    r"we (?:have )?received your application|submission confirmed"
)
_CONFIRM_URL = re.compile(r"(?i)/(?:thank(?:-you)?|confirmation|success|submitted)(?:[/?#]|$)")
_CAPTCHA_TEXT = re.compile(r"(?i)captcha|verify (?:that )?you are human|security challenge")

_PRIMARY_SELECTORS = (
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Submit application")',
    'button:has-text("Submit Application")',
    'button:has-text("Apply now")',
    'button:has-text("Apply Now")',
)


async def _visible_candidates(page) -> list[Any]:
    candidates: list[Any] = []
    seen: set[str] = set()
    for selector in _PRIMARY_SELECTORS:
        locator = page.locator(selector)
        for index in range(await locator.count()):
            item = locator.nth(index)
            try:
                if not await item.is_visible() or not await item.is_enabled():
                    continue
                identity = await item.evaluate(
                    "el => { window.__jobfinderFinalSeq = (window.__jobfinderFinalSeq || 0); "
                    "if (!el.__jobfinderFinalId) el.__jobfinderFinalId = "
                    "`jf-${++window.__jobfinderFinalSeq}`; return el.__jobfinderFinalId; }"
                )
            except Exception:
                continue
            if identity not in seen:
                seen.add(identity)
                candidates.append(item)
    return candidates


async def _has_captcha(page) -> bool:
    try:
        if await page.locator(
            'iframe[src*="captcha" i], iframe[title*="captcha" i], '
            '[class*="captcha" i], [id*="captcha" i]'
        ).count():
            return True
        text = (await page.locator("body").inner_text(timeout=3000))[:20000]
        return bool(_CAPTCHA_TEXT.search(text))
    except Exception:
        return True


async def submit_and_confirm(page, *, artifact_dir: str | Path | None = None,
                             settle_ms: int = 3500) -> dict:
    """Perform exactly one final click and require positive confirmation evidence.

    ``confirmed=False`` after a click is intentionally terminal/ambiguous for the
    caller: it must not be retried automatically because the server may have
    accepted the application without rendering a recognizable receipt.
    """
    before_url = str(getattr(page, "url", "") or "")
    if await _has_captcha(page):
        return {"confirmed": False, "clicked": False, "reason": "captcha_or_challenge"}

    candidates = await _visible_candidates(page)
    if len(candidates) != 1:
        return {
            "confirmed": False,
            "clicked": False,
            "reason": "missing_final_control" if not candidates else "ambiguous_final_controls",
            "candidate_count": len(candidates),
        }

    control = candidates[0]
    label = ""
    try:
        label = (await control.inner_text()).strip()[:120]
    except Exception:
        try:
            label = ((await control.get_attribute("value")) or "").strip()[:120]
        except Exception:
            pass

    await control.click(timeout=10000)
    await page.wait_for_timeout(max(1000, int(settle_ms)))

    after_url = str(getattr(page, "url", "") or "")
    try:
        body = (await page.locator("body").inner_text(timeout=5000))[:30000]
    except Exception:
        body = ""
    text_match = _CONFIRM_TEXT.search(body)
    url_match = _CONFIRM_URL.search(after_url) if after_url != before_url else None
    evidence = {
        "confirmed": bool(text_match or url_match),
        "clicked": True,
        "control_label": label,
        "before_url": before_url,
        "after_url": after_url,
        "signal": "confirmation_text" if text_match else (
            "confirmation_url" if url_match else "none"),
    }

    if artifact_dir is not None:
        destination = Path(artifact_dir) / "submission-result.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(destination), full_page=True)
            evidence["screenshot"] = str(destination)
        except Exception as exc:
            evidence["screenshot_error"] = str(exc)[:300]

    if not evidence["confirmed"]:
        evidence["reason"] = "ambiguous_after_click"
    return evidence
