"""Open ONE already-known application in a visible browser, pre-fill it, and hold
it open so a human can review and click Submit. Closed by sending a newline to
stdin (the Telegram bot does this on the "Done" button) or pressing Enter.

    python -m backend.tools.open_for_submit --profile <id> \
        --url <apply_url> --title "<role>" [--company Salmon] [--ai] [--draft]

The engine never clicks Submit — it only fills and waits.
"""
from __future__ import annotations

import argparse
import asyncio

from backend.applier.runner import prefill_application
from backend.profiles.store import get_profile


async def _main(a: argparse.Namespace) -> None:
    profile = get_profile(a.profile)
    job = {"title": a.title, "company": a.company,
           "description": a.description, "apply_url": a.url}
    # skip_gate: this exact pairing already cleared the gate during the headless
    # pre-fill; re-opening it for the human to submit must never be re-rejected.
    await prefill_application(job, profile, headless=False, use_ai=a.ai,
                             draft_answers=a.draft, use_variants=False,
                             hold_open=True, skip_gate=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--title", default="Customer Service Specialist")
    ap.add_argument("--company", default="Salmon")
    ap.add_argument("--description", default="")
    ap.add_argument("--ai", action="store_true")
    ap.add_argument("--draft", action="store_true")
    asyncio.run(_main(ap.parse_args()))


if __name__ == "__main__":
    main()
