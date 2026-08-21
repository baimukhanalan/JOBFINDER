"""Open SEVERAL already-pre-filled applications as N TABS in ONE visible browser,
each re-filled, held open so a human reviews and clicks Submit on each. Closed by a
newline on stdin (the Telegram bot's "Done" button) or pressing Enter.

    python -m backend.tools.open_batch --spec /path/to/batch.json

The spec is a JSON list of applications already pre-filled headlessly:
    [{"profile": "<id>", "url": "<apply_url>", "title": "<role>",
      "company": "Salmon", "description": "<jd>", "resume_pdf": "<path>",
      "reply_addr": "<local>+<jid>@takhet.com"}, ...]

One shared browser context -> N tabs in a single window (fast: one Chromium launch).
The engine never clicks Submit — it only fills each tab and waits.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from backend.applier.browser import BrowserManager
from backend.applier.runner import _fill_leftovers, _pick_strategy
from backend.profiles.facts import load_facts
from backend.profiles.store import get_profile
from backend.services.tailor.answers import cover_letter
from backend.services.tailor.render import render_text
from backend.services.tailor.tailor import tailor_resume


async def _open_one(ctx, app: dict, draft: bool) -> None:
    """Open one apply URL in a new tab and re-fill it (same pipeline as the headless
    pre-fill). Never submits."""
    profile = get_profile(app["profile"])
    job = {"title": app.get("title", ""), "company": app.get("company", "Salmon"),
           "description": app.get("description", ""), "apply_url": app["url"]}
    form = profile.to_form_dict()
    form["linkedin_url"] = ""
    if app.get("reply_addr"):
        form["email"] = app["reply_addr"]
    resume_pdf = app.get("resume_pdf", "")
    if resume_pdf:
        form["resume_path"] = resume_pdf

    tailored = tailor_resume(profile.resume, job["title"], job["company"],
                             job["description"], use_ai=False)
    if app.get("reply_addr"):
        tailored.setdefault("personal_info", {})["email"] = app["reply_addr"]
    resume_text = render_text(tailored)

    page = await ctx.new_page()
    await page.goto(app["url"], wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2000)
    try:
        cover = cover_letter(job, resume_text, form, use_llm=draft)
    except Exception:
        cover = ""
    try:
        strategy = _pick_strategy(app["url"])
        fill_report = await strategy.prefill(page, form, resume_pdf, cover_letter=cover, job=job,
                                             draft=draft, resume_summary=resume_text,
                                             facts=load_facts(app["profile"]),
                                             profile_id=app["profile"], niche="")
        # In résumé-parser-only mode (RESUME_PARSER_ONLY=1) prefill types NOTHING beyond the
        # résumé autofill — so do NOT fill leftovers either, or the reopened tab the human
        # submits would fill other fields (the spam signal we're avoiding).
        if (fill_report or {}).get("mode") != "resume_parser_only":
            await _fill_leftovers(page, form)
    except Exception as exc:  # one tab failing must not sink the batch
        print(f"fill error on {app.get('title')}: {exc}")


async def _main(spec_path: str, draft: bool) -> None:
    import os
    apps = json.loads(Path(spec_path).read_text())
    # Visible by default (the human submits); OPEN_BATCH_HEADLESS=1 is for smoke tests
    # so CI/dev can exercise the fill path without popping windows.
    bm = BrowserManager(headless=os.getenv("OPEN_BATCH_HEADLESS") == "1")
    await bm.start()
    ctx = await bm.new_context()  # ONE context -> N tabs in ONE window
    try:
        for app in apps:
            await _open_one(ctx, app, draft)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, input,
            f"\n>>> {len(apps)} tabs open and pre-filled. Review each, click Submit "
            "yourself, then press Enter here to close them all. <<<\n")
    finally:
        await bm.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--spec", required=True, help="path to the batch JSON spec")
    ap.add_argument("--draft", action="store_true")
    a = ap.parse_args()
    asyncio.run(_main(a.spec, a.draft))


if __name__ == "__main__":
    main()
