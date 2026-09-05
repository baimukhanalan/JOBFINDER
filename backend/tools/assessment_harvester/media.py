"""Screenshot capture for harvested items -> repo-relative path under
`backend/data/assessment_media/<platform>/<sha>.png`.

Idempotent by a sha of (question + option texts + url), so re-seeing the same item overwrites the
same file instead of piling up. Full-item viewport screenshot (the whole page) — enough for the
owner to SEE the real math/picture/speaking questions.
"""
from __future__ import annotations

import hashlib
import os

_MEDIA_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "data", "assessment_media")
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _sha(*parts: str) -> str:
    return hashlib.sha1("||".join(p or "" for p in parts).encode("utf-8", "ignore")).hexdigest()[:16]


async def capture(page, platform: str, question: str, option_texts: list[str], url: str = "") -> str | None:
    """Screenshot the current page for this item. Returns a repo-relative path (for the bank), or
    None on failure. Never raises."""
    try:
        sha = _sha(question, "|".join(option_texts or []), url)
        d = os.path.join(_MEDIA_ROOT, platform)
        os.makedirs(d, exist_ok=True)
        abspath = os.path.join(d, f"{sha}.png")
        await page.screenshot(path=abspath, full_page=False)
        rel = os.path.relpath(abspath, _REPO_ROOT)
        return rel
    except Exception:
        return None
