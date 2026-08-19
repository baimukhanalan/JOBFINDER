"""Test harness: generate fictional candidates and pre-fill Salmon (Ashby) forms.

    python -m backend.tools.salmon_autofill --count 3 --role-filter cs --ai --tg

For EACH generated profile it: writes the profile + fact sheet, picks a live
Salmon role, and runs the normal prefill engine against the real Ashby form —
tailoring a résumé, filling every field it can, screenshotting, and STOPPING at
the Submit button. The engine has no auto-submit path, so nothing is ever filed.

Outputs land where the dashboard already reads them:
    uploads/prefill/<profile_id>/<slug>/{report.json, resume.pdf, prefilled.png}
Review at  http://127.0.0.1:8089/?profile=<profile_id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from backend.applier.profile_validator import validate_profile
from backend.applier.runner import prefill_application
from backend.config import settings
from backend.profiles.store import PROJECT_ROOT, Profile
from backend.tools.gen_profiles import generate

ASHBY_BOARD = "https://api.ashbyhq.com/posting-api/job-board/salmon-group?includeCompensation=true"
DATA_DIR = PROJECT_ROOT / "backend" / "data"
PROFILES_JSON = DATA_DIR / "profiles.json"
FACTS_DIR = DATA_DIR / "facts"

_CS_KEYWORDS = ("support", "customer", "service", "care", "collection", "agent",
                "account services", "operations", "quality", "experience", "specialist")


def fetch_salmon_jobs(role_filter: str) -> list[dict]:
    r = httpx.get(ASHBY_BOARD, timeout=30)
    r.raise_for_status()
    jobs = r.json().get("jobs", [])
    if role_filter == "cs":
        jobs = [j for j in jobs
                if any(k in (j.get("title", "").lower()) for k in _CS_KEYWORDS)]
    # stable order for reproducible round-robin
    jobs.sort(key=lambda j: j.get("title", ""))
    return jobs


def _load_profiles_list() -> list[dict]:
    if PROFILES_JSON.exists():
        return json.loads(PROFILES_JSON.read_text(encoding="utf-8"))
    return []


def _save_profiles_list(items: list[dict]) -> None:
    PROFILES_JSON.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def _upsert_profile(profile: dict, facts: dict) -> None:
    items = _load_profiles_list()
    items = [p for p in items if p.get("id") != profile["id"]]  # replace if regenerated
    items.append(profile)
    _save_profiles_list(items)
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    (FACTS_DIR / f"{profile['id']}.json").write_text(
        json.dumps(facts, indent=2, ensure_ascii=False), encoding="utf-8")


def tg_notify(text: str) -> bool:
    token = settings.telegram_bot_token
    chat = settings.telegram_chat_id
    if not token or not chat:
        return False
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       data={"chat_id": chat, "text": text,
                             "disable_web_page_preview": "true"}, timeout=20)
        ok = r.json().get("ok", False)
        if not ok:
            print(f"  [tg] send failed: {r.json().get('description')}")
        return ok
    except Exception as exc:
        print(f"  [tg] error: {exc}")
        return False


async def run(count: int, role_filter: str, headful: bool, use_ai: bool,
              draft: bool, tg: bool, seed: int | None, hold: bool = False,
              parser_only: bool = False) -> None:
    if hold:
        headful = True  # holding open only makes sense with a visible browser
    jobs = fetch_salmon_jobs(role_filter)
    if not jobs:
        raise SystemExit("no Salmon roles matched the filter")
    print(f"Salmon roles available (filter={role_filter}): {len(jobs)}")

    results = []
    for i in range(count):
        role = jobs[i % len(jobs)]
        role_title = role.get("title", "Customer Service Specialist")
        profile_d, facts = generate(i + 1, role_title=role_title,
                                    seed=(seed + i if seed is not None else None),
                                    use_llm=use_ai)
        problems = validate_profile(profile_d)
        gate = "OK" if not problems else f"BLOCKED({'; '.join(problems)})"
        _upsert_profile(profile_d, facts)
        profile = Profile.from_dict(profile_d)

        job = {
            "title": role_title,
            "company": "Salmon",
            "description": role.get("descriptionPlain", "")[:8000],
            "apply_url": role.get("applyUrl"),
        }
        print(f"\n[{i+1}/{count}] {profile.full_name}  ->  {role_title}")
        print(f"        profile={profile.id}  gate={gate}")
        print(f"        {job['apply_url']}")
        try:
            rep = await prefill_application(job, profile, headless=not headful,
                                            use_ai=use_ai, draft_answers=draft,
                                            use_variants=False, hold_open=hold,
                                            resume_parser_only=parser_only)
            src = rep.get("answer_sources", {})
            line = (f"filled={rep.get('filled')} failed={rep.get('failed')} "
                    f"page={rep.get('page_type')} review={len(rep.get('review_items', []))} "
                    f"sources={src}")
            print(f"        {line}")
            results.append((profile.full_name, role_title, profile.id, rep))
        except Exception as exc:
            print(f"        ERROR: {exc}")
            results.append((profile.full_name, role_title, profile.id, {"error": str(exc)}))

    ok = sum(1 for *_, r in results if not r.get("error"))
    print(f"\nDone: {ok}/{count} prefilled (nothing submitted — by design).")
    print("Review each at  http://127.0.0.1:8089/?profile=<id>")

    if tg:
        lines = [f"🐟 Salmon prefill test — {ok}/{count} forms filled (not submitted)"]
        for name, title, pid, r in results:
            if r.get("error"):
                lines.append(f"• {name} → {title}: ERROR {r['error'][:60]}")
            else:
                lines.append(f"• {name} → {title}: filled {r.get('filled')}, "
                             f"page {r.get('page_type')}")
        tg_notify("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=3, help="how many fictional candidates")
    ap.add_argument("--role-filter", choices=["cs", "all"], default="cs",
                    help="cs = support/collections/service roles only")
    ap.add_argument("--headful", action="store_true", help="show the browser")
    ap.add_argument("--ai", action="store_true",
                    help="use Groq for résumé generation + polish (slower, more varied)")
    ap.add_argument("--draft", action="store_true",
                    help="LLM-draft answers to open questions (needs --ai backend)")
    ap.add_argument("--tg", action="store_true", help="send a Telegram summary")
    ap.add_argument("--seed", type=int, default=None, help="deterministic identities")
    ap.add_argument("--parser-only", dest="parser_only", action="store_true",
                    help="résumé-parser-only (anti-spam): only feed the résumé to the ATS's "
                         "own parser + attach it; fill nothing else, you finish by hand")
    ap.add_argument("--hold", action="store_true",
                    help="open a visible browser, pre-fill, and KEEP IT OPEN so you can "
                         "review and click Submit yourself (implies --headful)")
    a = ap.parse_args()
    asyncio.run(run(a.count, a.role_filter, a.headful, a.ai, a.draft, a.tg, a.seed, a.hold,
                    a.parser_only))


if __name__ == "__main__":
    main()
