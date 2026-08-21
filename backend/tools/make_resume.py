"""Render a candidate's résumé to a PDF in ~/Downloads (for the extension flow's
"Autofill from resume" upload).

    python -m backend.tools.make_resume --profile gen_kz_01_gulnara_nurpeisova
    python -m backend.tools.make_resume --profile <id> --job-url <ashby apply url>

With --job-url the résumé is tailored to that position's keywords (same tailoring the
prefill engine uses); without it, the base résumé is rendered.
"""
from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

import httpx

from backend.applier.browser import BrowserManager
from backend.applier.runner import _html_to_pdf
from backend.profiles.store import get_profile
from backend.services.tailor.render import render_html
from backend.services.tailor.tailor import tailor_resume

DOWNLOADS = Path.home() / "Downloads"


def _job_from_url(url: str) -> dict:
    """Fetch title + description for an Ashby apply URL (best-effort)."""
    m = re.search(r"ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url or "")
    if not m:
        return {"title": "", "description": ""}
    org, jid = m.group(1), m.group(2)
    try:
        board = httpx.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}",
                          timeout=20).json().get("jobs", [])
        job = next((j for j in board if j.get("id") == jid), None)
        if job:
            return {"title": job.get("title", ""),
                    "description": job.get("descriptionPlain", "")}
    except Exception:
        pass
    return {"title": "", "description": ""}


def _pick_online_role() -> tuple[str, dict]:
    """Default target = a random CS role that allows online work (same set the bot uses)."""
    import random
    from backend.tools.online_roles import online_cs_roles
    roles = online_cs_roles()
    if not roles:
        return "", {"title": "", "description": ""}
    j = roles[random.randrange(len(roles))]
    return j.get("applyUrl", ""), {"title": j.get("title", ""),
                                   "description": j.get("descriptionPlain", "")}


async def _run(profile_id: str, job_url: str, use_ai: bool) -> Path:
    profile = get_profile(profile_id)
    if job_url:
        job = _job_from_url(job_url)
    else:  # no URL given -> tailor to a random ONLINE role by default
        job_url, job = _pick_online_role()
    tailored = tailor_resume(profile.resume, job.get("title", ""), "Salmon",
                             job.get("description", ""), use_ai=use_ai)
    html = render_html(tailored)
    safe = re.sub(r"[^a-z0-9]+", "_", (profile.full_name or profile_id).lower()).strip("_")
    out = DOWNLOADS / f"resume_{safe}.pdf"
    bm = BrowserManager(headless=True)
    await bm.start()
    try:
        await _html_to_pdf(bm, html, out, title=profile.full_name, author=profile.full_name)
    finally:
        await bm.close()
    return out, tailored.get("match_score"), job.get("title", "")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True, help="profiles.json id")
    ap.add_argument("--job-url", default="", help="tailor to this Ashby position")
    ap.add_argument("--ai", action="store_true", help="LLM-polish to mirror JD vocabulary")
    a = ap.parse_args()
    out, score, title = asyncio.run(_run(a.profile, a.job_url, a.ai))
    print(f"Wrote {out}")
    if title:
        tag = "" if a.job_url else " (auto-picked online role)"
        print(f"Tailored to: {title}{tag} — keyword match score: {score}%")


if __name__ == "__main__":
    main()
