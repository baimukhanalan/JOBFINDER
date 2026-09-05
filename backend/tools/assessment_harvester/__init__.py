"""Assessment question-bank HARVESTER.

Enters a post-apply hiring assessment as a SYNTHETIC persona, walks all the way through answering
RANDOMLY (owner-authorized — random answers deliberately FAIL the scored test; the point is to
CAPTURE the questions, not pass), and saves every question + all answer options + a screenshot of
each item into a unified, platform-scoped question bank (`backend/data/assessment_bank.json`).

This is DISTINCT from `shl_assessment.py` (the etalon, which answers to PASS the Maximus OPQ and
hard-stops on cognitive items). The harvester's boundary is purely mechanical: it can screenshot +
bank a cognitive MCQ and click a random option, but it cannot produce a valid answer for a
FREE-RESPONSE item (typing/speaking/video) — there it captures the prompt + a screenshot and then
the run stalls at that gate (the honest ceiling).

Shared core (`core.harvest`) + per-platform adapters (`adapters/`). The etalon files are NOT
touched; the SHL adapter (future) imports their battle-tested helpers rather than duplicating them.

SYNTHETIC personas only. Headful only (AMCAT/SHL reject headless) — run under DISPLAY=:98 + sg mail.
"""
